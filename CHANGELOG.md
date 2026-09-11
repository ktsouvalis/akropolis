# Changelog

All notable changes to akropolis are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions before 1.0.0 were developed in-tree without git tags. They are
summarised below by theme rather than reproduced commit by commit; `git log`
carries the full sequence, and NOTES.md carries the reasoning, including the
dead ends.

## [Unreleased]

## [2.0.0] - 2026-09-11

### Added

- `akropolis monitor` now understands `site.topology: single` — previously
  the dashboard was entirely HA-shaped (ported from the standalone
  akropolis-monitor project) and single-node sites hit hard errors
  (`Invalid URL 'https://:443/...': No host supplied`) from worker/task-queue
  checks that hardcoded the VIP as their probe host, plus permanently-stuck
  "Checking..." panels for services (HAProxy, etcd, Patroni, keepalived)
  that don't exist on that topology. Single now gets its own 4-panel layout
  (Authentik, Workers, Worker Queue, Nginx), probing the node directly
  instead of a nonexistent VIP. Worker-connection matching also no longer
  falsely reports the one node as missing: it used to compare the
  configured node name against the connected worker's Docker-assigned
  container hostname, which can never match.
- Single topology now provisions its own reverse proxy: a bare-metal
  (systemd, not containerized) nginx in front of Authentik, terminating
  public TLS and serving the same bilingual maintenance page the HA
  topology already shows whenever Authentik is unreachable — including
  during `akropolis shutdown`, which previously left single-node sites with
  a bare connection-refused instead of a maintenance page. New phase
  `nginx_single_phase.py` (pipeline name `nginx`) replaces the old `certs`
  phase.

### Changed

- Single topology's Authentik container no longer publishes its HTTPS
  listener to host port 443 directly; both of its own listeners (9443
  https, 9000 http) are now loopback-only, with nginx as the only thing the
  public reaches. `tls.provider: none` on single is now genuinely plain
  HTTP end-to-end (previously it still got HTTPS via Authentik's own
  auto-generated certificate) — matching the HA topology's `none` semantics.
- `restore` on single topology no longer has to re-apply a certificate
  after loading a dump: TLS moved out of the database entirely (see above),
  removing a fragility class where a restored dump could silently revert
  the node to a self-signed certificate.

### Migration note

An already-provisioned single-node site must run
`akropolis provision <config> --replay authentik` before the new `nginx`
phase runs for the first time, since the compose file's port mapping
changes. The old `certs` phase's entry in the site's state file is now
inert and can be ignored.

## [1.7.0] - 2026-09-10

### Added

- `akropolis licenses`: print a third-party license listing for whatever is
  actually importable in the running copy right now — installed
  dependencies for a pip install, or the packages bundled inside the
  archive for a zipapp binary. Shares its row/render logic
  (`akropolis/licenses.py`) with the `THIRD_PARTY_LICENSES.md` that
  `tools/build_pyz.sh` generates for each release, so the two listings
  can't drift apart.

## [1.6.0] - 2026-09-10

### Added

- `akropolis whats-new`: print the CHANGELOG.md entry for the installed
  version (`--version` for another one, `--all` for the full file). The file
  now ships inside the package/zipapp (`akropolis/CHANGELOG.md` symlinks to
  the repo-root copy so there is still only one file to keep current).

## [1.5.0] - 2026-09-10

### Added

- `akropolis check-update`: force a fresh (cache-bypassing) check against
  GitHub and report a definite up-to-date/update-available answer, unlike
  the passive per-command nudge which is throttled and silent when current.
  Exits 1 when an update is available, for use in scripts/cron.
- `site.config_version`, required in every site config now. Previously a
  release that changed what the config file needed to say (a renamed key, a
  new required field, a default that would silently mean something
  different) had nothing to enforce that an existing config actually got
  updated — `load()` would either misbehave silently or fail with an error
  that didn't explain why. `load()` now refuses to run unless
  `site.config_version` matches `CONFIG_SCHEMA_VERSION` (akropolis/config.py),
  and says what to do instead of guessing: check CHANGELOG.md for what
  changed between the two versions, update the file, then bump the number.
  Existing configs need `config_version: 1` added by hand; `init` and
  `config.example.yml` already carry it.

## [1.4.0] - 2026-09-10

### Added

- `akropolis monitor` now runs the real-time cluster health dashboard
  (previously a stub telling you to install `akropolis-monitor` separately),
  and a new `akropolis logs` command runs the cluster-wide log viewer over
  SSH, including `--save FILE` to write a plain-text report instead of the
  TUI. Both are folded in from [akropolis-monitor](https://github.com/ktsouvalis/akropolis-monitor)
  and take the `config.<site>.monitor.yml` the `handoff` phase already
  emitted, not `config.<site>.yml`. `ha` topology only for now — the ported
  code has no concept of `single` yet, even though `handoff` has emitted a
  single-node-shaped monitor config since the single-node topology landed.
- New dependencies (`textual`, `requests`, `urllib3`, and optionally
  `psycopg2`/`python3-psycopg2` for the PostgreSQL replication-slot panel)
  are bundled the same way paramiko's are: pure-Python ones ship inside the
  zipapp, `psycopg2`'s compiled parts come from the system.

## [1.3.0] - 2026-09-10

### Added

- The `authentik` phase and the `restore` phase now self-heal a bootstrap
  API token left dead by a database restore (in-phase, or done out of band
  and picked up via `--replay authentik`): before failing, `verify` re-mints
  the token already pinned in state into the live database through `ak
  shell` — the same recovery `restore` (single-node) already did. The token
  value never changes, so an existing monitor config keeps working with no
  manual `.state/<name>.json` edit and no `--replay handoff` needed.

## [1.2.0] - 2026-09-10

### Added

- `akropolis shutdown` and `akropolis start`: gracefully pause and resume
  just the authentik `server`+`worker` containers on an already-provisioned
  site. On `ha`, the rest of the stack (etcd, Patroni/PostgreSQL, HAProxy,
  nginx, keepalived) stays up — the VIP keeps answering. On `single`, the
  `postgresql` container sharing the same compose project is explicitly
  left running. `start` refuses to run unless the last `shutdown` completed
  gracefully, and clears that flag again on success, so a stray `start`
  can't silently no-op against a cluster nobody deliberately paused.
- On `ha`, nginx now serves a bilingual (Greek/English) maintenance page
  whenever every authentik backend is unreachable (502/503/504) — from a
  deliberate `shutdown` or an actual outage — instead of its bare error
  page. The real HTTP status code is preserved, so uptime/alerting
  monitoring is unaffected.

## [1.1.0] - 2026-09-10

### Added

- Every command now checks GitHub for a newer release (cached 24h, silent on
  network failure) and prints a notice if one's available. `akropolis update`
  downloads the latest release binary, verifies it against `SHA256SUMS`, and
  replaces the running zipapp in place.
- The `authentik` phase's plan now warns when `authentik.tag` differs from
  the tag it last successfully applied, and the README documents the
  upgrade path (`--replay authentik` after bumping the tag) along with what
  akropolis does *not* do for you: take a backup, or check Authentik's own
  version-skip rules.

## [1.0.3] - 2026-09-10

### Fixed

- The HA cluster's ACME finalization (`nginx-keepalived` phase) ran certbot
  with `--keep-until-expiring` and never `--force-renewal`, so rehearsing
  with `tls.acme.staging: true` and then replaying with `staging: false`
  left the staging certificate in place: certbot decides whether to reissue
  purely on the existing lineage's validity, doesn't care which CA issued
  it, and exits 0 either way, so the phase reported success while
  redistributing the same untrusted cert to every node. The single-node
  `certs` phase already read the existing certificate's issuer and forced
  reissue on a staging↔production mismatch; that check is now shared by the
  cluster path, so `--replay tls` followed by `--replay nginx-keepalived`
  reliably swaps a staging cert for a production one.

## [1.0.2] - 2026-09-07

v1.0.1's published binary bundled paramiko, Jinja2, PyYAML, and rich as
source without ever verifying their license obligations were met — see
NOTES.md. Re-cutting v1.0.1 in place was considered and rejected: this
changes the artifact's actual bytes, and anyone who already downloaded
and checksum-verified v1.0.1 deserves that tag to keep meaning what it
meant. A patch version is the right size for a no-behavior-change fix.

### Added

- `THIRD_PARTY_LICENSES.md`, generated at build time from the bundled
  dependencies' own metadata and published alongside the `akropolis` binary
  on every release, indexing each vendored package's declared license and
  where its full license text lives inside the archive. paramiko (LGPL-2.1)
  is the one bundled dependency that isn't permissively licensed; the
  README's architecture table now says so.

### Fixed

- The documented install line used `curl -LO`, which saves an HTTP error page
  under the target filename instead of failing. Following the README while a
  release was still publishing produced a `chmod +x`'d copy of GitHub's 404
  page and the cryptic `/tmp/ak: line 1: Not: command not found`. Now `-fLO`,
  in both the README and the release-notes boilerplate.
- The zipapp build's dist-info pruning kept only `METADATA` and
  `entry_points.txt`, on the assumption that every wheel ships its license
  text under `dist-info/licenses/` (below the prune's `maxdepth 2`, so
  untouched). `mdurl` 0.1.2 doesn't: its `LICENSE` sits directly in
  `dist-info/`, so it was being silently deleted from every release
  artifact. The filter now also keeps `LICENSE*`/`LICENCE*`/`COPYING*`/
  `NOTICE*`/`AUTHORS*` wherever they sit.

## [1.0.1] - 2026-09-07

Packaging fixes found by checking the 1.0.0 release artifact against a local
build instead of assuming they matched. They did not.

### Fixed

- The build embedded the absolute path of the source tree in
  `direct_url.json`, so no two machines could ever produce the same artifact.
  The vendored `.dist-info` directories are now pruned to `METADATA`,
  `entry_points.txt` and license texts — dropping `direct_url.json`, `WHEEL`
  (which carries the interpreter tag of the downloaded wheel) and `RECORD`
  (which hashes console scripts whose shebang is the build machine's
  interpreter path). `METADATA` has to stay: paramiko resolves its own version
  through `importlib.metadata` at import.
- The release workflow only triggered on tag push, and a tag pushed in the
  same operation that first registers a workflow is evaluated before that
  workflow exists — the 1.0.0 tag produced no run at all. Added
  `workflow_dispatch`, guarded so it refuses to run against anything but a
  tag: `gh workflow run release --ref v1.0.1`.

### Changed

- `tools/build_pyz.sh` warns when the build interpreter is not 3.10, and
  `BUNDLE-MANIFEST.txt` records which interpreter produced the file. The
  bundled set is genuinely interpreter-dependent — `rich` requires
  `typing_extensions` only below 3.11 — so a 3.12-built artifact omits it and
  fails on a 3.10 host. Reproducibility is per commit *and* per Python minor
  version; the README no longer claims otherwise.

## [1.0.0] - 2026-09-07

First tagged release. Both topologies have completed real end-to-end
provisioning runs against live hardware, which is what 1.0.0 is claiming — not
that the tool is finished.

### Added

- Single-file distribution. `tools/build_pyz.sh` produces `dist/akropolis`, a
  ~2.3 MB executable zipapp carrying akropolis and its pure-Python
  dependencies. No install step, no virtualenv, no root. Builds are
  reproducible: the same commit yields a byte-identical file.
- `BUNDLE-MANIFEST.txt` inside the artifact, listing every bundled package and
  version, so the dependency surface of a deployed binary can be audited
  without unpacking it.
- Tag-triggered release workflow. Pushing `v*` builds the zipapp, wheel and
  sdist, checksums them, and publishes a GitHub release.

### Fixed

- `akropolis/templates/` was an implicit namespace package. Template loading
  goes through `importlib.resources.files()`, which resolves a namespace
  package to a `MultiplexedPath` — fine on disk, but `NotADirectoryError` as
  soon as the package lives inside a zip. Every template read failed in the
  single-file build. Adding `templates/__init__.py` gives it a real loader.

### Notes

- The zipapp deliberately does not bundle `cryptography`, `bcrypt` or `nacl`.
  zipimport cannot load compiled extension modules, and freezing a crypto
  library inside a release artifact is the wrong posture for a tool that
  provisions identity infrastructure. They come from
  `apt install python3-cryptography python3-bcrypt python3-nacl`.
- No `.deb` is published. It was evaluated and deferred; see NOTES.md for the
  full comparison, including the paramiko floor question it turns on.

## Pre-1.0 development

### The HA pipeline (v0.2.0 – v0.7.0)

Init wizard, resumable phase runner, and the full 3-node stack: preflight,
base, etcd, Patroni, HAProxy, TLS, nginx/keepalived, authentik, handoff. The
TLS phase carries a provider abstraction (`none` / `self_signed` /
`acme-staging` / `import`); the authentik phase gates bootstrap on migrations
and does rolling updates.

### Operational hardening (v0.7.1 – v0.8.2)

`network.trusted_proxies` for external reverse proxies; sudo-with-password so
NOPASSWD is not a prerequisite; clock-skew detection; `--replay` scoped to
named phases; SMTP configuration pinned in state; live per-step progress;
monitor-host UFW openings. Several fixes here came from production drift
observed on the live cluster rather than from testing — notably the worker
needing its own listen ports so the server actually owns 9080, and health
gates comparing exactly, since `healthy` is a substring of `unhealthy`.

### Restore, clean, and branding (v0.9.0 – v0.11.5)

Restore a `pg_dump` into the current Patroni leader, with GUC-skew checks
before anything destructive and object re-ownership afterwards. `clean` tears
a site down to bare VMs in reverse build order behind a typed site-name
confirmation. Branding asset upload, with the default brand actually pointed at
the uploaded files. `tools/audit_config_keys.py` began enforcing that every
config key the code reads appears in `config.example.yml`.

### Single-node topology (v0.12.0 – v0.16.0)

`site.topology` splits the pipeline. Single-node runs PostgreSQL in a
container, drops etcd/Patroni/HAProxy/keepalived entirely, and drops nginx too
— authentik's own webserver terminates TLS, so the certs phase talks to
authentik's Web Certificate API instead of rendering a cert directory.

### Convergence (v0.16.1 – v0.16.13)

The single-node bugs that only a real run surfaces: the worker squatting the
server's 443, then `CAP_NET_BIND_SERVICE`, then the realisation that dropping
`network_mode: host` made both problems structurally impossible. Optional
phases skip instead of halting the pipeline. Per-run command/output transcripts
with best-effort secret redaction. ACME staging no longer forced. Both
topologies marked tested.
