# akropolis

*[Ελληνικά](README.gr.md) | English*

Provision and monitor a highly-available 3-node [Authentik](https://goauthentik.io) cluster over SSH.

akropolis turns three fresh Ubuntu 24.04 VMs into a production-grade Authentik identity provider cluster — PostgreSQL 16 with Patroni auto-failover over etcd, per-node HAProxy connection routing, nginx TLS termination, and a keepalived VRRP virtual IP — following the implementation guide developed and battle-tested at the Digital Governance Unit of the University of Peloponnese. It runs from your workstation, needs nothing installed on the nodes beforehand, and ends by emitting a ready-to-use config for its monitoring companion.

Certificates from any source: self-signed, any ACME CA (Let's Encrypt, HARICA ACME, ...), or externally issued files (e.g. the HARICA portal flow used across the Greek public sector).

## Architecture deployed

```
Client → VIP (keepalived/VRRP) → nginx on MASTER node → any of 3 Authentik backends (:9443)
                                                          → Authentik → HAProxy (127.0.0.1:5000) → Patroni leader
                                                          → (cache / sessions / tasks / channels all in PostgreSQL)
```

| Component | How it runs | Purpose |
|---|---|---|
| PostgreSQL 16 + Patroni | bare-metal systemd | HA database with automatic failover |
| etcd v3.5 | Docker, `network_mode: host` | DCS: Patroni's distributed lock + config store |
| HAProxy | Docker, `network_mode: host` | per-node PG router: `127.0.0.1:5000` always reaches the current leader |
| nginx | Docker, `network_mode: host` | TLS termination + load balancing across Authentik backends |
| keepalived | bare-metal systemd | VRRP virtual IP with health-tracked failover |
| Authentik | Docker, `network_mode: host` | the identity provider itself |

No Redis: Authentik ≥ 2025.10 keeps sessions, cache, tasks, and WebSocket state in PostgreSQL.

## Status

| Phase | Status |
|---|---|
| preflight | ✅ implemented (read-only) |
| base / etcd / patroni / haproxy | ✅ implemented (not yet exercised against real hosts) |
| tls — `none` / `self_signed` / `acme` (staging) / `import` | ✅ implemented, tested |
| nginx-keepalived (incl. ACME finalization) | ✅ implemented (configs validated with real `nginx -t` / `keepalived -t`) |
| authentik | ✅ implemented (compose mirrors the production file verbatim) |
| restore | ✅ implemented (optional — skipped unless `restore.sql_file` is set) |
| handoff | ✅ implemented, tested (emits the real ak-monitor schema) |
| `akropolis monitor` | 🚧 stub (will fold in ak-monitor) |
| `site.topology: single` | ✅ implemented — preflight, base, authentik (postgres-in-container), certs (authentik's own Web Certificate, no nginx), restore, handoff, clean. Not yet run against a real VM. |

**The 3-node HA provisioning pipeline is complete** — every phase from preflight to handoff is implemented. First full run against real VMs is the remaining milestone. A single-node topology (no VIP, PostgreSQL as a plain container, intended as a production-fallback instance) is also feature-complete — preflight through clean — and likewise awaits a first real run; see `site.topology` in the configuration reference and the dedicated section below.

## Install

```bash
git clone https://github.com/ktsouvalis/akropolis.git
cd akropolis
python3 -m venv .venv            # Debian/Ubuntu/Mint: apt install python3-venv if missing
.venv/bin/pip install -e .
.venv/bin/akropolis --version
```

Requirements on the **workstation**: Python ≥ 3.10, SSH access to the nodes.
Requirements on the **nodes**: fresh Ubuntu 24.04, a root-capable SSH user, correct interface MTU (1400 on VXLAN overlays, 1500 on flat L2). Everything else is akropolis's job.

## Quickstart

```bash
# 1. answer questions once; they are materialized into a reviewable file
.venv/bin/akropolis init                          # → config.<site>.yml

# 2. read the file. really. this is the review-before-touching-anything step.

# 3. read-only validation of the nodes — safe to run anywhere, changes nothing
.venv/bin/akropolis provision config.<site>.yml --only preflight

# 4. the full pipeline (resumable; each phase asks before applying)
.venv/bin/akropolis provision config.<site>.yml
```

### Live progress

Every long-running operation announces itself *before* it runs: an animated status line in a terminal (`… (ak-node-2) bootstrap: pull + database migrations — 130s / 900s`), a plain `…` line when output is piped. Waits (leader promotion, replica join, backend convergence, container health gates, ACME issuance) tick elapsed/budget in place. Without this, a ten-minute image pull is indistinguishable from a hang; with it, a red ✘ is always preceded by the exact thing that was in flight.

## Commands

```
akropolis init [-o FILE]            interactive wizard → writes config.<site>.yml
akropolis provision CONFIG          run the phase pipeline (resumable)
akropolis provision CONFIG --only PHASE [PHASE...]     run only named phases
akropolis provision CONFIG --replay PHASE [PHASE...]   re-run completed phases
akropolis monitor CONFIG            (stub) will run the monitoring TUI
```

## The phase model

Every phase runs **plan → confirm → apply → verify**:

- **plan** prints exactly what apply will do, before anything happens.
- **confirm** — `lab` sites ask `y/N`; `production` sites require typing the site name. Read-only phases (preflight) skip confirmation. Declining stops the pipeline cleanly.
- **apply** does the work, streaming per-node ✔/✘/⚠ check lines.
- **verify** is a health gate. A phase that applies but fails verify is marked `failed` and **the runner stops** — it never builds on an unhealthy foundation.

Progress is recorded in a per-site state file (see below), so a re-run skips completed phases and resumes at the frontier. `--replay PHASE` marks exactly the named phases pending — everything else keeps its done-skip — and is designed to be a no-op or an explicit, detected change, never a re-bootstrap. Preflight is state-aware: on a mid-lifecycle run, ports, containers and the VIP owned by already-completed phases are expected (the VIP check even inverts once nginx-keepalived is done — answering becomes the healthy state), and residual findings like a low-disk reading or the artifacts of a phase being replayed degrade to warnings. A virgin host gets the full strict treatment.

## Configuration file

One YAML file per site is the single source of truth; `init` is just a convenient way to produce it. See [`config.example.yml`](config.example.yml) for the annotated full reference. The essentials:

```yaml
site:
  name: uop-test            # used in state, prompts, emitted monitor config
  environment: lab          # lab | production (production hardens confirmations,
                            #                   refuses tls provider "none")
  # topology: ha            # ha (default, 3 nodes below) | single (1 node, no VIP,
                            #   PostgreSQL as a container — see "Topology" below)
provision:
  state_file: .state/uop-test.json
  refuse_existing: true     # preflight hard-fails on hosts that already carry a cluster

ssh:
  user: root                # or a sudo-capable user with become: true
  auth: key                 # key | agent | password (password is prompted, never stored)
  key_file: ~/.ssh/id_ed25519
  # become: true — escalation via sudo. Passwordless sudo is used when available;
  # otherwise the sudo password is prompted once per run (fed via sudo -S,
  # never stored). Passphrase-protected keys: use auth: agent (or key — the
  # agent is tried first), so the passphrase never reaches akropolis.

nodes:                      # exactly 3; first is bootstrap_leader by default
  - { name: ak-node-1, ip: 10.99.97.71, bootstrap_leader: true }
  - { name: ak-node-2, ip: 10.99.97.72 }
  - { name: ak-node-3, ip: 10.99.97.73 }

network:
  vip: 10.99.97.70          # must share the nodes' /24 (VRRP needs L2 adjacency)
  interface: ens18
  expected_mtu: 1400        # 1400 for VXLAN overlays, 1500 for flat L2

tls:
  provider: self_signed     # none | self_signed | acme | import
  hostname: auth.example.gr
  # acme:   { directory_url: ..., email: ..., staging: true }
  # import: { fullchain: ./certs/fullchain.pem, privkey: ./certs/privkey.pem }

# network.trusted_proxies: []   # see "Behind an external reverse proxy" below

postgres:
  extra_pg_hba: []          # site-specific lines appended to the generated pg_hba
                            # e.g. "host postgres postgres 10.23.2.50/32 scram-sha-256"
base:
  apt_upgrade: false        # baseline packages are installed either way;
                            # full dist upgrades stay under the operator's patching policy
```

Config is validated in one pass at load: placeholder IPs, VIP/node subnet mismatches, missing key files, and provider-specific requirements are all reported together before anything connects anywhere.

## Phases

### preflight *(read-only)*

Validates all three nodes without changing anything. Safe to run against any host at any time — including production, where it should refuse with "existing cluster artifacts".

Checks per node: SSH reachability and root/sudo, OS release (warn if not Ubuntu 24.04), interface exists with the expected MTU, ≥ 20 GB free on `/`, all 14 required ports free (80, 443, 2379, 2380, 5432, 5000, 5001, 8008, 9000, 9080, 9081, 9300, 9301, 9443), no existing cluster artifacts (`/etc/patroni`, running etcd/haproxy/nginx/authentik containers, enabled keepalived) when `refuse_existing` is set, and an inter-node ping with the DF bit at the expected MTU (catches VXLAN overhead misconfiguration before it becomes a mysterious replication stall). Cluster-wide: clock skew ≤ 5 s, VIP not answering (nothing may own it yet), and DNS for `tls.hostname` resolving to the VIP — a hard failure for `acme`, a warning otherwise.

### base

Guide Step 1. Hostname per node, an akropolis-marker-managed block in `/etc/hosts` (removable and re-runnable), baseline packages + chrony, Docker CE from Docker's own repository, and UFW: default deny incoming, allow ssh/80/443/9000 and all traffic between the three node IPs, then `--force enable` (the ssh rule always lands before enable).

The monitoring host gets its own UFW opening: `monitor.ip` in the site config (or an interactive question, answer pinned in state — Enter to skip) is allowed to `2379,5000,5001,8008,9000,9443/tcp` on every node. ak-monitor is not one of the nodes, so without this rule default-deny silently blanks every dashboard column that isn't plain HTTPS — the polls just time out.

`apt upgrade` is deliberately **not** run unless `base.apt_upgrade: true` — package drift belongs to your patching policy, not the provisioner.

Verify: `docker compose` available, UFW active, chrony running, hostname applied — on every node.

### etcd

Guide Step 2. A 3-node RAFT cluster (quorum 2) as Patroni's DCS, from `gcr.io/etcd-development/etcd:v3.5.30` in Docker with `network_mode: host`.

The initial-cluster token is generated **once** and pinned in the state file, so a re-run can never re-bootstrap a formed cluster. Rendered compose files are pushed with checksum detection; a changed config on a running container triggers `down && up -d` — never `restart`, which does not re-apply volume mounts or network mode.

Verify: `etcdctl endpoint health` OK on all three client URLs and 3 members `started`, with a convergence window for fresh starts.

### patroni

Guide Step 3, and the phase where the bootstrap order makes or breaks the cluster — encoded explicitly instead of hoped for:

1. PostgreSQL 16 (pgdg repo) and the Patroni venv (`/opt/patroni`, `patroni[etcd3]` + `psycopg2-binary`) are installed on **all** nodes, configs and systemd units rendered everywhere — but **nothing starts**. The stock `postgresql` service is stopped and disabled: Patroni owns the lifecycle.
2. Patroni starts on the bootstrap leader **only**, and the phase gates on the REST API answering `200` on `/primary` — the *promoted self to leader* moment — with a 300 s budget. If it never promotes, the phase stops **before** any replica starts.
3. Replicas start one at a time, each gated on reaching `role=replica` with `state=running/streaming` before the next begins.
4. The `authentik` role and database are created on the leader, idempotently.

The rendered `pg_hba` starts with `local all postgres peer` — Patroni's DCS-managed pg_hba **replaces** the Debian default wholesale, and without an explicit `local` line, Unix-socket `psql` as postgres (which step 4 and the verify depend on) dies with a `no pg_hba.conf entry for host ""` FATAL. It further includes the `127.0.0.1/32` replication and rewind entries whose absence has caused silent streaming failures in the field, plus a `host all all <nodes-subnet>/24` line; site-specific lines go in `postgres.extra_pg_hba`. All four passwords (postgres superuser, replicator, rewind, authentik DB) are generated once, pinned in state, and never printed.

Two operational facts worth knowing even with automation: live Patroni settings are DCS-owned after bootstrap (change them with `patronictl edit-config`, not by editing the file), and restarting `patroni.service` on the current **leader** is a real failover, not a no-op.

Verify: `patronictl list` shows exactly 1 Leader + 2 replicas, all running/streaming, **and** the `authentik` role and database exist on the leader — the cluster topology can be perfectly green while step 4 failed, so verify checks both.

### haproxy

Guide Step 5. One HAProxy per node as a pure PostgreSQL connection router — Authentik will always talk to `127.0.0.1:5000` (leader) / `:5001` (replicas), and each HAProxy independently discovers the leader via `GET /primary` on Patroni's REST API. akropolis configures the mechanism; it never hardcodes the answer.

The config carries the WAN-hardened settings: `timeout client/server 1h` (long-lived Django LISTEN connections), TCP keepalives both directions, and `on-marked-down shutdown-sessions` so failover kills stale sessions instead of letting them hang. The stats page on `:9000` gets a password generated once and pinned in state.

Reload semantics follow hard-won learnings: a cfg-only change on a running container is a live `docker kill --signal=HUP` (the file push truncates in place, preserving the bind-mount inode); a compose change is `down && up -d`; an unchanged config is an explicit no-op. A crash-looping container (`Restarting` status) counts as *not* running and takes the full `down && up -d` path.

The config file is pushed with mode **0644** — not 0600, despite carrying the stats password. The official `haproxy` image drops privileges to the unprivileged `haproxy` user (UID 99) before reading its config, so a root-owned 0600 bind-mount is unreadable inside the container and it crash-loops on "Permission denied". The nodes are single-purpose hosts; on-host readability of the stats password is the accepted trade-off (identical to the production deployment's actual file mode). File pushes also converge mode on every run, so a file left at 0600 by an earlier version is repaired even when its content is unchanged.

Verify, two layers: the stats CSV on **every** node must converge to exactly 1 UP server in `pg_primary_backend` and 2 UP in `pg_replica_backend`; then a real `SELECT 1` as the `authentik` user through `127.0.0.1:5000` proves the whole chain — HAProxy routing, Patroni leader, `pg_hba`, credentials.

### tls

Certificate provider abstraction. The only thing that varies is how `fullchain.pem` + `privkey.pem` come to exist; distribution to `/opt/nginx/certs` on all nodes (privkey mode 0600) and verification are identical for all providers.

- **`none`** — testing/local development only; refused for `production` sites at config load. nginx will serve plain HTTP on :80. Note the honest limitation: no secure context means WebAuthn/passkey flows cannot be tested under `none`.
- **`self_signed`** — a 10-year cert generated on the bootstrap leader with SANs covering the hostname, all node names, the VIP, and all node IPs; then distributed. Idempotent: an existing cert that matches the hostname and is valid for > 30 days is kept.
- **`acme`** — any ACME CA via `directory_url` (Let's Encrypt, HARICA ACME, ZeroSSL...). This phase only **stages**: certbot installed on the leader, webroot prepared at `/var/www/certbot`, and a self-signed placeholder put in place so nginx can start and serve the HTTP-01 challenge. Issuance, the multi-node deploy hook, and the cert swap happen after the nginx phase brings nginx up. Start with `staging: true`; flip it after one clean end-to-end run to avoid burning rate limits.
- **`import`** — externally issued certificates, e.g. the HARICA portal flow. Validation happens **on the workstation before anything touches a node**: the private key must match the certificate (SPKI comparison), the SAN must cover the configured hostname (wildcards understood), and the cert must not be expired (≤ 30 days left is a warning). The expiry date is pinned in state so the monitor can alert on it — imported certs have no renewal timer, and pretending otherwise would be worse than saying so.

Verify: cert and key present and cryptographically matching on every node.

### nginx-keepalived

Guide Step 6, with the field learnings applied over the first-draft values. Order is enforced, not hoped for: nginx comes up on all nodes and each node is verified **individually** before keepalived is touched.

**nginx** (`nginx:1.27-alpine`, host network) terminates TLS and load-balances `least_conn` across the three Authentik backends on `:9443`. The config is identical on every node except the `/monitor` location, which returns per-node JSON (`{"node":"ak-node-2","ip":"..."}`) — so `curl -k https://<VIP>/monitor` always tells you which node currently holds the VIP; this is also what the monitoring tool polls. `:8080/nginx_status` serves `stub_status` restricted to loopback, the nodes' subnet, plus any CIDRs in `network.stub_status_allow` — `127.0.0.1` must be allowed explicitly, since an on-node `curl` under host networking arrives from loopback, not a subnet address, and would otherwise be 403'd by its own node. Port 80 redirects to HTTPS with an exception for the ACME challenge path (`alias`, not `root`). With `tls.provider: none`, a plain HTTP `:80` proxy is rendered instead, and it targets Authentik's HTTP listener (`:9080`) rather than `:9443` — the Go router generates absolute `https://` URLs for any request arriving on its TLS listener regardless of `X-Forwarded-Proto`, which behind a plain-HTTP frontend sends the browser to a nonexistent 443 and breaks the UI (assets and the flow executor fail with connection refused). Conf changes on a running container are applied with `down && up -d` — the single-file bind-mount inode trap makes `nginx -s reload` unreliable after a conf push.

**keepalived** (bare-metal systemd) provides the VIP via VRRP with a `chk_nginx` track script (`nc -z localhost 443`, or 80 for provider `none`): track weight **−25** — the learned value that eliminates priority ties under single-failure scenarios — with `fall 2 rise 2`, so a node whose nginx dies sheds the VIP within ~6 s. No-preemption is encoded in keepalived's canonical form: **all instances `state BACKUP` + `nopreempt`** with differing priorities (`network.vrrp.priorities`, default `[100, 90, 80]`). The highest-priority healthy node wins the initial election, and a recovered node never steals the VIP back — one flap per failure, not two. `script_user root` + `enable_script_security` silence the script-security violation. `auth_pass` is silently truncated by keepalived to 8 characters, so exactly 8 are generated and pinned in state.

**ACME finalization** runs at the end of this phase when the tls phase staged it: a dedicated root keypair (`/root/.ssh/id_ed25519_certbot`) is generated on the certbot node and marker-authorized on the others; `certbot certonly --webroot` issues against the configured directory URL (the VIP holder serves the HTTP-01 challenge); an akropolis-managed deploy hook at `/etc/letsencrypt/renewal-hooks/deploy/akropolis-nginx.sh` distributes `fullchain.pem`/`privkey.pem` to the other nodes and reloads nginx everywhere on **every future renewal**; the hook is run once immediately to swap the self-signed placeholder. The cert directory is a directory mount, so in-place file replacement + reload is safe there. With `staging: true` the issued cert is untrusted by browsers by design — flip to `false` and `--replay nginx-keepalived` for the real one. Failed issuance leaves the placeholder in place and stops the phase; nothing breaks, fix DNS/reachability and replay.

Verify: every node's `/monitor` returns its own identity and `stub_status` answers; **exactly one** node holds the VIP on the configured interface; the VIP itself answers `/monitor`. A live failover test (stop nginx on the VIP holder, watch `/monitor` change identity within seconds) is deliberately left as a manual exercise — akropolis will not kill services to prove a point.

### authentik

Guide Step 7. Two execution paths, chosen by inspecting what actually runs, never by assumption:

**Bootstrap** (first deployment): `.env` and compose are rendered on all nodes, but only the bootstrap leader starts — alone — and the phase gates on both its containers reaching Docker-`healthy`, a window that covers the image pull and the full database migration run (budget 15 min). Only then do the other two nodes start, one at a time, each health-gated; they find a migrated schema and come up clean. Three nodes racing migrations against one database is exactly the class of multi-node trouble this cluster has already met once — so it is structurally prevented, not hoped away.

**Rolling** (config or tag change on a running cluster): applied node-3 → node-2 → node-1 — the reverse-order change pattern used in production — with `down && up -d` per node (never `restart`) and a health gate before moving on. Nodes with unchanged config are skipped; a node that fails its gate stops the phase while the remaining nodes are still serving.

The compose file mirrors the production one verbatim: `depends_on: worker: condition: service_healthy` (the 2026.5.0 dual-port-bind race), python3/urllib healthchecks with `start_period: 60s` (the image dropped `curl` in 2026.5.6), the worker as root with the docker socket for outpost management, and `network_mode: host`. Site-specific mounts (branding, locale patches) are not hardcoded — add them via `authentik.extra_server_volumes` / `authentik.extra_worker_volumes`.

The `.env` carries the non-negotiables for this architecture: `AUTHENTIK_POSTGRESQL__HOST=127.0.0.1` (each node talks to its local HAProxy, never a node IP — the lesson of a past incident), and `AUTHENTIK_LISTEN__HTTP/HTTPS/METRICS` moved to 9080/9443/9300 because HAProxy owns 9000 and without the overrides the Go router silently fails to bind while everything looks alive. `AUTHENTIK_SECRET_KEY` (identical everywhere), the akadmin bootstrap password, and the bootstrap API token are generated once and pinned in state; the token is consumed by Authentik's first migration to create akadmin's API credentials, and later handed to the monitor. The worker container additionally overrides `AUTHENTIK_LISTEN__HTTP/METRICS` to 9081/9301: it inherits the shared `.env`, its liveness server binds those listen addresses, and — starting before the server under host networking — it would otherwise squat 9080/9300, leaving the server's binds to fail silently and 9080 answering empty 200s from the worker's liveness endpoint on every path.

**Branding** (`authentik.branding.logo` / `.favicon` / `.background`): paths on the workstation. akropolis uploads them over SFTP to `/opt/authentik/branding/{icons,images}/` on every node and derives the bind-mounts over `/web/dist/assets/{icons,images}/<name>`, matching the production compose. Doing only half of that — an `extra_server_volumes` entry pointing at a file nobody copied — mounts a directory over a missing asset and breaks it, so both halves are derived from one setting. Uploads are checksum-compared, so re-runs neither re-transfer nor churn the compose stack.

Mounting is only half of it. Authentik keeps showing the stock logo until the **brand record** points at the asset, so on a fresh cluster a perfectly correct branding setup looks broken — the file is on every node, the login page is unchanged. After the cluster is healthy, akropolis therefore PATCHes the default brand to `/static/dist/assets/{icons,images}/<name>` (the only absolute prefix brand fields accept). The restore phase re-applies it too, since a dump brings its own brand row, which may point at assets this cluster has never had. A failure here warns and tells you to set it in System > Brands — it never fails the phase, because the cluster itself is fine.

**SMTP email** (`AUTHENTIK_EMAIL__*`): the production `.env` carries a mail block — without it password recovery and email stages silently cannot send. Resolution order per value: `authentik.email` in the site config, else an interactive prompt at apply time whose answer is pinned in state (a `--replay` never re-asks and renders the same file). The SMTP password is never written to the config file: it is prompted with a hidden input once and pinned in state (0600). Set `authentik.email.enabled: false` to get no block and no questions.

Verify: both containers `healthy` on every node, `/-/health/ready/` answers per node, and the API responds to the pinned bootstrap token — proving now the exact credential the monitor will depend on later.

### restore *(optional)*

The migration/cutover move, wired in as a phase between `authentik` and `handoff`: set `restore.sql_file` (plain `.sql` or `.sql.gz`) and the freshly-bootstrapped database is replaced by a `pg_dump` from the old system — real users, providers and flows instead of a blank akadmin install. Without `restore.sql_file` the phase records "skipped" and the pipeline runs through untouched.

When enabled it is destructive by definition, so the order is strict and every step gated: locate the **current** Patroni leader via REST `/primary` (it may not be node-1 by now); check the dump's `SET <guc>` header against the target server's `pg_settings` **before anything destructive** — a dump written by a newer `pg_dump` carries GUCs an older server rejects (`pg_dump` 17 into PostgreSQL 16 emits `SET transaction_timeout = 0;`), and discovering that after the DROP would leave the cluster down on an empty database; those specific header lines are stripped at load time and nothing else is filtered, so `ON_ERROR_STOP` still governs the real content; stop authentik on **all** nodes before touching the database; SFTP the dump to the leader (the base64 push is unusable at dump sizes) and verify sha256 end-to-end; `DROP DATABASE ... WITH (FORCE)` → `CREATE ... OWNER authentik` → `psql -v ON_ERROR_STOP=1` — any error stops the phase with authentik deliberately still down, never half-up on half-data; delete the dump from the node (it contains every secret the IdP holds); then bring authentik back: the **worker starts alone** (`up -d --no-deps worker`) and is gated on `restore.migration_timeout` (default 3600s). This matters — a restored database is not a fresh one, and the worker migrating real data outlasts compose's own dependency wait (`start_period` 60s + `interval` 30s × `retries` 3 ≈ 150s), after which `docker compose up -d` aborts the whole thing with *dependency failed to start* while the migration is running perfectly well. The server follows once the worker is healthy, then the other nodes one at a time — their workers find a migrated schema and come up normally. Whenever a health gate expires, the tail of the relevant container log is printed automatically rather than telling you to go and fetch it. Verify proves the restored schema has tables, `authentik_core_user` is populated, and every node is healthy and ready. The dump's sha256 and timestamp land in state as the paper trail; at real cutover, re-run with the fresh dump via `--replay restore`.

### handoff

The last phase, and the only one that touches nothing on the nodes. It writes the monitoring tool's `config.yml` **on the workstation** (path from `monitor.output`, default `./config.<site>.monitor.yml`, mode 0600), filled entirely from the site config and pinned state: the per-service node groups, ports, SSH log-collection settings, the HAProxy stats and postgres credentials, the Authentik API token that the authentik phase already proved against the live API, and the keepalived `track_weight` (−25) and per-node `base_priority` values exactly as deployed — the monitor computes effective VRRP priorities from these, so config and reality match by construction rather than by discipline. When the tls provider is `import`, the certificate expiry is noted in the emitted file, since no renewal timer exists.

It then prints the landing card: admin URL, the `akadmin` username, and the bootstrap password — shown **once**, in your terminal, because you need it to log in; change it after first login. Pending-ACME and staging-cert conditions are called out on the card if applicable.

Verify parses the emitted file back and asserts the schema: all top-level keys the monitor expects, a non-empty API token, and 3 nodes in every service group.

## Topology: HA vs single-node

`site.topology` picks which phases run. `akropolis init` asks for it up
front and adapts everything downstream (node count, whether it asks for a
VIP, the monitor-IP question, the restore comment footer) — it defaults to
`ha` (the 3-node stack
documented above) and is the only thing most of this README assumes. Setting
it to `single` changes the shape of the pipeline, not just its size:

- **1 node, not 3** — `nodes:` takes exactly one entry.
- **No VIP, no keepalived** — nothing to fail over to. `network.vip` is
  neither required nor validated.
- **No etcd, no Patroni, no HAProxy** — PostgreSQL runs as a plain
  `postgres:16-alpine` container (loopback-only `127.0.0.1:5432`, same
  "always local, never a remote IP" reasoning as an HA node's `127.0.0.1:5000`
  HAProxy connection) instead of a bare-metal Patroni-managed instance.
- **No nginx either.** A single node is meant to stand on its own behind a
  NAT with no port translation (public 443 → this node's 443, unchanged) —
  so unlike `ha`, akropolis doesn't put its own reverse proxy in front.
  authentik's **own core webserver** serves HTTPS directly on 443 instead of
  the usual 9080/9443 offset. It gets there through mechanisms authentik
  already ships: a `certs` directory mounted at `/certs` on the worker
  container (certificate *discovery* — see [authentik's certificate
  docs](https://docs.goauthentik.io/sys-mgmt/certificates/)) and each
  brand's **Web Certificate** field, which akropolis PATCHes via the API —
  the same mechanism already used for the branding logo/favicon.
- **Ordinary bridge networking, not `network_mode: host`.** Unlike every HA
  service, single-node's `server`/`worker` containers are isolated from each
  other and from the host — matching the [reference compose at
  docs.goauthentik.io](https://docs.goauthentik.io/compose.yml). Docker's own
  port publish (`ports: ["443:9443"]` on `server`) maps the host's 443 to the
  container's own default 9443 — never a privileged port from the container's
  point of view, so no `cap_add` or root is needed. PostgreSQL isn't even
  loopback-published; `server`/`worker` reach it by Docker's own DNS
  (`AUTHENTIK_POSTGRESQL__HOST: postgresql`). An earlier version of this used
  `network_mode: host` (copied from the HA cluster without the reason —
  HAProxy routing — that exists for it there) and hit two real bugs before a
  live run surfaced them: the worker inheriting and squatting the server's
  HTTPS port, then the server needing a capability to bind 443 at all. Neither
  is possible once each container has its own network namespace — see
  NOTES.md for the full story.
- **Required free ports on the host** are just `80, 443` — everything else
  (server/worker's own 9000/9443/9300) stays inside their isolated network
  namespaces and never touches the host at all. Port 80 stays free by
  construction — nothing akropolis renders binds it — so certbot can use it
  in standalone mode for ACME issuance and renewal.
- **A different default `authentik.tag`**: `ha` stays pinned to `2026.5.6`
  (2026.8.0 hit a multi-node embedded-outpost restart loop — see
  NOTES.md); `single` defaults to `2026.8.1`, since a single node has no
  multi-node outpost topology to trigger that bug. Set `authentik.tag`
  explicitly to override either default.

Intended use: a fallback instance to bring up quickly if the HA cluster is
down, not a smaller HA cluster. `preflight`, `base`, `authentik`, and `certs`
are implemented and adapt to `single` today.

`authentik` here has no bootstrap-vs-rolling split the way the HA phase
does — with one node, Docker Compose's own `depends_on: condition:
service_healthy` chain (postgresql → worker → server) already prevents the
problem that split exists for on 3 nodes (migrations racing against one
database), so `apply` is just render + `docker compose up -d` + a health
gate. It also resolves the AUTHENTIK_ERROR_REPORTING__ENABLED guide-vs-code
mismatch by making it an explicit `authentik.error_reporting` setting —
config, or asked once and pinned in state, same pattern as `monitor.ip` —
instead of a silent default; this is single-node-only for now, the HA phase
still hardcodes `false`.

`certs` runs *after* `authentik` (it needs a live API to PATCH the brand) and
depends on `tls.provider`: `none`/`self_signed` are a no-op — authentik
already generates and serves its own self-signed certificate on first boot,
so there's nothing to add; `acme` runs certbot in `--standalone` mode (port
80 is free by construction) with a renewal deploy hook that re-copies the
cert and restarts the worker on every future renewal; `import` validates the
provided cert on the workstation first (key↔cert match, SAN coverage,
expiry — the same checks the HA `tls` phase runs) and pushes it to the node.
Either way the cert lands in authentik's discovery folder, the worker is
restarted so discovery runs immediately, and the default brand's Web
Certificate is set to the result.

`handoff` is the same job as the HA version — emit the monitor config, print
the landing card — much smaller by construction: no VIP, no keepalived
priorities, no HAProxy/postgres credentials to hand out (PostgreSQL never
leaves the loopback interface, so a remote monitor couldn't use those
credentials anyway). The admin URL falls back to the node's own IP when
`tls.hostname` isn't set — single-node has no VIP for `ha`'s `tls: none` to
fall back to, and authentik always answers HTTPS here regardless of
provider, so an empty URL was the alternative.

Still missing: nothing from the original list — `restore` and `clean` are
both done. `restore` mirrors the HA phase's shape (dump-vs-server GUC skew
check before anything destructive, SFTP to /tmp, DROP/CREATE/load, dump
deleted after) but is considerably simpler: no Patroni leader to locate
(there is one postgres container, always reached the same way via `docker
exec`), and no ownership-normalisation step — the postgres container's only
superuser IS the app role (`POSTGRES_USER=authentik`), so the HA phase's
"restored objects owned by postgres, not the app role" trap (NOTES.md)
cannot happen here. `clean` reuses the exact same `/opt/authentik` compose
project as `authentik`, so `docker compose down -v` already drops the
containerized postgres's named volume with it — a shorter step list than
`ha`'s, since there's no keepalived/haproxy/patroni/etcd to have ever
existed, and `/etc/letsencrypt` already covers both topologies' certbot
material without a separate step.

## Cleaning a site

`akropolis clean config.<site>.yml` tears the whole stack back down to bare VMs — the inverse of the pipeline, for test iteration. Teardown runs in reverse build order (keepalived/VIP first, so nothing routes traffic at a cluster being dismantled; database before its DCS): keepalived → nginx → authentik → haproxy → patroni + **all** PostgreSQL data → etcd + data → TLS material (letsencrypt, webroot, the certbot distribution key and its authorized_keys line) → UFW reset with ssh re-allowed *before* re-enable → the `/etc/hosts` block → restore-dump leftovers in `/tmp`. Packages (docker, postgresql-16, keepalived, certbot) and the hostname are deliberately left alone — apt state belongs to your patching policy, and removing data and config is what makes the next `provision` honest.

Destruction earns the typed-site-name gate in *every* environment, not just production; `site.environment: production` is refused outright unless `--i-know-this-is-production` is also given. The local state file is archived to `.state/<site>.json.cleaned-<timestamp>` (0600 — the pinned secrets are your paper trail) and removed, so the next provision regenerates every secret and preflight's `refuse_existing` passes on a genuinely blank slate. Cleaning a half-built node — exactly what a failed provision leaves behind — is a supported case: every step is idempotent.


## Configuration reference

`config.example.yml` is the reference: every key akropolis reads appears there, commented out when optional. That is enforced rather than promised — `python3 tools/audit_config_keys.py` fails if the code reads a key the example never mentions. It exists because `base.apt_upgrade` and `network.trusted_proxies` were documented in this README and implemented in code but missing from the example, which meant that in practice nobody could find them.

## Behind an external reverse proxy

If public TLS is terminated upstream — e.g. a Traefik instance holding a HARICA wildcard, with the cluster reachable only through it — the intended setup is `tls.provider: self_signed`: the internal VIP serves a self-signed cert, and the external proxy forwards to it with certificate verification disabled. Preflight's DNS→VIP check is warn-only for `self_signed`, so a public hostname that resolves to the proxy rather than the VIP does not block.

The part that is easy to get silently wrong in this topology is client identity: without special handling, the cluster's nginx overwrites `X-Real-IP` with the proxy's address and `X-Forwarded-Proto` with its own scheme, so Authentik sees every login as coming from the proxy — poisoning reputation policies, event logs, and GeoIP. Declare the proxy instead:

```yaml
network:
  trusted_proxies:
    - 192.168.20.20        # IPs or CIDRs of the upstream proxy
```

When set, nginx trusts `X-Forwarded-For` from those addresses (`set_real_ip_from` + `real_ip_recursive`), so real client IPs reach Authentik, and the original `X-Forwarded-Proto` is passed through instead of overwritten (falling back to the local scheme when the header is absent, so direct internal access still works). Only list addresses you actually control — a trusted proxy can assert any client IP it likes.

## State file & secrets

Each site gets a JSON state file (default `.state/<site>.json`, mode 0600) recording phase completion and **pinned generate-once values**: the etcd initial-cluster token, all PostgreSQL passwords, the HAProxy stats password, TLS metadata. Pinning is what makes re-runs safe — a completed bootstrap can never be re-bootstrapped, and a re-render can never rotate a password out from under a running cluster. The state file refuses to load for a different site name than the one it was created for.

Security posture, stated plainly:

- The site config file contains **no secrets** and is safe to commit (git-ignored by default anyway, except the example).
- SSH passwords, when used, are prompted at runtime and never stored.
- Generated secrets currently live **in plaintext** inside the state file. That is acceptable for lab work and is flagged as a hard requirement to fix (age/sops encryption or OS keyring) before production use. Treat `.state/` accordingly: it is git-ignored, keep it that way.
- Imported TLS private keys transit through workstation memory during validation and distribution; they are written only to `/opt/nginx/certs/privkey.pem` (0600) on the nodes.

## Roadmap

first real run of both topologies against actual VMs → fold in the monitoring TUI as `akropolis monitor` (including teaching it single-node's smaller schema) → encrypted state secrets.

## License

MIT.
