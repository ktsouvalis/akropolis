"""patroni — guide Step 3: PostgreSQL 16 + Patroni over etcd, with the strict
bootstrap order that makes or breaks the cluster.

The dangerous part is encoded explicitly:
  1. install everywhere, render configs everywhere — but start NOTHING yet
  2. start patroni on the bootstrap leader only; gate on REST /primary == 200
     (that is the "promoted self to leader" moment)
  3. start node-2, gate on role=replica state=streaming/running; then node-3
  4. create the authentik role + database on the leader, idempotently

Passwords (postgres / replicator / rewind / authentik DB) are generated once
and pinned in the state file. pg_hba includes the 127.0.0.1/32 replication +
rewind entries whose absence caused the Aug 2026 silent-streaming incident.
"""

from __future__ import annotations

import shlex

from ..remote import gen_password, push_file, render, wait_for
from .base import Phase, PhaseContext

APT = "DEBIAN_FRONTEND=noninteractive apt-get -y -qq"

PG_INSTALL = r"""
if ! test -x /usr/lib/postgresql/16/bin/postgres; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    | gpg --dearmor --yes -o /etc/apt/keyrings/postgresql.gpg
  echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get -qq update
  DEBIAN_FRONTEND=noninteractive apt-get -y -qq install postgresql-16 postgresql-client-16
fi
systemctl stop postgresql 2>/dev/null; systemctl disable postgresql 2>/dev/null; true
"""

PATRONI_INSTALL = r"""
test -x /opt/patroni/bin/patroni || {
  python3 -m venv /opt/patroni
  /opt/patroni/bin/pip install --quiet --upgrade pip
  /opt/patroni/bin/pip install --quiet 'patroni[etcd3]' psycopg2-binary
}
mkdir -p /etc/patroni /var/lib/postgresql/16/patroni
chown postgres:postgres /var/lib/postgresql/16/patroni
chmod 700 /var/lib/postgresql/16/patroni
"""

# role JSON from the local REST API; jq is in the baseline packages
ROLE_CMD = "curl -s http://127.0.0.1:8008/patroni | jq -r '.role + \" \" + .state'"


class PatroniPhase(Phase):
    name = "patroni"

    # ------------------------------------------------------------------ util
    def _secrets(self, ctx: PhaseContext) -> dict[str, str]:
        g = ctx.state.get_or_generate
        return {
            "postgres_password": g("pg_superuser_password", gen_password),
            "replicator_password": g("pg_replicator_password", gen_password),
            "rewind_password": g("pg_rewind_password", gen_password),
            "authentik_db_password": g("authentik_db_password", gen_password),
        }

    def _subnet(self, ctx: PhaseContext) -> str:
        ip = ctx.cfg.nodes[0].ip
        return ".".join(ip.split(".")[:3]) + ".0/24"

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        leader = cfg.bootstrap_leader
        others = [n.name for n in cfg.nodes if not n.bootstrap_leader]
        return [
            "install PostgreSQL 16 from pgdg on all nodes; stop+disable stock postgresql "
            "(Patroni owns the lifecycle)",
            "create Patroni venv at /opt/patroni (patroni[etcd3] + psycopg2-binary), dirs, unit file",
            f"render /etc/patroni/config.yml per node — scope '{cfg.name}-postgres', "
            f"pg_hba for {self._subnet(ctx)} + the 127.0.0.1/32 replication/rewind entries",
            "passwords for postgres/replicator/rewind/authentik generated once, pinned in state "
            "(NEVER printed)",
            f"START ORDER (gated, not fire-and-forget): {leader.name} first → wait until REST "
            f"/primary answers 200 ('promoted self to leader') → {others[0]} → wait streaming → {others[1]}",
            "create authentik role + database on the leader (idempotent)",
            "verify: patronictl list shows 1 Leader + 2 running replicas",
        ]

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        secrets_ = self._secrets(ctx)
        scope = f"{cfg.name}-postgres"
        extra_pg_hba = list((cfg.raw.get("postgres") or {}).get("extra_pg_hba", []) or [])
        unit = __import__("importlib").resources.files("akropolis.templates") \
            .joinpath("patroni.service").read_text()

        # --- 1. install + config on ALL nodes, start nothing --------------
        for conn in ctx.fleet:
            node = conn.node.name
            ctx.begin(node, "installing PostgreSQL 16 from pgdg", "no-op when present")
            r = conn.run(PG_INSTALL, timeout=900)
            ctx.record(node, "postgresql 16 installed", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")
            ctx.begin(node, "patroni venv + dirs", "pip install patroni[etcd3]")
            r = conn.run(PATRONI_INSTALL, timeout=900)
            ctx.record(node, "patroni venv + dirs", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            content = render("patroni-config.yml.j2",
                             node=conn.node, nodes=cfg.nodes, scope=scope,
                             nodes_subnet=self._subnet(ctx),
                             extra_pg_hba=extra_pg_hba,
                             postgres_password=secrets_["postgres_password"],
                             replicator_password=secrets_["replicator_password"],
                             rewind_password=secrets_["rewind_password"])
            push_file(conn, content, "/etc/patroni/config.yml",
                      mode="0600", owner="postgres:postgres")
            push_file(conn, unit, "/etc/systemd/system/patroni.service")
            r = conn.run("systemctl daemon-reload")
            ctx.record(node, "config + unit rendered", r.ok, r.err if not r.ok else "")

        # --- 2. leader first, gated -----------------------------------------
        leader_conn = next(c for c in ctx.fleet if c.node.bootstrap_leader)
        followers = [c for c in ctx.fleet if not c.node.bootstrap_leader]

        r = leader_conn.run("systemctl enable --now patroni")
        ctx.record(leader_conn.node.name, "patroni started (bootstrap leader)", r.ok,
                   r.err if not r.ok else "")
        ctx.begin(leader_conn.node.name, "waiting for leader promotion",
                  "initdb + REST /primary → 200")
        promoted = wait_for(
            leader_conn,
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8008/primary",
            expect="200", timeout=300, interval=5,
            tick=lambda el: ctx.tick(f"{int(el)}s / 300s"),
        )
        ctx.record(leader_conn.node.name, "promoted self to leader", promoted,
                   "" if promoted else "REST /primary never returned 200 within 300s — "
                   "check journalctl -u patroni")
        if not promoted:
            raise RuntimeError("bootstrap leader never promoted — stopping before "
                               "starting replicas (see journalctl -u patroni on "
                               f"{leader_conn.node.name})")

        # --- 3. replicas one at a time, each gated --------------------------
        for conn in followers:
            node = conn.node.name
            r = conn.run("systemctl enable --now patroni")
            ctx.record(node, "patroni started (replica)", r.ok, r.err if not r.ok else "")
            ctx.begin(node, "waiting for replica to join", "basebackup + streaming")
            streaming = wait_for(conn, ROLE_CMD, expect="replica", timeout=300, interval=5,
                                 tick=lambda el: ctx.tick(f"{int(el)}s / 300s"))
            if streaming:
                # role is replica — now require state running/streaming
                streaming = wait_for(
                    conn,
                    f"{ROLE_CMD} | grep -E 'replica (running|streaming)'",
                    timeout=180, interval=5,
                )
            ctx.record(node, "replica streaming", streaming,
                       "" if streaming else "never reached replica running/streaming")
            if not streaming:
                raise RuntimeError(f"{node} never became a healthy replica — stopping")

        # --- 4. authentik role + database on the leader (idempotent) --------
        db_pass = secrets_["authentik_db_password"].replace("'", "''")
        sql = (
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authentik') "
            f"THEN CREATE ROLE authentik LOGIN PASSWORD '{db_pass}'; END IF; END $$;"
        )
        r = leader_conn.run(f"sudo -u postgres psql -h /var/run/postgresql -p 5432 "
                            f"-v ON_ERROR_STOP=1 -c {shlex.quote(sql)}", timeout=60)
        ctx.record(leader_conn.node.name, "authentik role", r.ok, r.err if not r.ok else "")
        r2 = leader_conn.run(
            "sudo -u postgres psql -h /var/run/postgresql -p 5432 -tAc "
            "\"SELECT 1 FROM pg_database WHERE datname='authentik'\"")
        if r2.out.strip() != "1":
            r2 = leader_conn.run("sudo -u postgres psql -h /var/run/postgresql -p 5432 "
                                 "-v ON_ERROR_STOP=1 -c "
                                 "'CREATE DATABASE authentik OWNER authentik'", timeout=60)
        ctx.record(leader_conn.node.name, "authentik database", r2.ok,
                   r2.err if not r2.ok else "")

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        leader_conn = next(c for c in ctx.fleet if c.node.bootstrap_leader)
        r = leader_conn.run(
            "/opt/patroni/bin/patronictl -c /etc/patroni/config.yml list 2>&1", timeout=60)
        out = r.out
        leaders = out.count(" Leader ")
        replicas = sum(out.count(f" {role} ") for role in ("Replica", "Sync Standby"))
        running = out.count("running") + out.count("streaming")
        ok = r.ok and leaders == 1 and replicas == 2 and running >= 3
        ctx.record("cluster", "patronictl list: 1 Leader + 2 replicas, all running", ok,
                   f"leaders={leaders} replicas={replicas} running-states={running}")
        if not ok and out:
            print(out)

        # The apply step can fail without breaking the cluster topology (seen in
        # the field: pg_hba rejected the Unix-socket psql, yet patronictl was
        # green and the phase reported OK). Verify must own the outcome.
        r_role = leader_conn.run(
            "sudo -u postgres psql -h /var/run/postgresql -p 5432 -tAc "
            "\"SELECT 1 FROM pg_roles WHERE rolname='authentik'\"", timeout=60)
        r_db = leader_conn.run(
            "sudo -u postgres psql -h /var/run/postgresql -p 5432 -tAc "
            "\"SELECT 1 FROM pg_database WHERE datname='authentik'\"", timeout=60)
        app_ok = r_role.out.strip() == "1" and r_db.out.strip() == "1"
        ctx.record("cluster", "authentik role + database exist", app_ok,
                   "" if app_ok else (r_role.err or r_db.err or "role/db missing"))
        return ok and app_ok
