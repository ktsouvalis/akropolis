# Development notes

Bugs found, and lessons learned, while building and testing akropolis
against real clusters. Kept here so a fixed mistake doesn't get repeated in
a later phase, or reintroduced in a future rewrite.

Format varies by entry — some are terse fix logs, some carry an explicit
rule for the codebase. All dates are when the bug was found, not necessarily
when it was fixed.

---

## pg_dump version skew on restore (Sep 2026)

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


## Restored database vs compose's dependency wait (Sep 2026)

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


## Restored objects owned by postgres, not the app role (Sep 2026)

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


## Documented in the README, missing from the example config (Sep 2026)

`base.apt_upgrade` and `network.trusted_proxies` were both implemented and
described in the README, but absent from `config.example.yml` — so an operator
reading the file they actually edit had no way to discover them. A README
paragraph is not discovery.

Auditing the whole surface found four such keys: `base.apt_upgrade`,
`network.trusted_proxies`, `network.stub_status_allow`,
`postgres.extra_pg_hba`. All are now in the example, commented out with the
reasoning that matters (trusted_proxies poisoning client IPs; extra_pg_hba
needing to live in DCS to survive a reinit; apt_upgrade vs unattended library
replacement under a running Patroni).

`tools/audit_config_keys.py` now fails when the code reads a key the example
does not mention, so this cannot silently recur.


## Single-node topology: scaffolding (Sep 2026)

First slice of `site.topology: single` — config validation, `preflight`,
`base`. Kept deliberately separate from the HA phases rather than sprinkling
`if topology == "ha"` through them everywhere; `config.py` and `preflight.py`
each hold exactly one topology branch, `base_setup.py` two (UFW rule set,
monitor port list).

A few decisions worth recording:

- **Different default `authentik.tag` per topology, not one shared default.**
  `ha` stays on `2026.5.6` (multi-node embedded-outpost restart loop on
  2026.8.0). `single` has no multi-node outpost topology to trigger that bug,
  so it defaults to `2026.8.1` instead of inheriting the HA pin. Explicit
  `authentik.tag` in the site config still wins either way.
- **No external reverse-proxy network.** An early sketch had single-node
  Authentik sit behind an existing reverse-proxy Docker network (the pattern
  the current hand-built `auth-tmp` instance uses, itself only there because
  that host doubles as a general-purpose test box). The actual target
  deployment is a dedicated VM reachable by 1:1 NAT (public IP → node's
  private IP via pfsense) with nothing upstream — so akropolis has to own TLS
  termination itself here too. nginx is still part of the single-node
  pipeline; keepalived is not, since there is nothing to fail over to.
- **DNS→VIP becomes DNS→"resolves at all."** The HA preflight's DNS check
  confirms the hostname resolves to the VIP specifically. Single-node has no
  VIP, and NAT means akropolis running on the node itself has no reliable way
  to confirm the hostname resolves to *this* node's public IP rather than
  something else entirely. Rather than fake a check it can't actually make,
  preflight only confirms the hostname resolves to something, and says so —
  the operator is expected to verify the target by hand.
- **Required free ports drop out entirely**, not just get shorter: no
  etcd/Patroni/HAProxy ports at all, because PostgreSQL is a container on an
  internal Docker network and is never published to the host.

Still to do: the `authentik` phase itself (postgres-in-container + server +
worker, generated `.env`/compose derived from a real running `auth-tmp`
instance's files), a keepalived-less `nginx` phase, and a single-node-aware
`clean` (different on-disk paths — no `/etc/patroni`, no `/opt/haproxy`).


## Branding is two halves, not one (Sep 2026)

Noticed while testing a restore against a populated database: the logo only
appeared after the source database was loaded. Mounting a file changes
nothing by itself — Authentik serves the stock logo until the **brand row**
in the database references the asset. The restore brought the source
system's brand row with it, which is why it suddenly worked; on a fresh
cluster the same (correct) configuration looks broken.

The reverse case is worse: a dump whose brand references an asset this cluster
never mounted serves a broken image on the login page.

v0.11.3 closes the loop — after the cluster is healthy, the default brand is
PATCHed via /api/v3/core/brands/ to /static/dist/assets/{icons,images}/<name>.
Brand fields only accept the /static prefix for absolute paths
(goauthentik #19557), which is exactly where /web/dist/assets is served from.
The restore phase re-applies it after loading a dump. Failures warn rather
than fail: the cluster is healthy either way, and a logo is not worth
aborting a provision over.
