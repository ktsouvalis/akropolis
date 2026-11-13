---
name: akropolis
description: Operate akropolis (the `akro` CLI), which provisions an Authentik identity provider over SSH, either as a 3-node HA cluster (Patroni/etcd/HAProxy/nginx/keepalived VIP) or as a single node. Use for planning, reviewing config, running or resuming provisioning, Authentik upgrades, database restore/cutover, branding/email templates, LDAP identifier reconciliation, shutdown/start, teardown, monitoring and troubleshooting akropolis-managed sites.
---

# akropolis

akropolis turns fresh Ubuntu 24.04 hosts into an Authentik identity provider
and runs from the operator's workstation. It has two topologies, set by
`site.topology`:

- **`ha`** (default): 3 nodes. PostgreSQL 16 under Patroni with etcd as DCS,
  per-node HAProxy routing to the leader, nginx TLS termination, and a
  keepalived VRRP virtual IP.
- **`single`**: 1 node, meant as a fast fallback instance. Postgres runs as a
  container, there is no VIP, and nginx runs bare-metal in front of Authentik
  on loopback.

Upstream: https://github.com/ktsouvalis/akropolis. The README of the
installed version is the authority, and `NOTES.md` explains the reasoning
behind each design decision. This skill describes behaviour as of akropolis
2.10.1; if `akro --version` is newer, read `akro whats-new` before relying on
details here.

## First: establish the deployment context

This skill is organisation-agnostic. Never assume hostnames, IPs, domains,
VIPs, site names or paths. Before doing anything site-specific:

1. Find the site config, `config.<site>.yml`. Ask the operator which machine
   and directory hold it; it is often a management host, not the operator's
   laptop. The state file and transcripts sit next to it.
2. Read it in full. Note `site.topology`, `site.environment`,
   `site.config_version`, `tls.provider`, `authentik.tag`, and the node list.
3. Run `akro status config.<site>.yml` (local only, no SSH).
4. Check the version: `akro --version`, and `akro check-update`.
   `config_version` must match what the installed release expects, or `load()`
   refuses. When it doesn't, read the CHANGELOG before changing anything.

If there is no config yet, the task is a new site. Start with `akro init`.

## How akropolis works

Every phase runs **plan → confirm → apply → verify**. Lab sites confirm with
y/N; production sites require typing the site name. A phase that fails verify
is marked `failed` and the pipeline stops. Re-runs resume at the frontier.

- HA pipeline: `preflight` → `base` → `etcd` → `patroni` → `haproxy` → `tls`
  → `nginx-keepalived` → `authentik` → `restore` (optional) → `handoff`
- Single pipeline: `preflight` → `base` → `authentik` → `nginx` → `restore`
  (optional) → `handoff`

`--only PHASE...` runs only those phases; `--replay PHASE...` re-runs
completed ones. See `references/phases.md` for details.

## Rules for the agent

- **The operator runs interactive commands.** `provision`, `shutdown`,
  `start`, `clean`, `init` and `ldap-reconcile` prompt for confirmations,
  passwords and per-user y/N. Prepare the exact command and let the operator
  run it in their terminal (in Claude Code they can prefix it with `!`). Never
  automate a confirmation or the typed-name gate.
- **Preflight first.** Run `akro provision CONFIG --only preflight` before any
  apply on a new or changed site. It is read-only and safe on production,
  where it should refuse with "existing cluster artifacts".
- **Treat the state file as a secret store.** `.state/<site>.json` holds
  plaintext secrets: Authentik secret key, akadmin password, API token, all
  Postgres passwords, the HAProxy stats password, the etcd token and the VRRP
  pass. Never print, paste, commit or upload it. Use `akro status`.
- **Pinned values are set once.** The etcd token and generated passwords are
  never rotated by a replay. That is what makes re-runs safe.
- **Back up before upgrades or restores.** Authentik migrations have no
  rollback, and akropolis does not take a backup for you. Use `pg_dump`
  against the Patroni leader (ha) or the `postgresql` container (single).
- **Respect the upgrade path.** akropolis renders whatever `authentik.tag` it
  is given. Check Authentik's release notes for required intermediate
  versions. On `ha`, don't move past the tag the installed release pins
  without reading NOTES.md: some versions broke multi-node embedded outposts.
- **Patroni owns PostgreSQL on HA.** Live settings are changed with
  `patronictl edit-config`, not by editing files. Restarting `patroni` on the
  leader is a real failover.
- **Don't hand-edit what akropolis renders** (compose files, `.env`, nginx,
  haproxy, keepalived, patroni configs). Change the config and replay the
  phase.
- **Destructive commands need explicit intent:** `clean`, `restore` (it drops
  and recreates the Authentik database), and replays on a live production
  site. Restate what will be lost and confirm before preparing the command.

## Common tasks

**Install the CLI**
```bash
sudo apt install python3-cryptography python3-bcrypt python3-nacl
curl -fLO https://github.com/ktsouvalis/akropolis/releases/latest/download/akro
chmod +x akro && ./akro --version
```
`python3-psycopg2` is optional; it is only used by one `monitor` panel.
Upgrades: `akro update` (zipapp only; use sudo if installed in
`/usr/local/bin`).

**New site.** `akro init`, review the file, `--only preflight`, then the full
`akro provision`. On HA, check MTU (1400 for VXLAN overlays, 1500 for flat
L2), L2 adjacency for VRRP, and that the VIP is free. For ACME, rehearse with
staging and then flip to production (see troubleshooting).

**Upgrade Authentik.** Back up first. Bump the quoted `authentik.tag`, then
`akro provision CONFIG --replay authentik`. HA rolls node-3 → node-2 → node-1
with a health gate on each; a failing node stops the roll while the others
keep serving.

**Restore or cut over from an old system.** Set `restore.sql_file` (`.sql` or
`.sql.gz`). The restore phase runs after `authentik`. For a fresh dump at the
real cutover, use `--replay restore`. Branding and, on single, the API token
are re-applied automatically.

**Branding and email.** Set `authentik.branding.logo|favicon|background`
(workstation paths; use a PNG logo for emails) and
`authentik.custom_templates` (a directory mirrored to every node, for example
`email/password_reset.html`). Then `--replay authentik`.

**Behind an external reverse proxy.** Use `tls.provider: self_signed` and list
the proxy in `network.trusted_proxies` so real client IPs reach Authentik.

**LDAP users stuck after their entryUUID changed.**
`akro ldap-reconcile CONFIG [--source SLUG]`. It is read-first and asks y/N
per user before repointing the stored identifier. It never merges or creates
users.

**Maintenance window.** `akro shutdown CONFIG` stops Authentik and nginx
serves a maintenance page. `akro start CONFIG` refuses without a prior
graceful shutdown.

**Monitoring.** `akro monitor config.<site>.monitor.yml` and
`akro logs config.<site>.monitor.yml [--last H] [--level L] [--save FILE]`.
These work on both topologies and take the **monitor** file from
`handoff`. The SSH user must be able to read Docker on each node; otherwise
`akro logs` reports `cannot read logs` for that service.

**Tear down.** `akro clean CONFIG` (typed-name confirmation; production also
needs `--i-know-this-is-production`). It removes all data and config but
leaves packages. The state file is archived, so the next provision generates
fresh secrets.

## Troubleshooting

See `references/troubleshooting.md` for known failure modes: Patroni never
promoting, HAProxy permission crash-loops, WebAuthn under `tls: none`, ACME
staging certificates that stick, restore version skew and migration timeouts,
red monitor panels on healthy clusters, and port squatting between server and
worker.
