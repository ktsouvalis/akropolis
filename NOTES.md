# Production findings

Discrepancies between the running UoP production cluster, the implementation
guide, and what akropolis deploys — discovered while building the tool by
comparing real node configs against guide text. Fresh akropolis-provisioned
clusters do NOT inherit these; this list exists so production can be brought in
line when convenient.

Format: finding, impact, recommended fix, status.

---

## 1. keepalived track-script weight is −20 in prod; learnings say −25

**Found:** 2026-09-02, `/etc/keepalived/keepalived.conf` on ak-node-1 and
ak-node-2 both carry `weight -20`. Guide 6.B.12 ("Key learnings") explicitly
says *use `weight -25` to eliminate priority ties* — the learning was recorded
but never rolled out to the files.

**Impact:** with priorities 100/90/80, node-1 with failed nginx sits at
100−20 = 80 — a dead tie with healthy node-3 at 80. VRRP tie-breaking then
falls to highest source IP, which is arbitrary relative to health. Exactly the
scenario −25 was meant to eliminate.

**Fix:** set `weight -25` in `/etc/keepalived/keepalived.conf` on all 3 nodes,
then `systemctl reload keepalived` one node at a time (BACKUP nodes first,
MASTER last). No VIP movement expected from the reload itself.

**Status:** OPEN. akropolis template already uses −25.

---

## 2. No `nopreempt` in prod — recovered node-1 steals the VIP back

**Found:** 2026-09-02, node-1 runs `state MASTER` priority 100 with no
`nopreempt`; node-2 `state BACKUP` priority 90, also without it. The intended
behavior (node-1 does not reclaim the VIP after recovery, avoiding a second
service flap) is not what the files implement.

**Impact:** every node-1 failure produces TWO client-visible flaps instead of
one — VIP moves away on failure, then moves back on recovery, dropping
in-flight connections both times.

**Fix:** keepalived's canonical no-preemption form — ALL nodes `state BACKUP`
with `nopreempt` and differing priorities (`nopreempt` is ignored on a
MASTER-state instance, so simply adding the keyword to node-1 as-is would not
work). Apply on all 3 nodes, restart keepalived one node at a time starting
from the lowest priority; expect one controlled VIP election at the end.
Schedule in a maintenance window.

**Status:** OPEN. akropolis template already uses the all-BACKUP + nopreempt
form (see README, nginx-keepalived section).

---

## 3. keepalived script security warning

**Found:** 2026-09-02, while validating the akropolis template with
`keepalived -t`: configs without `global_defs { script_user root;
enable_script_security }` log a SECURITY VIOLATION about track scripts running
without script security. Prod configs (which lack the block) will be logging
the same warning.

**Impact:** cosmetic/log noise, but it masks real warnings in `journalctl -u
keepalived` and is trivially silenced correctly.

**Fix:** add the `global_defs` block above to all 3 nodes; can ride along with
the fix for #1/#2 in the same edit.

**Status:** OPEN. akropolis template already includes it.

---

## 4. ESDA Lab: verify client-IP visibility behind Traefik

**Found:** 2026-09-02, while adding `network.trusted_proxies` support, refined
after reviewing the actual Traefik dynamic config (`authentik.yml`): Traefik
forwards to `https://10.10.255.25` (internal VIP) with `insecureSkipVerify`,
and appends the real client to `X-Forwarded-For` as standard.

**Nuance:** this is probably NOT fully broken today. nginx's
`$proxy_add_x_forwarded_for` appends rather than overwrites, and Authentik's
Go router parses X-Forwarded-For from trusted private-range proxies by
default — so for EXTERNAL clients the real IP plausibly survives the chain.
The clearly-degraded pieces are `X-Real-IP` (always Traefik's address) and
potentially INTERNAL campus clients, where every XFF hop is a private address
and the trusted-proxy walk can land on the wrong entry.

**Verify:** open recent login events in ESDA Authentik admin — one from an
external client, one from an internal workstation — and check the recorded
client IPs against reality.

**Fix if needed:** add to the ESDA nginx.conf http block:
`set_real_ip_from 192.168.20.20; real_ip_header X-Forwarded-For;
real_ip_recursive on;` and pass through the upstream X-Forwarded-Proto.
(Exactly what akropolis's `network.trusted_proxies` renders — an ESDA-style
site config is `tls.provider: self_signed` + `trusted_proxies:
[192.168.20.20]`.)

**Status:** OPEN — verify first; external-client path may already be correct.


## pg_dump version skew on restore (Sep 2026, ESDA Lab)

First real run of the `restore` phase failed with `ERROR: unrecognized
configuration parameter "transaction_timeout"`. Not a data problem: the dump
had been written by a `pg_dump` 17 binary, whose header emits
`SET transaction_timeout = 0;` — a GUC that does not exist before PostgreSQL
17, so the 16.x target rejects it on the first header line.

Two findings:

1. The phase discovered the incompatibility *after* the DROP, leaving the
   cluster down on an empty database. The compatibility question is now
   answered before anything destructive happens (v0.10.2).
2. The fix is generic, not a hardcoded `transaction_timeout` special case:
   the dump's header GUCs are checked against the target's `pg_settings`, so
   18→16 or 17→15 skew is handled by the same code path.

Operational note: prefer dumping with a `pg_dump` matching the target major
version. The strip is a safety net for dumps you did not produce (backup
appliances, colleagues' workstations with newer client tools).


## Restored database vs compose's dependency wait (Sep 2026, ESDA Lab)

Right after the GUC-skew fix, the same restore failed again — this time on
startup: `dependency failed to start: container authentik-worker-1 is
unhealthy`. The restore itself was clean; the worker was migrating the
restored data (real user counts, not an empty schema) and had not yet begun
answering its liveness port when compose's dependency wait expired.

The budget is baked into the compose file: `start_period: 60s` +
`interval: 30s` x `retries: 3`, so `docker compose up -d` gives up ~150s
after the worker starts and tears down the `up` mid-migration. Fresh
bootstraps never hit it because an empty schema migrates in seconds.

Fix (v0.10.3): on the restore path the worker is started alone with
`up -d --no-deps worker`, so no dependent service is watching a clock, and
gated on `restore.migration_timeout` (default 3600s). The server is started
only after the worker reports healthy. Nodes 2 and 3 are unaffected — they
find a migrated schema — so they keep the plain `up -d`.

Also added: whenever a health gate expires, akropolis prints the tail of the
container log instead of advising the operator to go and read it.


## Monitor config assumed TLS everywhere (Sep 2026)

The emitted monitor config hardcoded `ports.authentik: 9443` and said nothing
about scheme, while ak-monitor probes `https://<node-ip>/monitor` for the
nginx/keepalived panel and `/-/health/live/` on 9443. On a `tls: none` lab
site nginx serves plain HTTP on :80, so every node shows DOWN for a reason
that has nothing to do with the cluster.

Second problem in the same place: the Authentik **worker** does not listen on
9443 at all — since v0.7.10 it binds its own port (9081) so it cannot squat
the server's. Probing the worker on 9443 reaches the *server*, so a dead
worker looks alive — the exact failure mode the worker healthcheck exists to
catch.

Handoff (v0.10.4) now emits a `scheme:` block: `nginx` (http/https) and
`nginx_port` derived from tls.provider, `authentik`/`authentik_worker`
schemes, `verify_tls` (true only for acme/import — self-signed and lab certs
must not be verified), plus `ports.authentik_worker: 9081`.

**ak-monitor must be taught to read these keys** — emitting them is only half
the fix. Until then, a tls: none site needs the scheme edited by hand.


## Health gate accepted "unhealthy" (Sep 2026) — gate correctness

`wait_one_healthy` (introduced v0.10.3) polled with `expect="healthy"`, a
SUBSTRING test. `"healthy" in "unhealthy"` is True in Python, so the gate
reported "worker healthy (migrations complete)" for a container Docker had
marked **unhealthy**. The phase then tried to start the server, compose
refused on the dependency condition, and the failure appeared to contradict
the line printed immediately above it.

The same trap existed in the older pair gate's polling (`grep -v healthy`
filters out "unhealthy" lines too); it survived only because a separate exact
`== "healthy"` confirmation ran afterwards.

Both now use shell string equality against the deduplicated status list —
`[ "$(docker inspect -f '{{.State.Health.Status}}' ... | sort -u)" = healthy ]`
— which cannot match a substring. Verified against a real shell for healthy /
unhealthy / starting / empty.

**Rule for this codebase: never test container health with a substring or
grep match.** A gate that lies is worse than no gate: it moves the failure
somewhere unrelated and makes the operator distrust correct output.

Second fix in the same version: the server is now started with
`up -d --no-deps server`. A plain `up -d` re-evaluates depends_on, RESTARTS
the freshly-gated worker (health resets to "starting") and then waits on it
with compose's own ~150s clock — reintroducing the exact failure the phase
exists to avoid.


## Restored objects owned by postgres, not the app role (Sep 2026, ESDA Lab)

The worker crash-looped after a successful restore:

    psycopg.errors.InsufficientPrivilege:
    permission denied for table authentik_install_id

Cause: the phase creates the database `OWNER authentik` but loads the dump as
the postgres SUPERUSER (extensions and some dump statements need it). Every
object the dump creates *without* an explicit `OWNER TO` therefore belongs to
postgres. The app role can read through its grants but cannot write, so the
worker dies on the `install_id` system migration and `restart: unless-stopped`
loops it roughly every 6 seconds.

Dumps taken with `--no-owner`, or from a source whose role was named
differently, hit this every time. Loading as the app role instead is not a
general fix (extensions require superuser), so ownership is now re-applied
explicitly after the load and proven afterwards (v0.10.6). Reproduced and
verified against a real PostgreSQL 16.

Two things made this much harder to diagnose than it should have been, both
now fixed:

- **verify passed.** It counted tables and users — reads only. The app's first
  action is a WRITE. Verify now does a real write as the app role and asserts
  no public table is owned by anyone else.
- **the liveness endpoint kept answering 200** while the Python process was
  dead, so `/-/health/live/` on the worker said nothing useful (already known
  for the server; equally true here). Container health, not the endpoint, is
  the signal — and container health must be compared exactly (see v0.10.5).


## Restore invalidates the bootstrap API token (Sep 2026)

Obvious in hindsight: the authentik phase proves the bootstrap token against
the live API, then the restore phase replaces the entire database with a dump
from another system. The token the monitor was handed at handoff no longer
exists, and the failure surfaces far from its cause — as "unauthorized" in
the dashboard's Workers panels, long after the restore reported success.

The restore phase now checks the token after bringing authentik back and, if
it is dead, says exactly what to do (new token in akadmin > Directory >
Tokens, admin scope, into the monitor config) as a warning rather than a
failure — the cluster itself is healthy.

## Monitor host blocked by the stub_status ACL (Sep 2026)

The nginx panel read UNREACHABLE for every node while nginx was fine: the
stub_status server on :8080 allows loopback and the node subnet only, and the
monitor runs from a workstation outside it, so nginx answered 403. monitor.ip
is now added to `stub_status_allow` automatically — the same host that gets
the UFW opening in the base phase.


## SFTP cannot sudo (Sep 2026)

First run with branding failed on `[Errno 13] Permission denied`. `conn.put()`
is SFTP, which runs as the SSH user; `sudo` applies to `run()` only. The
branding directory is created root-owned by a privileged `run()`, and the
SFTP write into it is then refused under `become: true`.

The restore phase never hit this because it uploads to /tmp, which is world
writable — so the flaw shipped hidden behind the one call site that happened
to be safe.

push_binary now stages in /tmp and installs with a privileged
`mkdir && mv && chmod`, and re-checks the checksum AFTER the move (a
truncated transfer would otherwise be bind-mounted into the container and
serve a broken asset). The bare OSError is also wrapped: "[Errno 13]
Permission denied" named neither the node, the file, nor which end refused.

**Rule: any write outside /tmp must go through run(), not put().**
