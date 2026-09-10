"""authentik lifecycle — operational shutdown/start of the authentik backends
on an already-provisioned site, for maintenance that only needs the identity
provider itself paused (e.g. an out-of-band database operation) without
tearing down the surrounding cluster.

Deliberately narrow, and topology-aware in what "the backends" means:

  ha     — the server+worker pair on each of the 3 nodes. etcd, Patroni/
           PostgreSQL, HAProxy, nginx, and keepalived keep running — the VIP
           stays up and nginx serves its maintenance page (see
           nginx_keepalived_phase.py) for the site until `start` brings
           authentik back. Compose command runs unscoped
           (`docker compose stop`/`up -d` with no service names) because the
           HA compose project contains only server+worker — see
           authentik-compose.yml.j2.
  single — the server+worker pair on the one node, service names given
           explicitly (`docker compose stop/up -d server worker`) so the
           postgresql container in that same compose project — see
           authentik-single-compose.yml.j2 — is deliberately left running.
           Stopping the database isn't "pausing the app", and single-node
           has no separate DCS to protect it the way Patroni does in ha.

`shutdown` uses `docker compose stop`, which (Compose V2) stops services in
REVERSE dependency order — on ha, server (which depends_on worker) stops
before worker, so requests stop being accepted before the background
processor that might still be mid-task; on single, server and worker have no
depends_on between each other (see authentik_single_phase.py) and stop
concurrently. `start` uses `docker compose up -d`, which on ha brings worker
up first and gates server on `depends_on: worker: condition: service_healthy`
automatically — the same ordering the bootstrap/rolling paths in
authentik_phase.py rely on; on single, server and worker come up concurrently
once postgresql (left running throughout) reports healthy.

`start` refuses to run unless `shutdown` last completed gracefully (tracked
via the `authentik-shutdown` phase status in site state) — starting an
already-running stack via this path isn't meaningful, and a stale/partial
shutdown should be reconciled by hand before the operator asks akropolis to
bring things back up. A successful `start` clears that flag again, so a
second `start` without an intervening `shutdown` is refused rather than
silently re-running `docker compose up -d` on a live stack.
"""

from __future__ import annotations

from .authentik_phase import dump_logs, wait_healthy
from .base import Phase, PhaseContext

# authentik-server-1 stops before authentik-worker-1 under `docker compose
# stop` on ha (reverse dependency order) — checking both together is still
# the correct "is anything up" probe for the idempotent no-op case, on
# either topology (container names come from the compose *project*
# directory, /opt/authentik, which is identical on both).
RUNNING = ("docker ps --filter status=running --format '{{.Names}}' "
           "| grep -qE '^authentik-(server|worker)-1$'")


def _scope(ctx: PhaseContext) -> str:
    """Compose service-name suffix: unscoped on ha (the project has nothing
    else in it), explicit on single (excludes the postgresql service, which
    lives in the same compose project but must stay running)."""
    return "" if ctx.cfg.topology == "ha" else " server worker"


def _ready_port(ctx: PhaseContext) -> int:
    """Port the server's /-/health/ready/ answers on, from the HOST side.

    ha runs network_mode: host, so the container's own 9443 is the host's
    9443. single has ordinary bridge networking with only 443 published to
    the host (443 -> container 9443) — 9443 itself is not reachable there.
    """
    return 9443 if ctx.cfg.topology == "ha" else 443


class AuthentikShutdownPhase(Phase):
    name = "authentik-shutdown"

    def plan(self, ctx: PhaseContext) -> list[str]:
        ha = ctx.cfg.topology == "ha"
        n = len(ctx.fleet.conns)
        lines = [
            f"gracefully stop authentik server+worker on "
            + (f"all {n} nodes" if ha else "the node") +
            " that are currently running (docker compose stop, 60s grace — "
            "lets in-flight requests and worker tasks finish; a no-op where "
            "already stopped)",
        ]
        if ha:
            lines.append("etcd, Patroni/PostgreSQL, HAProxy, nginx, and keepalived "
                         "are left running untouched — only the authentik "
                         "application containers stop")
            lines.append("the VIP stays up; nginx serves its maintenance page for "
                         "the site until `akropolis start` brings authentik back")
        else:
            lines.append("the postgresql container in the same compose project is "
                         "left running untouched — only server+worker stop")
        lines.append("on success, unlocks `akropolis start` for this site (start "
                     "refuses unless this command last completed gracefully)")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        scope = _scope(ctx)
        for conn in ctx.fleet:
            node = conn.node.name
            if not conn.run(RUNNING).ok:
                ctx.record(node, "authentik already stopped", True, "")
                continue
            ctx.begin(node, "stopping authentik", "docker compose stop, 60s grace")
            r = conn.run(f"cd /opt/authentik && docker compose stop --timeout 60{scope}",
                        timeout=180)
            ctx.record(node, "authentik stopped", r.ok, r.err if not r.ok else "")
            if not r.ok:
                raise RuntimeError(f"could not stop authentik on {node} — cluster "
                                   "left in a mixed state; inspect and retry")

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        for conn in ctx.fleet:
            node = conn.node.name
            stopped = not conn.run(RUNNING).ok
            ctx.record(node, "verify: authentik containers stopped", stopped, "")
            ok = ok and stopped
        return ok


class AuthentikStartPhase(Phase):
    name = "authentik-start"

    def _gate(self, ctx: PhaseContext) -> str:
        return ctx.state.phase_status("authentik-shutdown")

    def plan(self, ctx: PhaseContext) -> list[str]:
        status = self._gate(ctx)
        if status != "done":
            return [f"[red]refusing[/red]: no completed graceful shutdown on record "
                    f"(authentik-shutdown: {status}) — run `akropolis shutdown "
                    "<config>` first; apply will refuse"]
        ha = ctx.cfg.topology == "ha"
        n = len(ctx.fleet.conns)
        if ha:
            first = (f"start authentik worker+server on all {n} nodes, one node "
                     "at a time — docker compose up -d, health-gated before "
                     "moving to the next node (worker starts first within a "
                     "node; server's depends_on: worker: condition: "
                     "service_healthy handles that ordering automatically)")
        else:
            first = ("start authentik worker+server on the node — docker compose "
                     "up -d server worker (postgresql, left running by "
                     "`shutdown`, is untouched); server and worker come up "
                     "concurrently once postgresql reports healthy")
        return [first, "clears the graceful-shutdown flag on success, so a further "
                "`start` without an intervening `shutdown` is refused"]

    def apply(self, ctx: PhaseContext) -> None:
        status = self._gate(ctx)
        if status != "done":
            raise RuntimeError(
                f"no completed graceful shutdown on record (authentik-shutdown: "
                f"{status}) — run `akropolis shutdown <config>` first")

        scope = _scope(ctx)
        for conn in ctx.fleet:
            node = conn.node.name
            ctx.begin(node, "starting authentik", "docker compose up -d")
            r = conn.run(f"cd /opt/authentik && docker compose up -d{scope}", timeout=1800)
            ctx.record(node, "authentik starting", r.ok, r.err if not r.ok else "")
            if not r.ok:
                raise RuntimeError(f"could not start authentik on {node}")
            good = wait_healthy(ctx, conn, timeout=900)
            ctx.record(node, "healthy", good, "" if good else "never reached healthy")
            if not good:
                dump_logs(ctx, conn, "worker")
                dump_logs(ctx, conn, "server")
                raise RuntimeError(f"{node} never became healthy — stopping before "
                                   "starting the remaining nodes")

        ctx.state.mark_phase("authentik-shutdown", "started",
                             {"note": "cleared by a successful `akropolis start`"})

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        port = _ready_port(ctx)
        for conn in ctx.fleet:
            node = conn.node.name
            good = wait_healthy(ctx, conn, timeout=60, label="verify: healthy gate")
            ctx.record(node, "verify: containers healthy", good, "")
            ok = ok and good
            r = conn.run(f"curl -sk -o /dev/null -w '%{{http_code}}' "
                         f"https://127.0.0.1:{port}/-/health/ready/")
            ready = r.out in ("200", "204")
            ctx.record(node, "verify: /-/health/ready/", ready, f"HTTP {r.out}")
            ok = ok and ready
        return ok
