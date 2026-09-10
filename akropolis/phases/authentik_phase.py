"""authentik — guide Step 7: the identity provider itself, three nodes over one
PostgreSQL, everything else already in place underneath it.

Two distinct execution paths, chosen by looking at what's actually running:

  BOOTSTRAP (first deployment): render everywhere, start the bootstrap leader
  ALONE and gate on both containers reaching `healthy` — that window covers the
  image pull and the full database migration run. Only then start the other two
  nodes, which find a migrated schema and come up clean. This avoids N nodes
  racing migrations on one database.

  ROLLING (config/tag change on a running cluster): apply node-3 → node-2 →
  node-1 (reverse fleet order — the change order used in production), one node
  at a time, `down && up -d` (never `restart`), gating on `healthy` before
  moving on. A node that fails its gate stops the phase with two nodes still
  serving.

The compose file mirrors the production one verbatim (including the
python3/urllib healthchecks that replaced curl when the 2026.5.6 image dropped
it, worker as root with the docker socket for outpost management, and
`depends_on: worker: condition: service_healthy` for the dual-port-bind race).
Site-specific mounts (branding, locale chunks) are NOT hardcoded — use
`authentik.extra_server_volumes` / `authentik.extra_worker_volumes` in the
site config.

Generated once and pinned in state: AUTHENTIK_SECRET_KEY (identical on all
nodes — non-negotiable), the akadmin bootstrap password, and the bootstrap API
token that the handoff phase gives to the monitor.
"""

from __future__ import annotations

import getpass
import json
import secrets as pysecrets
import shlex
from pathlib import Path

from ..remote import base_url, push_binary, push_file, render, wait_for
from .base import Phase, PhaseContext, console

HEALTHY = ("docker inspect -f '{{.State.Health.Status}}' authentik-server-1 "
           "authentik-worker-1 2>/dev/null | sort -u")

# EXACT comparison, never a substring or grep match: "healthy" is a substring
# of "unhealthy", so `expect="healthy"` and `grep healthy` both report a
# FAILED container as good. Shell string equality against the deduplicated
# status list is the only form that cannot lie: it is true only when every
# named container reports exactly healthy.
ALL_HEALTHY = f'[ "$({HEALTHY})" = healthy ]'


def previous_tag(ctx: PhaseContext, phase_name: str) -> str | None:
    """The authentik.tag pinned by this phase's last successful apply, or
    None on a fresh site (nothing to compare a version jump against yet)."""
    return ctx.state.data["phases"].get(phase_name, {}).get("authentik_tag")


def tag_change_warning(ctx: PhaseContext, phase_name: str, new_tag: str) -> str | None:
    """A plan() line warning about an authentik.tag change, or None when the
    tag is unchanged (or this is the first-ever apply).

    Authentik runs its DB migrations on container start with no rollback, and
    its own release notes sometimes require going through an intermediate
    version rather than jumping straight to the target — akropolis has no way
    to know that, so it just makes sure the operator sees the change and is
    reminded to check before confirming.
    """
    old = previous_tag(ctx, phase_name)
    if old and old != new_tag:
        return (f"[yellow]authentik.tag changed: {old} -> {new_tag}[/yellow] — triggers a "
                "rolling upgrade with DB migrations on container start (no rollback). "
                "Back up first (restore.sql_file / pg_dump) and check Authentik's release "
                f"notes for the {old} -> {new_tag} path — some jumps require an "
                "intermediate version.")
    return None


def pin_applied_tag(ctx: PhaseContext, phase_name: str, tag: str) -> None:
    ctx.state.mark_phase(phase_name, "applying", {"authentik_tag": tag})


def one_healthy_cmd(container: str) -> str:
    return ('[ "$(docker inspect -f \'{{.State.Health.Status}}\' '
            f'{container} 2>/dev/null)" = healthy ]')


def wait_healthy(ctx, conn, timeout: float, label: str = "waiting for healthy") -> bool:
    """Gate until BOTH containers report exactly 'healthy', with a live line.

    Module-level so the restore phase reuses the identical gate.
    """
    node = conn.node.name
    ctx.begin(node, label, f"0s / {int(timeout)}s")
    return wait_for(conn, ALL_HEALTHY, timeout=timeout, interval=10,
                    tick=lambda el: ctx.tick(f"{int(el)}s / {int(timeout)}s"))


# Mounting a file changes nothing on its own: Authentik shows the stock logo
# until the BRAND RECORD points at the asset. On a fresh cluster that makes a
# correct branding setup look broken (the file is there, the login page is
# unchanged) — so akropolis sets the brand too, closing the loop.
#
# The container path /web/dist/assets/<sub>/<name> is served at
# /static/dist/assets/<sub>/<name>, and brand fields only accept the /static
# prefix for absolute paths (goauthentik #19557).
BRAND_FIELDS = {
    "logo": ("icons", "branding_logo"),
    "favicon": ("icons", "branding_favicon"),
    "background": ("images", "branding_default_flow_background"),
}


def find_default_brand_uuid(ctx, conn, token: str, port: int = 9443) -> str:
    """Look up the UUID of the default brand. Empty string if not found."""
    auth = shlex.quote("Authorization: Bearer " + token)
    base = f"https://127.0.0.1:{port}/api/v3/core/brands/"
    r = conn.run(f"curl -sk -H {auth} {shlex.quote(base + '?ordering=domain')} "
                 "| jq -r '.results[] | select(.default==true) | .brand_uuid' | head -1",
                 timeout=60)
    return r.out.strip() if r.ok else ""


def patch_brand(ctx, conn, token: str, fields: dict, port: int = 9443) -> str:
    """PATCH the default brand with `fields`. Returns the HTTP status code
    (as a string; "" if the default brand lookup itself failed)."""
    auth = shlex.quote("Authorization: Bearer " + token)
    base = f"https://127.0.0.1:{port}/api/v3/core/brands/"
    uuid = find_default_brand_uuid(ctx, conn, token, port=port)
    if not uuid:
        return ""
    payload = json.dumps(fields)
    r = conn.run(f"curl -sk -X PATCH -H {auth} -H 'Content-Type: application/json' "
                 f"-d {shlex.quote(payload)} -o /dev/null -w '%{{http_code}}' "
                 f"{shlex.quote(base + uuid + '/')}", timeout=60)
    return r.out.strip()


def apply_brand(ctx, conn, branding: dict, token: str, port: int = 9443) -> bool:
    """Point the DEFAULT brand at the mounted assets. Idempotent."""
    fields = {}
    for key, (subdir, field) in BRAND_FIELDS.items():
        src = str(branding.get(key) or "").strip()
        if src:
            fields[field] = f"/static/dist/assets/{subdir}/{Path(src).name}"
    if not fields:
        return True

    ctx.begin(conn.node.name, "pointing default brand at the mounted assets")
    if not find_default_brand_uuid(ctx, conn, token, port=port):
        ctx.record(conn.node.name, "default brand lookup", False,
                   "could not find the default brand — set the logo manually in "
                   "System > Brands", warn=True)
        return False
    code = patch_brand(ctx, conn, token, fields, port=port)
    ok = code == "200"
    ctx.record(conn.node.name, "default brand updated", ok,
               ", ".join(f"{k}={v}" for k, v in fields.items()) if ok
               else f"HTTP {code} — set it manually in System > Brands", warn=not ok)
    return ok


def wait_one_healthy(ctx, conn, container: str, timeout: float,
                     label: str = "waiting for healthy") -> bool:
    """Gate on ONE container's health, with a live line.

    Needed when a service is started alone (`up -d --no-deps`): the pair gate
    above would never pass, because the other container is deliberately not
    running yet.
    """
    ctx.begin(conn.node.name, label, f"0s / {int(timeout)}s")
    return wait_for(conn, one_healthy_cmd(container), timeout=timeout, interval=10,
                    tick=lambda el: ctx.tick(f"{int(el)}s / {int(timeout)}s"))


def token_alive(conn, token: str, port: int = 9443) -> bool:
    """Does the API accept `token` right now? Used to detect a bootstrap
    token that has gone stale against whatever database is actually live
    (typically: the database was replaced out from under it — a restore,
    in or out of band — while the pinned value in state didn't change)."""
    r = conn.run(f"curl -sk -H {shlex.quote('Authorization: Bearer ' + token)} "
                 "-o /dev/null -w '%{http_code}' "
                 f"https://127.0.0.1:{port}/api/v3/admin/version/", timeout=30)
    return r.out.strip() == "200"


def remint_bootstrap_token(ctx, conn, token: str, port: int = 9443) -> bool:
    """Re-mint the pinned bootstrap token into whatever database is
    currently live, through the ORM inside the worker container — the one
    route that does not itself require a working API token.

    AUTHENTIK_BOOTSTRAP_TOKEN in .env does not fix this on its own: authentik
    only applies it when it creates akadmin, and a database that already has
    an akadmin (any restore, in or out of band) makes that bootstrap step a
    no-op. This re-establishes the SAME token value state already has
    pinned, so nothing else downstream (the monitor config, this phase's own
    branding step) needs to change.

    Best-effort: a failure here is reported as a warning, not raised —
    callers decide whether a dead token is fatal for them.
    """
    node = conn.node.name
    py = (
        "try:\n"
        "    from authentik.core.models import Token, TokenIntents, User\n"
        "except Exception as e:\n"
        "    print('ERR import %s' % e); raise SystemExit(1)\n"
        f"KEY = {token!r}\n"
        "IDENT = 'akropolis-bootstrap'\n"
        "u = User.objects.filter(username='akadmin').first() or \\\n"
        "    User.objects.filter(is_active=True).order_by('pk').first()\n"
        "if u is None:\n"
        "    print('ERR no user in database'); raise SystemExit(1)\n"
        "Token.objects.filter(key=KEY).exclude(identifier=IDENT).delete()\n"
        "Token.objects.update_or_create(identifier=IDENT, defaults={\n"
        "    'user': u, 'intent': TokenIntents.INTENT_API, 'key': KEY,\n"
        "    'expiring': False, 'description': 'akropolis provisioning token'})\n"
        "print('OK %s' % u.username)\n"
    )
    ctx.begin(node, "re-minting bootstrap token", "into the live database")
    r = conn.run("cd /opt/authentik && docker compose exec -T worker "
                 f"ak shell -c {shlex.quote(py)}", timeout=180)
    if not (r.ok and "OK " in r.out):
        ctx.record(node, "bootstrap token re-minted", False,
                   (r.out or r.err).splitlines()[-1] if (r.out or r.err) else "ak shell failed",
                   warn=True)
        return False
    owner = r.out.rsplit("OK ", 1)[-1].strip()
    ctx.record(node, "bootstrap token re-minted", True,
               f"attached to user {owner!r} — the token in state and in the "
               "monitor config keeps working")
    return token_alive(conn, token, port=port)


def dump_logs(ctx, conn, service: str, lines: int = 40) -> None:
    """Print the tail of a container's log when a gate fails.

    An expired health gate tells the operator nothing on its own; the reason
    is always in the log, and sending someone to SSH for it is a wasted
    round-trip when the connection is already open.
    """
    r = conn.run(f"cd /opt/authentik && docker compose logs --no-color "
                 f"--tail {lines} {service} 2>&1", timeout=120)
    if r.out:
        console.rule(f"[red]{conn.node.name}: last {lines} lines of {service} log[/red]")
        console.print(r.out, markup=False, highlight=False)
        console.rule()


class AuthentikPhase(Phase):
    name = "authentik"

    # ------------------------------------------------------------------ util
    def _secrets(self, ctx: PhaseContext) -> dict[str, str]:
        g = ctx.state.get_or_generate
        return {
            "secret_key": g("authentik_secret_key",
                            lambda: pysecrets.token_urlsafe(60)),
            "bootstrap_password": g("authentik_bootstrap_password",
                                    lambda: pysecrets.token_urlsafe(18)),
            "bootstrap_token": g("authentik_bootstrap_token",
                                 lambda: pysecrets.token_urlsafe(45)),
        }

    # ----------------------------------------------------------- branding
    # Custom logo/background are two moving parts, not one: the files must
    # exist ON EVERY NODE, and the container needs a bind-mount over the
    # asset path it actually serves. Doing only the second (an
    # extra_server_volumes entry) mounts a directory over a missing file and
    # breaks the asset. So akropolis uploads from the workstation and derives
    # the mounts, keeping the two in sync by construction.
    #
    # Asset paths mirror the production compose:
    #   /web/dist/assets/icons/<name>   ← logo
    #   /web/dist/assets/images/<name>  ← background
    def _branding_volumes(self, ctx: PhaseContext) -> list[str]:
        b = self._acfg(ctx).get("branding") or {}
        vols: list[str] = []
        for key, (subdir, _field) in BRAND_FIELDS.items():
            src = str(b.get(key) or "").strip()
            if not src:
                continue
            local = Path(src).expanduser()
            if not local.exists():
                raise RuntimeError(f"authentik.branding.{key} does not exist: {local}")
            name = local.name
            remote = f"/opt/authentik/branding/{subdir}/{name}"
            for conn in ctx.fleet:
                changed = push_binary(conn, local, remote)
                ctx.record(conn.node.name, f"branding {key}", True,
                           f"{name} → {remote}" + ("" if changed else " (unchanged)"))
            vols.append(f"{remote}:/web/dist/assets/{subdir}/{name}")
        return vols

    def _acfg(self, ctx: PhaseContext) -> dict:
        return ctx.cfg.raw.get("authentik") or {}

    # ------------------------------------------------------------- email/SMTP
    # The production .env carries an AUTHENTIK_EMAIL__* block (guide 7.x) —
    # without it password recovery and email stages silently can't send.
    # Resolution order per value: site config `authentik.email` → interactive
    # prompt at apply time, answer pinned in state (so a --replay never
    # re-asks and renders identically). The SMTP password never lives in the
    # config file: it is prompted with getpass and pinned in state (0600).
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
        leader = cfg.bootstrap_leader.name
        running = self._all_running(ctx)
        lines = [
            f"render /opt/authentik/.env (0600) + docker-compose.yml on all nodes — "
            f"tag {cfg.authentik_tag}, PG via 127.0.0.1:5000, listen ports 9080/9443/9300 "
            "(off HAProxy's 9000), python3/urllib healthchecks (curl absent from image)",
            "AUTHENTIK_SECRET_KEY / bootstrap admin password / bootstrap API token: "
            "generated once, pinned in state, identical everywhere, never printed",
        ]
        warning = tag_change_warning(ctx, self.name, cfg.authentik_tag)
        if warning:
            lines.append(warning)
        ecfg = self._acfg(ctx).get("email")
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
        if running:
            lines.append("cluster already running → ROLLING apply, node-3 → node-2 → node-1, "
                         "down && up -d, health-gated between nodes")
        else:
            lines.append(f"BOOTSTRAP: start {leader} ALONE, gate on healthy "
                         "(covers image pull + database migrations, up to 15 min), "
                         "then the other nodes one at a time")
        b = self._acfg(ctx).get("branding") or {}
        named = [k for k in ("logo", "background") if b.get(k)]
        if named:
            lines.append(f"branding: upload {', '.join(named)} to every node under "
                         "/opt/authentik/branding/, bind-mount over "
                         "/web/dist/assets/{icons,images}/, AND point the default "
                         "brand at /static/dist/assets/... via the API (mounting "
                         "alone leaves the stock logo showing)")
        lines.append("verify: server+worker healthy on all nodes, /-/health/ready/ 200 "
                     "per node, API answers with the bootstrap token")
        return lines

    def _all_running(self, ctx: PhaseContext) -> bool:
        try:
            return all(
                conn.run("docker ps --filter status=running --format '{{.Names}}' | grep -q authentik-server").ok
                for conn in ctx.fleet)
        except Exception:  # noqa: BLE001
            return False

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        sec = self._secrets(ctx)
        acfg = self._acfg(ctx)
        email = self._email(ctx)  # may prompt — resolved before any node is touched
        rolling = self._all_running(ctx)

        env = render("authentik-env.j2",
                     tag=cfg.authentik_tag,
                     db_password=ctx.state.data["generated"]["authentik_db_password"],
                     secret_key=sec["secret_key"],
                     bootstrap_password=sec["bootstrap_password"],
                     bootstrap_token=sec["bootstrap_token"],
                     bootstrap_email=acfg.get("bootstrap", {}).get("email", ""),
                     base_url=base_url(cfg),
                     email=email)
        # uploads happen before the compose file is rendered, so the mounts
        # in it always refer to files that are already on the node
        branding = self._branding_volumes(ctx)
        compose = render("authentik-compose.yml.j2",
                         extra_server_volumes=branding
                         + list(acfg.get("extra_server_volumes", []) or []),
                         extra_worker_volumes=list(acfg.get("extra_worker_volumes", []) or []))

        changed: dict[str, bool] = {}
        for conn in ctx.fleet:
            node = conn.node.name
            conn.run("mkdir -p /opt/authentik/data /opt/authentik/certs "
                     "/opt/authentik/custom-templates")
            c1 = push_file(conn, env, "/opt/authentik/.env", mode="0600")
            c2 = push_file(conn, compose, "/opt/authentik/docker-compose.yml")
            changed[node] = c1 or c2
            ctx.record(node, "config rendered", True,
                       "changed" if changed[node] else "unchanged")

        if rolling:
            # reverse fleet order: node-3 → node-2 → node-1
            for conn in reversed(ctx.fleet.conns):
                node = conn.node.name
                if not changed[node]:
                    ctx.record(node, "rolling: skipped", True, "config unchanged")
                    continue
                ctx.begin(node, "rolling: down && up")
                r = conn.run("cd /opt/authentik && docker compose down && "
                             "docker compose up -d", timeout=1200)
                ctx.record(node, "rolling: down && up", r.ok, r.err if not r.ok else "")
                good = r.ok and wait_healthy(ctx, conn, timeout=900,
                                             label="rolling: waiting for healthy")
                ctx.record(node, "rolling: healthy", good,
                           "" if good else "containers never reached healthy")
                if not good:
                    dump_logs(ctx, conn, "server")
                    raise RuntimeError(f"{node} unhealthy after rolling update — "
                                       "stopping with the remaining nodes still serving")
        else:
            leader = next(c for c in ctx.fleet if c.node.bootstrap_leader)
            others = [c for c in ctx.fleet if not c.node.bootstrap_leader]

            ctx.begin(leader.node.name, "bootstrap: compose up", "image pull can take minutes")
            r = leader.run("cd /opt/authentik && docker compose up -d", timeout=1800)
            ctx.record(leader.node.name, "bootstrap node starting", r.ok,
                       r.err if not r.ok else "pull + migrations in progress")
            good = r.ok and wait_healthy(ctx, leader, timeout=900,
                                         label="bootstrap: pull + database migrations")
            ctx.record(leader.node.name, "bootstrap node healthy (migrations done)",
                       good, "" if good else
                       "never reached healthy — docker compose logs on the node")
            if not good:
                dump_logs(ctx, leader, "worker")
                dump_logs(ctx, leader, "server")
                raise RuntimeError("bootstrap node never became healthy — stopping "
                                   "before starting the other nodes")

            for conn in others:
                node = conn.node.name
                ctx.begin(node, "compose up", "schema already migrated")
                r = conn.run("cd /opt/authentik && docker compose up -d", timeout=1800)
                ctx.record(node, "starting", r.ok, r.err if not r.ok else "")
                good = r.ok and wait_healthy(ctx, conn, timeout=900)
                ctx.record(node, "healthy", good,
                           "" if good else "never reached healthy")
                if not good:
                    dump_logs(ctx, conn, "server")
                    raise RuntimeError(f"{node} never became healthy")

        # the brand record is set last: it needs a live API, and it is the half
        # that actually makes the uploaded assets visible
        branding = acfg.get("branding") or {}
        if branding:
            apply_brand(ctx, ctx.fleet.conns[0], branding, sec["bootstrap_token"])

        pin_applied_tag(ctx, self.name, cfg.authentik_tag)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        for conn in ctx.fleet:
            node = conn.node.name
            good = wait_healthy(ctx, conn, timeout=60, label="verify: healthy gate")
            ctx.record(node, "verify: containers healthy", good, "")
            ok = ok and good
            r = conn.run("curl -sk -o /dev/null -w '%{http_code}' "
                         "https://127.0.0.1:9443/-/health/ready/")
            ready = r.out == "200" or r.out == "204"
            ctx.record(node, "verify: /-/health/ready/", ready, f"HTTP {r.out}")
            ok = ok and ready

        # API answers with the bootstrap token — this is the token the monitor
        # will use, so proving it now saves a debugging session later.
        #
        # A live cluster whose database was replaced out from under it (a
        # restore run outside the `restore` phase — direct pg_restore,
        # snapshot rollback, etc.) leaves state pinned to a token the new
        # database has never heard of: HTTP 403, not 401 (the header parses
        # fine, the key just doesn't match any row). That is recoverable
        # without editing state by hand — re-mint the SAME pinned value into
        # whatever database is live now — so try that once before giving up.
        token = ctx.state.data["generated"].get("authentik_bootstrap_token", "")
        conn0 = ctx.fleet.conns[0]
        api = token_alive(conn0, token)
        if not api:
            ctx.record("cluster", "verify: API with bootstrap token", False,
                       "token rejected — attempting to re-mint it into the live "
                       "database before failing", warn=True)
            api = remint_bootstrap_token(ctx, conn0, token)
        ctx.record("cluster", "verify: API with bootstrap token", api,
                   "" if api else "token rejected and re-mint did not recover it — "
                   "create one by hand (akadmin > Directory > Tokens, admin scope) "
                   "and put it in the monitor config, or fix `ak shell` access "
                   "and --replay authentik")
        return ok and api
