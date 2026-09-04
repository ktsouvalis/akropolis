"""restore (single) — optional phase: load a SQL dump into the single node's
containerized PostgreSQL.

Same DESTRUCTIVE-by-definition shape as the HA restore phase, considerably
simpler because there is exactly one node and one database container:

  - no Patroni leader lookup — there is just the one postgres container,
    always reachable the same way
  - no ownership-normalisation step: the postgres container's only
    superuser IS the app role (POSTGRES_USER=authentik in
    authentik-single-env.j2 — the official postgres image renames its
    bootstrap superuser to whatever POSTGRES_USER says, there is no
    separate 'postgres' role here at all), so anything the dump creates
    without an explicit OWNER TO already belongs to authentik. The HA
    phase's "restored objects owned by postgres, not the app role" trap
    (see NOTES.md) cannot happen on this topology.
  - psql runs via `docker exec` into the postgres container instead of
    `sudo -u postgres psql -h /var/run/postgresql` on bare metal —
    PostgreSQL here is a container, not a Patroni-managed bare-metal
    instance (see authentik_single_phase.py)

Still checks the dump's pg_dump-version-skew GUC header against the target
server before anything destructive (identical reasoning to the HA phase —
see restore_phase.py's own docstring), and still stages the dump in /tmp on
the node before loading it (NOTES.md: "SFTP cannot sudo" — /tmp is world
writable, exactly where a privileged put() is not required).

`restore.database` / `restore.owner` are deliberately NOT read here (unlike
the HA phase): the container's actual database name and superuser are fixed
by authentik-single-env.j2's PG_DB/PG_USER, both "authentik", neither
currently configurable — so a restore.owner override would silently not
match what the container was actually initialised with. Fixed to match
reality instead of accepting a config value that could lie about it.

Skipped entirely unless restore.sql_file is set in the site config.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import shlex
import time
from pathlib import Path

from .authentik_phase import apply_brand, dump_logs, wait_healthy, wait_one_healthy
from .base import Phase, PhaseContext

DB = "authentik"      # fixed — matches PG_DB in authentik-single-env.j2
OWNER = "authentik"    # fixed — matches PG_USER in authentik-single-env.j2
PSQL = "docker exec -i authentik-postgresql-1 psql -U authentik -v ON_ERROR_STOP=1"


class RestoreSinglePhase(Phase):
    name = "restore"

    # ------------------------------------------------------------------ util
    def _rcfg(self, ctx: PhaseContext) -> dict:
        return ctx.cfg.raw.get("restore") or {}

    def _sql_file(self, ctx: PhaseContext) -> Path | None:
        f = str(self._rcfg(ctx).get("sql_file") or "").strip()
        return Path(f).expanduser() if f else None

    # version-skew check — identical logic to the HA phase's, just against
    # the containerized server instead of a bare-metal one (see module doc)
    def _header_set_params(self, local: Path, gz: bool) -> list[str]:
        opener = gzip.open if gz else open
        params: list[str] = []
        with opener(local, "rt", errors="replace") as f:
            for _ in range(400):
                line = f.readline()
                if not line:
                    break
                m = re.match(r"^SET\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=", line)
                if m and m.group(1) not in params:
                    params.append(m.group(1))
        return params

    def _unknown_params(self, ctx: PhaseContext, conn, params: list[str]) -> list[str]:
        if not params:
            return []
        lst = ",".join(f"'{p}'" for p in params)
        r = conn.run(f"{PSQL} -tA -d postgres -c "
                     + shlex.quote(f"SELECT name FROM pg_settings WHERE name IN ({lst})"),
                     timeout=60)
        if not r.ok:
            return []
        known = {ln.strip() for ln in r.out.splitlines() if ln.strip()}
        return [p for p in params if p not in known]

    # ------------------------------------------------------------------ plan
    def needs_confirm(self, ctx: PhaseContext) -> bool:
        return self._sql_file(ctx) is not None

    def plan(self, ctx: PhaseContext) -> list[str]:
        local = self._sql_file(ctx)
        if local is None:
            return ["restore.sql_file is not set — phase will be SKIPPED "
                    "(set it and --replay restore to load a dump later)"]
        size = f"{local.stat().st_size / 1024 / 1024:.1f} MB" if local.exists() \
            else "FILE NOT FOUND — apply will refuse"
        return [
            f"[red]DESTRUCTIVE[/red]: database '{DB}' is dropped and replaced "
            f"with {local} ({size})",
            "check the dump's SET-header GUCs against the server (pg_settings) BEFORE "
            "anything destructive — version-skew params are stripped at load; nothing "
            "else is filtered",
            "stop server + worker (docker compose stop server worker) — the postgres "
            "container stays up throughout",
            "SFTP the dump to /tmp on the node, verify sha256, chmod 600",
            f"DROP DATABASE {DB} WITH (FORCE) → CREATE OWNER {OWNER} → "
            "psql -v ON_ERROR_STOP=1 the dump, via docker exec into the postgres "
            "container (any error stops with authentik down, never half-up on half-data)",
            f"no ownership-normalisation step needed — the container's only superuser "
            f"IS '{OWNER}' (POSTGRES_USER in authentik-single-env.j2), so the dump "
            "already loads owned correctly",
            "delete the dump from the node (it contains every secret the IdP holds)",
            "start the WORKER alone first and gate on restore.migration_timeout "
            "(default 3600s): migrating restored data outlasts compose's ~150s "
            "dependency wait and would abort the up",
            "then the server, --no-deps (a plain up -d would restart the worker "
            "we just gated); container logs are printed automatically if a gate expires",
            "verify: restored DB has tables + users, server+worker healthy",
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
        timeout = int(self._rcfg(ctx).get("timeout", 3600))
        mig_timeout = int(self._rcfg(ctx).get("migration_timeout", 3600))
        gz = local.suffix == ".gz"
        conn = ctx.fleet.conns[0]
        node = conn.node.name

        ctx.begin("workstation", "hashing dump", str(local))
        sha = hashlib.sha256()
        with open(local, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        digest = sha.hexdigest()
        size_mb = local.stat().st_size / 1024 / 1024
        ctx.record("workstation", "dump hashed", True,
                   f"{size_mb:.1f} MB, sha256 {digest[:16]}…")

        # --- version skew, BEFORE anything destructive ------------------------
        ctx.begin(node, "checking dump against server", "pg_dump GUC header")
        params = self._header_set_params(local, gz)
        strip = self._unknown_params(ctx, conn, params)
        if strip:
            ctx.record(node, "version-skew GUCs will be stripped", True,
                       ", ".join(strip) + " — unknown to this server "
                       "(dump taken with a newer pg_dump); header-only, data untouched",
                       warn=True)
        else:
            ctx.record(node, "dump header compatible with server", True,
                       f"{len(params)} SET params, all recognised")

        # --- server + worker down (postgres stays up) --------------------------
        ctx.begin(node, "stopping server + worker", "postgres stays up")
        r = conn.run("cd /opt/authentik && docker compose stop server worker", timeout=300)
        ctx.record(node, "server + worker stopped", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("could not stop server/worker — refusing to touch "
                               "the database with clients up")

        # --- upload + integrity -------------------------------------------------
        remote = f"/tmp/akropolis-restore-{ctx.cfg.name}.sql" + (".gz" if gz else "")
        ctx.begin(node, "uploading dump", f"0 / {size_mb:.0f} MB")
        conn.put(str(local), remote,
                 callback=lambda done, total:
                 ctx.tick(f"{done / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB"))
        conn.run(f"chmod 600 {shlex.quote(remote)}")
        r = conn.run(f"sha256sum {shlex.quote(remote)} | cut -d' ' -f1", timeout=300)
        if r.out.strip() != digest:
            conn.run(f"rm -f {shlex.quote(remote)}")
            raise RuntimeError("uploaded dump sha256 mismatch — transfer corrupted, "
                               "dump removed from the node")
        ctx.record(node, "dump uploaded + sha256 verified", True, remote)

        # --- drop, create, load ---------------------------------------------------
        ctx.begin(node, f"drop + recreate database '{DB}'")
        r = conn.run(f"{PSQL} -d postgres "
                     f"-c {shlex.quote(f'DROP DATABASE IF EXISTS {DB} WITH (FORCE)')} "
                     f"-c {shlex.quote(f'CREATE DATABASE {DB} OWNER {OWNER}')}",
                     timeout=120)
        ctx.record(node, f"database '{DB}' dropped + recreated", r.ok,
                   r.err if not r.ok else f"owner {OWNER}")
        if not r.ok:
            raise RuntimeError("drop/create failed — authentik is still down; "
                               "inspect, then --replay restore")

        # the dump lives on the HOST at `remote`; docker exec -i's stdin is
        # fed by the host shell's redirect below — the file never needs to be
        # visible inside the container's own filesystem.
        reader = f"gunzip -c {shlex.quote(remote)}" if gz else f"cat {shlex.quote(remote)}"
        if strip:
            expr = "; ".join(f"/^SET {p} = /d" for p in strip)
            reader += f" | sed {shlex.quote(expr)}"
        ctx.begin(node, "restoring dump", f"psql into '{DB}' — budget {timeout}s")
        r = conn.run(f"{reader} | {PSQL} -q -d {DB}", timeout=timeout)
        ctx.record(node, "dump restored", r.ok,
                   (r.err.splitlines()[-1] if r.err else "") if not r.ok
                   else "ON_ERROR_STOP clean")
        conn.run(f"rm -f {shlex.quote(remote)}")
        if not r.ok:
            raise RuntimeError("restore failed — authentik left DOWN on purpose "
                               "(never half-up on half-data); fix the dump and "
                               "--replay restore")
        ctx.record(node, "dump removed from node", True, "")

        ctx.state.data["generated"]["restore_last"] = {
            "file": str(local), "sha256": digest, "database": DB,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        ctx.state.save()

        # --- authentik back up, bootstrap-style ------------------------------
        # See the HA restore phase's docstring for why the worker is started
        # ALONE first, gated on our own budget, and the server with --no-deps:
        # a plain `up -d` re-evaluates depends_on and restarts the worker we
        # just gated, waiting on it with compose's own ~150s clock instead.
        ctx.begin(node, "starting worker alone", "migrations on restored data")
        r = conn.run("cd /opt/authentik && docker compose up -d --no-deps worker",
                     timeout=1800)
        ctx.record(node, "worker starting", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError(f"could not start the worker on {node}")

        good = wait_one_healthy(ctx, conn, "authentik-worker-1", timeout=mig_timeout,
                                label="migrating restored data")
        ctx.record(node, "worker healthy (migrations complete)", good,
                   "" if good else f"still not healthy after {mig_timeout}s")
        if not good:
            st = conn.run("docker inspect -f '{{.State.Health.Status}}' "
                          "authentik-worker-1 2>/dev/null").out.strip()
            ctx.record(node, "worker health status", False, st or "unknown")
            dump_logs(ctx, conn, "worker")
            hint = ("still migrating — raise restore.migration_timeout"
                    if st == "starting" else
                    "the healthcheck is FAILING, not slow — the log above has the reason")
            raise RuntimeError(f"worker did not reach healthy within {mig_timeout}s "
                               f"(status: {st or 'unknown'}) — {hint}")

        ctx.begin(node, "starting server", "--no-deps: worker stays up")
        r = conn.run("cd /opt/authentik && docker compose up -d --no-deps server",
                     timeout=1800)
        ctx.record(node, "server starting", r.ok, r.err if not r.ok else "")
        good = r.ok and wait_healthy(ctx, conn, timeout=900)
        ctx.record(node, "healthy on restored data", good,
                   "" if good else "never reached healthy")
        if not good:
            dump_logs(ctx, conn, "server")
            raise RuntimeError("node never became healthy on the restored database")

        # Check the bootstrap token against the just-restored database BEFORE
        # the branding re-apply below, which uses this same token to talk to
        # the API — a dead token there would otherwise surface from inside
        # apply_brand() as "could not find the default brand", which is
        # misleading (the brand is fine; the token isn't valid anymore).
        # verify() re-checks this for the operator-facing record; this copy
        # only exists to gate whether apply_brand() is worth attempting.
        token = ctx.state.data["generated"].get("authentik_bootstrap_token", "")
        tok_ok_now = True
        if token:
            r = conn.run(f"curl -sk -H {shlex.quote('Authorization: Bearer ' + token)} "
                         "-o /dev/null -w '%{http_code}' "
                         "https://127.0.0.1:443/api/v3/admin/version/", timeout=30)
            tok_ok_now = r.out.strip() == "200"

        branding = (ctx.cfg.raw.get("authentik") or {}).get("branding") or {}
        if branding and token and not tok_ok_now:
            ctx.record(node, "branding re-apply skipped", True,
                       "bootstrap token is dead post-restore (not a missing brand) — "
                       "get a fresh token and re-apply branding by hand, or "
                       "--replay restore once state has a live one", warn=True)
        elif branding and token:
            apply_brand(ctx, conn, branding, token, port=443)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        if self._sql_file(ctx) is None:
            ctx.record("workstation", "verify: restore skipped", True, "")
            return True
        conn = ctx.fleet.conns[0]
        node = conn.node.name

        r = conn.run(f"{PSQL} -tA -d {DB} -c "
                     "\"SELECT count(*) FROM pg_tables WHERE schemaname='public'\"",
                     timeout=60)
        tables = int(r.out.strip() or 0) if r.ok else 0
        ctx.record(node, "verify: restored schema has tables",
                   tables > 0, f"{tables} public tables")

        r = conn.run(f"{PSQL} -tA -d {DB} -c 'SELECT count(*) FROM authentik_core_user'",
                     timeout=60)
        users = int(r.out.strip() or 0) if r.ok else -1
        ctx.record(node, "verify: authentik_core_user populated",
                   users > 0, f"{users} users" if users >= 0 else "table missing")

        # The restore replaced the database the bootstrap API token lived in —
        # it is almost certainly dead now, and the failure otherwise surfaces
        # far away (an "unauthorized" panel in the monitor, hours later).
        token = ctx.state.data["generated"].get("authentik_bootstrap_token", "")
        tok_ok = True
        if token:
            r = conn.run(f"curl -sk -H {shlex.quote('Authorization: Bearer ' + token)} "
                         "-o /dev/null -w '%{http_code}' "
                         "https://127.0.0.1:443/api/v3/admin/version/", timeout=30)
            tok_ok = r.out.strip() == "200"
            ctx.record(node, "bootstrap API token still valid", tok_ok,
                       f"HTTP {r.out}" if tok_ok else
                       f"HTTP {r.out} — the restored database does not contain this "
                       "token. Create a new one (akadmin > Directory > Tokens, admin "
                       "scope) and put it in the monitor config", warn=not tok_ok)

        good = wait_healthy(ctx, conn, timeout=60, label="verify: healthy gate")
        ctx.record(node, "verify: containers healthy", good, "")
        r = conn.run("curl -sk -o /dev/null -w '%{http_code}' "
                     "https://127.0.0.1:443/-/health/ready/")
        ready = r.out in ("200", "204")
        ctx.record(node, "verify: /-/health/ready/", ready, f"HTTP {r.out}")

        return tables > 0 and users > 0 and good and ready
