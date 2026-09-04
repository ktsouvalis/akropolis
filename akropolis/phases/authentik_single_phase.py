"""authentik (single) — the identity provider on ONE node: PostgreSQL as a
plain container instead of Patroni, no leader/rolling distinction. There is
only one node and no bridge-network sharing between containers (see below),
so there is nothing for the HA phase's bootstrap/rolling split to protect
against — server and worker start concurrently, each depending only on
postgresql being healthy, and coordinate migrations via Authentik's own
internal database lock ("waiting to acquire database lock" in its own
logs) rather than anything akropolis has to choreograph.

Ordinary bridge networking, NOT network_mode: host — matching the official
reference compose at docs.goauthentik.io/compose.yml. An earlier version of
this phase used network_mode: host (inherited from the HA cluster's compose
without the reason — HAProxy routing — that exists for it there) and caused
two real bugs before a real run surfaced them: the worker inheriting and
squatting the server's HTTPS port, and the server itself needing
CAP_NET_BIND_SERVICE to bind a privileged port at all. Neither is possible
once each container has its own isolated network namespace: the server's
own internal port (9443) is never privileged, Docker's own port publish
(root/dockerd, not the containerized process) maps host 443 to it, and
server/worker can never see each other's ports to conflict over. See
NOTES.md for the full story.

Deliberately reuses the exact same on-disk path as the HA phase
(/opt/authentik), so the health-gate, log-dump, and branding helpers from
authentik_phase.py apply unchanged (container names — authentik-server-1 /
authentik-worker-1 — come from the compose *project* directory name, which
is the same on both topologies).

TODO(cleanup): _email()/_branding_volumes()/_acfg() below are near-identical
copies of the same methods on AuthentikPhase (HA). Left duplicated rather
than restructuring that already-deployed file in this patch — the only
change made there is additive (an optional `port` parameter on
apply_brand/patch_brand, default 9443, so HA is unaffected — see
authentik_certs_phase.py, which reuses patch_brand at port 443). Unifying
_email/_branding_volumes/_acfg, and wiring the error-reporting prompt into
the HA phase (which still hardcodes AUTHENTIK_ERROR_REPORTING__ENABLED=false),
is a good follow-up once single-node has seen a real run.
"""

from __future__ import annotations

import getpass
import secrets as pysecrets
import shlex
from pathlib import Path

from ..remote import base_url, push_binary, push_file, render
from .authentik_phase import BRAND_FIELDS, apply_brand, dump_logs, wait_healthy
from .base import Phase, PhaseContext


class AuthentikSinglePhase(Phase):
    name = "authentik"

    # ------------------------------------------------------------------ util
    def _acfg(self, ctx: PhaseContext) -> dict:
        return ctx.cfg.raw.get("authentik") or {}

    def _secrets(self, ctx: PhaseContext) -> dict[str, str]:
        g = ctx.state.get_or_generate
        return {
            # same state-key names as the HA phase — different site, different
            # state file, no collision, and it keeps the two topologies
            # readable side by side in state files / any future tooling.
            "pg_password": g("authentik_db_password", lambda: pysecrets.token_urlsafe(32)),
            "secret_key": g("authentik_secret_key", lambda: pysecrets.token_urlsafe(60)),
            "bootstrap_password": g("authentik_bootstrap_password",
                                    lambda: pysecrets.token_urlsafe(18)),
            "bootstrap_token": g("authentik_bootstrap_token",
                                 lambda: pysecrets.token_urlsafe(45)),
        }

    # error_reporting: config-or-prompt-pinned, same pattern as monitor.ip and
    # the SMTP block below. Resolves the open guide-vs-code mismatch (the v21
    # guide says true, the HA template hardcodes false) by making it an
    # explicit, discoverable decision instead of a silent default either way.
    def _error_reporting(self, ctx: PhaseContext) -> bool:
        acfg = self._acfg(ctx)
        configured = acfg.get("error_reporting")
        if configured is not None:
            return bool(configured)

        def ask() -> str:
            v = input("Send crash/error reports to Authentik's maintainers "
                      "(AUTHENTIK_ERROR_REPORTING__ENABLED)? [y/N] ").strip().lower()
            return "true" if v in ("y", "yes") else "false"

        return ctx.state.get_or_generate("authentik_error_reporting", ask) == "true"

    # branding — identical mechanism to the HA phase (upload, then derive the
    # bind-mount from the same setting so the two halves can't drift apart).
    def _branding_volumes(self, ctx: PhaseContext) -> list[str]:
        b = self._acfg(ctx).get("branding") or {}
        vols: list[str] = []
        conn = ctx.fleet.conns[0]
        for key, (subdir, _field) in BRAND_FIELDS.items():
            src = str(b.get(key) or "").strip()
            if not src:
                continue
            local = Path(src).expanduser()
            if not local.exists():
                raise RuntimeError(f"authentik.branding.{key} does not exist: {local}")
            name = local.name
            remote = f"/opt/authentik/branding/{subdir}/{name}"
            changed = push_binary(conn, local, remote)
            ctx.record(conn.node.name, f"branding {key}", True,
                       f"{name} → {remote}" + ("" if changed else " (unchanged)"))
            vols.append(f"{remote}:/web/dist/assets/{subdir}/{name}")
        return vols

    # email/SMTP — identical resolution order to the HA phase: site config →
    # interactive prompt, pinned in state, password via hidden input, never
    # written to the config file.
    def _email(self, ctx: PhaseContext) -> dict | None:
        ecfg = self._acfg(ctx).get("email")
        if ecfg is not None and not ecfg.get("enabled", True):
            return None
        g = ctx.state.get_or_generate
        if ecfg is None:
            want = g("authentik_email_configure",
                     lambda: input("Configure SMTP email for Authentik "
                                   "(password resets, email stages)? [y/N] ").strip().lower())
            if want not in ("y", "yes"):
                return None
            ecfg = {}

        def ask(label: str, default: str = "") -> str:
            suffix = f" [{default}]" if default else ""
            while True:
                v = input(f"  SMTP {label}{suffix}: ").strip() or default
                if v or not default:
                    return v

        host = ecfg.get("host") or g("authentik_email_host", lambda: ask("host"))
        port = int(ecfg.get("port") or g("authentik_email_port", lambda: ask("port", "587")))
        username = ecfg.get("username")
        if username is None:
            username = g("authentik_email_username",
                         lambda: input("  SMTP username (Enter for none): ").strip())
        password = ""
        if username:
            password = (ecfg.get("password")
                        or g("authentik_email_password",
                             lambda: getpass.getpass("  SMTP password (hidden, pinned in state): ")))
        if "use_tls" in ecfg or "use_ssl" in ecfg:
            use_tls = bool(ecfg.get("use_tls", False))
            use_ssl = bool(ecfg.get("use_ssl", False))
        else:
            enc = g("authentik_email_encryption",
                    lambda: ask("encryption (tls/ssl/none)", "tls"))
            use_tls, use_ssl = enc == "tls", enc == "ssl"
        from_addr = ecfg.get("from") or g("authentik_email_from",
                                          lambda: ask("From address", f"noreply@{ctx.cfg.tls.hostname or 'example.org'}"))
        return {"host": host, "port": port, "username": username,
                "password": password, "use_tls": use_tls, "use_ssl": use_ssl,
                "timeout": int(ecfg.get("timeout", 10)), "from_addr": from_addr}

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        acfg = self._acfg(ctx)
        lines = [
            f"render /opt/authentik/.env (0600) + docker-compose.yml on {cfg.nodes[0].name} — "
            f"tag {cfg.authentik_tag}, PostgreSQL as a postgres:16-alpine container "
            "(no published port — reached via Docker's own DNS, service name "
            "'postgresql'; named volume `database`)",
            "server, worker: ordinary isolated containers (no network_mode: host, "
            "no AUTHENTIK_LISTEN__* overrides needed — nothing to conflict over), "
            "both depend only on postgresql being healthy, python3/urllib "
            "healthchecks. Docker publishes host 443 -> the server container's "
            "own 9443 (never a privileged port, so no cap_add either)",
            "AUTHENTIK_SECRET_KEY / postgres password / bootstrap admin password / "
            "bootstrap API token: generated once, pinned in state, never printed",
        ]
        if acfg.get("error_reporting") is not None:
            lines.append(f"error reporting: {acfg['error_reporting']} (from site config)")
        elif "authentik_error_reporting" in ctx.state.data["generated"]:
            lines.append("error reporting: previous interactive answer pinned in state will be reused")
        else:
            lines.append("error reporting: not in site config — you will be asked interactively "
                         "(answer pinned in state)")
        ecfg = acfg.get("email")
        if ecfg is not None and not ecfg.get("enabled", True):
            lines.append("SMTP email: disabled in site config — no AUTHENTIK_EMAIL__* block")
        elif ecfg is not None:
            lines.append(f"SMTP email: from site config (host {ecfg.get('host', '?')}) — "
                         "missing values (incl. password) prompted once and pinned in state")
        elif "authentik_email_configure" in ctx.state.data["generated"]:
            lines.append("SMTP email: previous interactive answers pinned in state will be reused")
        else:
            lines.append("SMTP email: not in site config — you will be asked interactively "
                         "(answers pinned in state, password via hidden prompt)")
        b = acfg.get("branding") or {}
        named = [k for k in ("logo", "background") if b.get(k)]
        if named:
            lines.append(f"branding: upload {', '.join(named)}, bind-mount over "
                         "/web/dist/assets/{icons,images}/, AND point the default brand "
                         "at /static/dist/assets/... via the API")
        lines.append("apply: docker compose up -d, then gate on server+worker healthy "
                     "(both start concurrently once postgresql is healthy; Authentik's "
                     "own internal database lock coordinates migrations between them — "
                     "no manual bootstrap choreography needed)")
        lines.append("verify: server+worker healthy, /-/health/ready/ 200, "
                     "API answers with the bootstrap token")
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        sec = self._secrets(ctx)
        acfg = self._acfg(ctx)
        error_reporting = self._error_reporting(ctx)  # may prompt
        email = self._email(ctx)                      # may prompt — both before any node is touched

        env = render("authentik-single-env.j2",
                     tag=cfg.authentik_tag,
                     pg_pass=sec["pg_password"],
                     secret_key=sec["secret_key"],
                     bootstrap_password=sec["bootstrap_password"],
                     bootstrap_token=sec["bootstrap_token"],
                     bootstrap_email=acfg.get("bootstrap", {}).get("email", ""),
                     error_reporting=error_reporting,
                     base_url=base_url(cfg),
                     email=email)
        # uploads happen before the compose file is rendered, so the mounts
        # in it always refer to files that are already on the node
        branding = self._branding_volumes(ctx)
        compose = render("authentik-single-compose.yml.j2",
                         extra_server_volumes=branding
                         + list(acfg.get("extra_server_volumes", []) or []),
                         extra_worker_volumes=list(acfg.get("extra_worker_volumes", []) or []))

        conn = ctx.fleet.conns[0]  # topology: single — config.py enforces exactly 1 node
        node = conn.node.name
        conn.run("mkdir -p /opt/authentik/data /opt/authentik/certs "
                 "/opt/authentik/custom-templates")
        c1 = push_file(conn, env, "/opt/authentik/.env", mode="0600")
        c2 = push_file(conn, compose, "/opt/authentik/docker-compose.yml")
        changed = c1 or c2
        ctx.record(node, "config rendered", True, "changed" if changed else "unchanged")

        # Fresh bootstrap: server and worker start concurrently once
        # postgresql is healthy (no depends_on between them — see module
        # docstring), each just waiting on Authentik's own internal database
        # lock if the other reaches migrations first. Nothing here for
        # compose's own dependency-health wait to time out on, unlike the
        # HA cluster's restore phase (see NOTES.md) — that trap was a
        # consequence of network_mode: host forcing an explicit worker-then-
        # server order, which single-node no longer has any reason to do.
        ctx.begin(node, "compose up",
                  "postgresql healthy, then server + worker concurrently; "
                  "image pull can take minutes")
        r = conn.run("cd /opt/authentik && docker compose up -d", timeout=1800)
        ctx.record(node, "starting", r.ok, r.err if not r.ok else "")
        good = r.ok and wait_healthy(ctx, conn, timeout=900,
                                     label="waiting for server+worker healthy")
        ctx.record(node, "healthy", good,
                   "" if good else "never reached healthy — docker compose logs on the node")
        if not good:
            dump_logs(ctx, conn, "worker")
            dump_logs(ctx, conn, "server")
            raise RuntimeError(f"{node} never became healthy")

        branding_cfg = acfg.get("branding") or {}
        if branding_cfg:
            apply_brand(ctx, conn, branding_cfg, sec["bootstrap_token"], port=443)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        conn = ctx.fleet.conns[0]
        node = conn.node.name
        good = wait_healthy(ctx, conn, timeout=60, label="verify: healthy gate")
        ctx.record(node, "verify: containers healthy", good, "")

        r = conn.run("curl -sk -o /dev/null -w '%{http_code}' "
                     "https://127.0.0.1:443/-/health/ready/")
        ready = r.out in ("200", "204")
        ctx.record(node, "verify: /-/health/ready/", ready, f"HTTP {r.out}")

        token = ctx.state.data["generated"].get("authentik_bootstrap_token", "")
        r2 = conn.run(
            f"curl -sk -H {shlex.quote('Authorization: Bearer ' + token)} "
            f"-o /dev/null -w '%{{http_code}}' "
            f"https://127.0.0.1:443/api/v3/admin/version/")
        api = r2.out == "200"
        ctx.record(node, "verify: API with bootstrap token", api, f"HTTP {r2.out}")
        return good and ready and api
