# akropolis phases: what each does and what to watch

Read this when planning a run, explaining a plan to the operator, or deciding
between `--only` and `--replay`.

## Shared by both topologies

### preflight (read-only, no confirmation)
- Per node: SSH and root/sudo, OS (Ubuntu 24.04 expected), interface and MTU,
  ≥20 GB free on `/`, required ports free, and no existing cluster artifacts
  when `refuse_existing` is set.
- HA also checks DF-bit ping between nodes at the expected MTU, clock skew of
  at most 5 s, and that the VIP is not answering yet.
- DNS for `tls.hostname` must resolve to something. This is a hard failure
  for `acme` and a warning otherwise. It does **not** have to resolve to the
  VIP, because NAT and upstream proxies are legitimate.
- It is state-aware: footprints of completed phases are expected.

### base
- Hostname, an `/etc/hosts` marker block, baseline packages, chrony, and
  Docker CE.
- UFW: deny incoming; allow ssh, 80, 443 (plus HA ports between nodes).
- `monitor.ips` opens the monitoring ports to the monitor host(s). It is
  asked once and pinned if unset.
- `apt upgrade` runs only with `base.apt_upgrade: true`. unattended-upgrades
  is masked unless `base.unattended_upgrades: true`.

### restore (optional)
- Runs only with `restore.sql_file` set. Declining records it as skipped.
- It checks the dump's `SET` GUCs against the target server **before** any
  DROP, stops Authentik everywhere, uploads via SFTP with a sha256 check, runs
  `DROP DATABASE ... WITH (FORCE)`, CREATE and an `ON_ERROR_STOP` load,
  re-applies object ownership to the app role, and deletes the dump from the
  node.
- The worker starts alone, gated on `restore.migration_timeout` (default
  3600 s), because migrating real data outlasts compose's dependency wait.
- Verify performs a real **write** as the app role and checks that
  `authentik_core_user` is populated.
- Branding is re-applied. On single, the bootstrap API token is re-minted
  through `ak shell`.

### handoff (read-only, no confirmation)
- Writes `config.<site>.monitor.yml` (0600), always with
  `verify_tls: false`.
- Prints the admin URL and the akadmin bootstrap password **once**; it must be
  changed after first login.

## HA only

- **etcd:** a 3-member raft cluster. The initial-cluster token is pinned.
  Config changes use `down && up -d`, never `restart`.
- **patroni:** installs everywhere and starts nothing. The leader starts
  alone and is gated on `/primary` returning 200 (300 s budget). Replicas join
  one at a time until they are streaming. Then the `authentik` role and
  database are created. The rendered pg_hba starts with
  `local all postgres peer`; extra lines go in `postgres.extra_pg_hba`.
- **haproxy:** routes `127.0.0.1:5000` to the leader and `:5001` to replicas
  via Patroni's REST. Config is mode **0644** on purpose, because the
  container runs as UID 99. Verify checks 1 UP primary and 2 UP replicas, then
  a real `SELECT 1` as `authentik`.
- **tls:** one of `none`, `self_signed`, `acme` or `import`. `acme` only
  stages here; issuance happens in nginx-keepalived. `import` validates the key
  match, SAN coverage and expiry on the workstation first.
- **nginx-keepalived:** nginx comes up on all nodes and each is verified
  before keepalived starts. VRRP uses all-BACKUP with nopreempt, a track
  weight of −25, and an 8-character auth_pass. ACME issuance runs here with a
  deploy hook that distributes renewals to every node. Verify checks that
  exactly one node holds the VIP.
- **authentik:** on bootstrap, the leader starts alone and passes a health
  gate (15 min budget) before the others start one at a time. On a rolling
  update the order is node-3 → node-2 → node-1. Listen ports are moved to
  9080/9443/9300 (worker 9081/9301) because HAProxy owns 9000.

## Single only

- **authentik:** compose with bridge networking and a postgres container.
  Server published on loopback 9443/9000 only. `authentik.error_reporting`
  is asked once and pinned.
  - Backup tooling: `backup.localhost: true` publishes Postgres on
    `127.0.0.1:5432` (reach it through an SSH tunnel).
  - Alternatively, `backup.ips` publishes it to specific hosts, enforced in
    the DOCKER-USER chain.
- **nginx (bare-metal):** terminates TLS, proxies to `127.0.0.1:9443`, and
  serves the maintenance page on 502/503/504. ACME uses certbot webroot and
  forces a reissue when the current cert's issuer doesn't match the run (for
  example, staging to production).

## Lifecycle and maintenance commands

- `shutdown` stops the Authentik server and worker. On HA, other services keep
  running; on single, postgres and nginx do. `start` requires a prior graceful
  shutdown.
- `clean` tears down in reverse build order to bare VMs and keeps packages.
  The state file is archived as `.cleaned-<timestamp>`.
- `ldap-reconcile` runs `ak shell` in the worker over SSH and reuses
  Authentik's stored LDAP bind. Exit code 2 means no LDAP source exists or
  `--source` matched nothing.
