"""haproxy — guide Step 5: per-node PostgreSQL connection router.

One HAProxy per node; Authentik will talk to 127.0.0.1:5000 (primary) and
:5001 (replicas). Each HAProxy discovers the Patroni leader itself via
`GET /primary` on 8008 — akropolis configures the mechanism, never the answer.

Reload semantics follow the guide's learnings:
  - haproxy.cfg changed, container running  → `docker kill --signal=HUP` (live reload;
    the base64 push truncates in place, so the bind-mount inode is preserved)
  - docker-compose.yml changed              → `down && up -d` (never `restart`)

The stats password is generated once and pinned in state.

Verify is two-layered: the stats CSV must show exactly 1 UP server in
pg_primary_backend and 2 UP in pg_replica_backend on every node, and a real
`psql` through 127.0.0.1:5000 as the authentik user must answer SELECT 1 —
proving the whole chain HAProxy → Patroni leader → pg_hba → credentials.
"""

from __future__ import annotations

import shlex
from importlib import resources

from ..remote import gen_password, push_file, render, wait_for
from .base import Phase, PhaseContext

CSV_CMD = "curl -s -u admin:{pw} 'http://127.0.0.1:9000/stats;csv'"


class HAProxyPhase(Phase):
    name = "haproxy"

    def _stats_password(self, ctx: PhaseContext) -> str:
        return ctx.state.get_or_generate("haproxy_stats_password", gen_password)

    def plan(self, ctx: PhaseContext) -> list[str]:
        return [
            "create /opt/haproxy/config on all nodes",
            "render haproxy.cfg (identical on all nodes): stats on :9000 (password pinned "
            "in state), :5000 → Patroni /primary, :5001 → /replica, "
            "on-marked-down shutdown-sessions, 1h client/server timeouts",
            "write docker-compose.yml (haproxy:3.0-alpine, network_mode: host)",
            "start/reload per guide semantics: HUP for cfg-only changes, "
            "down && up for compose changes",
            "verify: stats CSV shows 1 UP primary + 2 UP replicas on every node; "
            "end-to-end `SELECT 1` as authentik via 127.0.0.1:5000",
        ]

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        stats_pw = self._stats_password(ctx)
        compose = resources.files("akropolis.templates") \
            .joinpath("haproxy-compose.yml").read_text()
        hacfg = render("haproxy.cfg.j2", nodes=cfg.nodes, stats_password=stats_pw)

        for conn in ctx.fleet:
            node = conn.node.name
            r = conn.run("mkdir -p /opt/haproxy/config")
            ctx.record(node, "directories", r.ok, r.err if not r.ok else "")

            cfg_changed = push_file(conn, hacfg, "/opt/haproxy/config/haproxy.cfg",
                                    mode="0600")
            compose_changed = push_file(conn, compose, "/opt/haproxy/docker-compose.yml")
            running = conn.run("docker ps --format '{{.Names}}' | grep -qx haproxy").ok

            if not running or compose_changed:
                r = conn.run("cd /opt/haproxy && docker compose down 2>/dev/null; "
                             "cd /opt/haproxy && docker compose up -d", timeout=300)
                ctx.record(node, "compose up", r.ok, r.err if not r.ok else "")
            elif cfg_changed:
                r = conn.run("docker kill --signal=HUP haproxy")
                ctx.record(node, "live reload (HUP)", r.ok, r.err if not r.ok else "")
            else:
                ctx.record(node, "unchanged", True, "config identical, container running")

    def verify(self, ctx: PhaseContext) -> bool:
        stats_pw = self._stats_password(ctx)
        csv_cmd = CSV_CMD.format(pw=shlex.quote(stats_pw))
        ok = True

        for conn in ctx.fleet:
            node = conn.node.name
            # stats page must answer, then backends must converge (health checks
            # need a few inter=5s cycles after a fresh start)
            up = wait_for(conn, f"{csv_cmd} | grep -q pg_primary_backend",
                          timeout=60, interval=5)
            if not up:
                ctx.record(node, "stats page answering", False, "no CSV within 60s")
                ok = False
                continue

            converged = wait_for(
                conn,
                f"{csv_cmd} | awk -F, "
                "'$1==\"pg_primary_backend\" && $2!~/BACKEND|FRONTEND/ && $18==\"UP\" {p++} "
                "$1==\"pg_replica_backend\" && $2!~/BACKEND|FRONTEND/ && $18==\"UP\" {r++} "
                "END {exit !(p==1 && r==2)}'",
                timeout=90, interval=5,
            )
            ctx.record(node, "backends: 1 UP primary + 2 UP replicas", converged,
                       "" if converged else "did not converge within 90s")
            ok = ok and converged

        # end-to-end: real connection through the local HAProxy on node-1
        first = ctx.fleet.conns[0]
        db_pw = ctx.state.data["generated"].get("authentik_db_password")
        if db_pw:
            r = first.run(
                f"PGPASSWORD={shlex.quote(db_pw)} psql -h 127.0.0.1 -p 5000 "
                f"-U authentik -d authentik -tAc 'SELECT 1'", timeout=30)
            e2e = r.ok and r.out.strip() == "1"
            ctx.record("cluster", "SELECT 1 via 127.0.0.1:5000 as authentik", e2e,
                       r.err if not e2e else "full chain works")
            ok = ok and e2e
        else:
            ctx.record("cluster", "end-to-end psql check", False,
                       "authentik_db_password missing from state — run patroni phase first",
                       warn=True)
        return ok
