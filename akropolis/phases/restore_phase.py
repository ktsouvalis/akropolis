"""restore — optional phase: load a SQL dump into the Patroni leader.

The migration/cutover move: a `pg_dump` taken on the old system (plain .sql or
.sql.gz) replaces the freshly-bootstrapped authentik database, so the new
cluster comes back up with the real users, providers and flows instead of a
blank akadmin install.

Skipped entirely unless `restore.sql_file` is set in the site config — the
pipeline runs through it untouched otherwise. When enabled it is DESTRUCTIVE
by definition (the current database is dropped), so the ordering is strict and
every step is gated:

  1. locate the CURRENT Patroni leader via REST /primary on each node — the
     leader may have moved since bootstrap; assuming node-1 would psql a replica
  2. stop authentik on ALL nodes (`docker compose down`) — no live connections,
     no half-written sessions during the swap
  3. SFTP the dump to the leader (base64 push is unusable at dump sizes),
     verify sha256 end-to-end
  4. DROP DATABASE ... WITH (FORCE) + CREATE ... OWNER authentik, then
     `psql -v ON_ERROR_STOP=1` the dump in — any error stops the phase with
     authentik still down, never half-up on half-data
  5. delete the dump from the node (it contains every secret the IdP holds)
  6. start authentik back exactly like a bootstrap: leader alone first, health
     gate covering any migrations the server applies on top of the restored
     schema (dump from an older tag), then the other nodes one at a time

Replays: the phase is marked done afterwards; `--replay restore` re-runs it
(e.g. a fresher dump at real cutover). The dump's sha256 and timestamp are
recorded in state for the paper trail.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import time
from pathlib import Path

from .authentik_phase import wait_healthy
from .base import Phase, PhaseContext

PSQL = "sudo -u postgres psql -h /var/run/postgresql -p 5432 -v ON_ERROR_STOP=1"


class RestorePhase(Phase):
    name = "restore"

    # ------------------------------------------------------------------ util
    def _rcfg(self, ctx: PhaseContext) -> dict:
        return ctx.cfg.raw.get("restore") or {}

    def _sql_file(self, ctx: PhaseContext) -> Path | None:
        f = str(self._rcfg(ctx).get("sql_file") or "").strip()
        return Path(f).expanduser() if f else None

    def _database(self, ctx: PhaseContext) -> str:
        db = str(self._rcfg(ctx).get("database") or "authentik")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", db):
            raise RuntimeError(f"restore.database {db!r}: only [a-z0-9_] names are "
                               "accepted (it is interpolated into SQL)")
        return db

    def _find_leader(self, ctx: PhaseContext):
        for conn in ctx.fleet:
            r = conn.run("curl -s -o /dev/null -w '%{http_code}' "
                         "http://127.0.0.1:8008/primary", timeout=15)
            if r.out.strip() == "200":
                return conn
        return None

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        local = self._sql_file(ctx)
        if local is None:
            return ["restore.sql_file is not set — phase will be SKIPPED "
                    "(set it and --replay restore to load a dump later)"]
        size = f"{local.stat().st_size / 1024 / 1024:.1f} MB" if local.exists() \
            else "FILE NOT FOUND — apply will refuse"
        db = self._database(ctx)
        return [
            f"[red]DESTRUCTIVE[/red]: database '{db}' on the cluster is dropped and "
            f"replaced with {local} ({size})",
            "stop authentik on ALL nodes (docker compose down) before touching the DB",
            "locate the CURRENT Patroni leader via REST /primary (it may not be node-1)",
            "SFTP the dump to the leader, verify sha256, chmod 600",
            f"DROP DATABASE {db} WITH (FORCE) → CREATE OWNER authentik → "
            "psql -v ON_ERROR_STOP=1 the dump (any error stops with authentik down, "
            "never half-up on half-data)",
            "delete the dump from the node (it contains every secret the IdP holds)",
            "start authentik back bootstrap-style: leader alone first, health gate "
            "covers migrations on top of the restored schema, then the others",
            "verify: restored DB has tables + users, all nodes healthy + ready",
        ]

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        local = self._sql_file(ctx)
        if local is None:
            ctx.record("workstation", "restore skipped", True,
                       "restore.sql_file not set in the site config")
            return
        if not local.exists():
            raise RuntimeError(f"restore.sql_file does not exist: {local}")
        db = self._database(ctx)
        timeout = int(self._rcfg(ctx).get("timeout", 3600))
        gz = local.suffix == ".gz"

        ctx.begin("workstation", "hashing dump", str(local))
        sha = hashlib.sha256()
        with open(local, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        digest = sha.hexdigest()
        size_mb = local.stat().st_size / 1024 / 1024
        ctx.record("workstation", "dump hashed", True,
                   f"{size_mb:.1f} MB, sha256 {digest[:16]}…")

        # --- 1. current leader ------------------------------------------------
        leader = self._find_leader(ctx)
        if leader is None:
            raise RuntimeError("no node answers REST /primary with 200 — is the "
                               "patroni cluster healthy? (patronictl list)")
        lname = leader.node.name
        ctx.record(lname, "current Patroni leader located", True, "REST /primary → 200")

        # --- 2. authentik down everywhere ------------------------------------
        for conn in ctx.fleet:
            node = conn.node.name
            ctx.begin(node, "stopping authentik", "docker compose down")
            r = conn.run("cd /opt/authentik && docker compose down", timeout=300)
            ctx.record(node, "authentik stopped", r.ok, r.err if not r.ok else "")
            if not r.ok:
                raise RuntimeError(f"could not stop authentik on {node} — "
                                   "refusing to touch the database with clients up")

        # --- 3. upload + integrity -------------------------------------------
        remote = f"/tmp/akropolis-restore-{ctx.cfg.name}.sql" + (".gz" if gz else "")
        ctx.begin(lname, "uploading dump", f"0 / {size_mb:.0f} MB")
        leader.put(str(local), remote,
                   callback=lambda done, total:
                   ctx.tick(f"{done / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB"))
        leader.run(f"chmod 600 {shlex.quote(remote)}")
        r = leader.run(f"sha256sum {shlex.quote(remote)} | cut -d' ' -f1", timeout=300)
        if r.out.strip() != digest:
            leader.run(f"rm -f {shlex.quote(remote)}")
            raise RuntimeError("uploaded dump sha256 mismatch — transfer corrupted, "
                               "dump removed from the node")
        ctx.record(lname, "dump uploaded + sha256 verified", True, remote)

        # --- 4. drop, create, load -------------------------------------------
        ctx.begin(lname, f"drop + recreate database '{db}'")
        r = leader.run(f"{PSQL} -c {shlex.quote(f'DROP DATABASE IF EXISTS {db} WITH (FORCE)')} "
                       f"-c {shlex.quote(f'CREATE DATABASE {db} OWNER authentik')}",
                       timeout=120)
        ctx.record(lname, f"database '{db}' dropped + recreated", r.ok,
                   r.err if not r.ok else "owner authentik")
        if not r.ok:
            raise RuntimeError("drop/create failed — authentik is still down; "
                               "inspect, then --replay restore")

        reader = f"gunzip -c {shlex.quote(remote)}" if gz else f"cat {shlex.quote(remote)}"
        ctx.begin(lname, "restoring dump", f"psql into '{db}' — budget {timeout}s")
        r = leader.run(f"{reader} | {PSQL} -q -d {db}", timeout=timeout)
        ctx.record(lname, "dump restored", r.ok,
                   (r.err.splitlines()[-1] if r.err else "") if not r.ok else "ON_ERROR_STOP clean")
        # --- 5. the dump never outlives the restore ---------------------------
        leader.run(f"rm -f {shlex.quote(remote)}")
        if not r.ok:
            raise RuntimeError("restore failed — authentik left DOWN on purpose "
                               "(never half-up on half-data); fix the dump and "
                               "--replay restore")
        ctx.record(lname, "dump removed from node", True, "")

        ctx.state.data["generated"]["restore_last"] = {
            "file": str(local), "sha256": digest, "database": db,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        ctx.state.save()

        # --- 6. authentik back up, bootstrap-style ----------------------------
        first = next(c for c in ctx.fleet if c.node.bootstrap_leader)
        others = [c for c in ctx.fleet if not c.node.bootstrap_leader]

        ctx.begin(first.node.name, "starting authentik", "alone first")
        r = first.run("cd /opt/authentik && docker compose up -d", timeout=1800)
        ctx.record(first.node.name, "authentik starting", r.ok, r.err if not r.ok else "")
        good = r.ok and wait_healthy(ctx, first, timeout=900,
                                     label="migrations on restored schema")
        ctx.record(first.node.name, "healthy on restored data", good,
                   "" if good else "never reached healthy — docker compose logs")
        if not good:
            raise RuntimeError("first node never became healthy on the restored "
                               "database — stopping before starting the others")

        for conn in others:
            node = conn.node.name
            ctx.begin(node, "starting authentik")
            r = conn.run("cd /opt/authentik && docker compose up -d", timeout=1800)
            ctx.record(node, "authentik starting", r.ok, r.err if not r.ok else "")
            good = r.ok and wait_healthy(ctx, conn, timeout=900)
            ctx.record(node, "healthy", good, "" if good else "never reached healthy")
            if not good:
                raise RuntimeError(f"{node} never became healthy after the restore")

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        if self._sql_file(ctx) is None:
            ctx.record("workstation", "verify: restore skipped", True, "")
            return True
        db = self._database(ctx)
        leader = self._find_leader(ctx)
        if leader is None:
            ctx.record("cluster", "verify: patroni leader", False, "no /primary → 200")
            return False

        r = leader.run(f"{PSQL} -tA -d {db} -c "
                       "\"SELECT count(*) FROM pg_tables WHERE schemaname='public'\"",
                       timeout=60)
        tables = int(r.out.strip() or 0) if r.ok else 0
        ctx.record(leader.node.name, "verify: restored schema has tables",
                   tables > 0, f"{tables} public tables")

        r = leader.run(f"{PSQL} -tA -d {db} -c "
                       "'SELECT count(*) FROM authentik_core_user'", timeout=60)
        users = int(r.out.strip() or 0) if r.ok else -1
        ctx.record(leader.node.name, "verify: authentik_core_user populated",
                   users > 0, f"{users} users" if users >= 0 else "table missing")

        ok = tables > 0 and users > 0
        for conn in ctx.fleet:
            node = conn.node.name
            good = wait_healthy(ctx, conn, timeout=60, label="verify: healthy gate")
            ctx.record(node, "verify: containers healthy", good, "")
            r = conn.run("curl -sk -o /dev/null -w '%{http_code}' "
                         "https://127.0.0.1:9443/-/health/ready/")
            ready = r.out in ("200", "204")
            ctx.record(node, "verify: /-/health/ready/", ready, f"HTTP {r.out}")
            ok = ok and good and ready
        return ok
