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
UoP-specific mounts (branding, locale chunks) are NOT hardcoded — use
`authentik.extra_server_volumes` / `authentik.extra_worker_volumes` in the
site config.

Generated once and pinned in state: AUTHENTIK_SECRET_KEY (identical on all
nodes — non-negotiable), the akadmin bootstrap password, and the bootstrap API
token that the handoff phase gives to the monitor.
"""

from __future__ import annotations

import getpass
import secrets as pysecrets
import shlex

from ..remote import push_file, render, wait_for
from .base import Phase, PhaseContext, console

HEALTHY = ("docker inspect -f '{{.State.Health.Status}}' authentik-server-1 "
           "authentik-worker-1 2>/dev/null | sort -u")


def wait_healthy(ctx, conn, timeout: float, label: str = "waiting for healthy") -> bool:
    """Gate until BOTH containers report exactly 'healthy', with a live line.

    Module-level so the restore phase reuses the identical gate.
    """
    node = conn.node.name
    ctx.begin(node, label, f"0s / {int(timeout)}s")
    ok = wait_for(conn, f"{HEALTHY} | grep -v healthy | wc -l",
                  expect="0", timeout=timeout, interval=10,
                  tick=lambda el: ctx.tick(f"{int(el)}s / {int(timeout)}s"))
    return ok and conn.run(f"{HEALTHY}").out.strip() == "healthy"


def wait_one_healthy(ctx, conn, container: str, timeout: float,
                     label: str = "waiting for healthy") -> bool:
    """Gate on ONE container's health, with a live line.

    Needed when a service is started alone (`up -d --no-deps`): the pair gate
    above would never pass, because the other container is deliberately not
    running yet.
    """
    ctx.begin(conn.node.name, label, f"0s / {int(timeout)}s")
    return wait_for(
        conn,
        "docker inspect -f '{{.State.Health.Status}}' " + container + " 2>/dev/null",
        expect="healthy", timeout=timeout, interval=10,
        tick=lambda el: ctx.tick(f"{int(el)}s / {int(timeout)}s"))


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
                     email=email)
        compose = render("authentik-compose.yml.j2",
                         extra_server_volumes=list(acfg.get("extra_server_volumes", []) or []),
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
        # will use, so proving it now saves a debugging session later
        token = ctx.state.data["generated"].get("authentik_bootstrap_token", "")
        r = ctx.fleet.conns[0].run(
            f"curl -sk -H {shlex.quote('Authorization: Bearer ' + token)} "
            f"-o /dev/null -w '%{{http_code}}' "
            f"https://127.0.0.1:9443/api/v3/admin/version/")
        api = r.out == "200"
        ctx.record("cluster", "verify: API with bootstrap token", api, f"HTTP {r.out}")
        return ok and api
