# Development notes

Bugs found, and lessons learned, while building and testing akropolis
against real clusters. Kept here so a fixed mistake doesn't get repeated in
a later phase, or reintroduced in a future rewrite.

Format varies by entry: some are terse fix logs, some carry an explicit
rule for the codebase. All dates are when the bug was found, not necessarily
when it was fixed.

---

## "Reproducible" was a claim, not a fact (Sep 2026)

The 1.0.0 README said the build was reproducible. Checked it properly after
the release published: built locally, hashed, compared against `SHA256SUMS`
from the runner. Different. Downloaded the published artifact and diffed it
entry by entry against a local build rather than guessing why.

823 entries on the runner, 815 locally. Ten shared entries differed. Every
difference traced to one of two roots:

**The build path.** `pip install --target` writes `direct_url.json` into the
dist-info of anything installed from a local directory:

```
{"dir_info": {}, "url": "file:///home/runner/work/akropolis/akropolis"}
```

Two machines cannot agree on that, ever. Fixed by pruning the vendored
dist-info directories to `METADATA`, `entry_points.txt` and license texts.
`METADATA` is the one that cannot go: paramiko resolves its own version
through `importlib.metadata` at import. `WHEEL` and `RECORD` went with it:
`WHEEL` records the interpreter tag of the wheel pip chose (`cp310` vs
`cp312` for markupsafe and pyyaml, which ship compiled variants), and `RECORD`
hashes the generated console scripts in `bin/`, whose shebang is the build
machine's interpreter path, so they differed even though we delete `bin/`.

**The interpreter.** This one cannot be fixed, only documented. The runner
build carried `typing_extensions` 4.16.0; the local build did not. `rich`
declares it conditionally for Python < 3.11. So the artifact's *contents*
depend on the interpreter that built it, not only on the commit.

That last point retroactively justifies a decision made on general caution
when writing the workflow: build on 3.10, the `requires-python` floor, rather
than the newest available. The reasoning at the time was that a newer
interpreter might resolve a wheel that requires it. The actual failure is
worse and more specific: a 3.12-built artifact silently *omits* a package a
3.10 host needs, so it imports fine everywhere it was built and dies on the
oldest supported target. Had the release been cut on 3.12, it would have
worked on every machine we tested and failed on the one host that mattered.

The build script now warns when the interpreter is not 3.10 and
`BUNDLE-MANIFEST.txt` records which one built the file, so a mismatched
checksum is self-explaining rather than alarming.

The general lesson is narrower than "verify your claims": a reproducibility
claim is only meaningful with its conditions attached. Same commit is not
enough. Same commit, same interpreter minor version, and no build-host state
in the artifact.

---

## Packaging: a single file, not a .deb (Sep 2026)

For 1.0.0 the question was how to ship this to an Ubuntu server inside the
unit so several admins can run it. Three candidates.

**A .deb depending on archive packages.** Smallest artifact (~150 KB),
`apt remove` works, the version is visible to dpkg, and dependencies resolve
themselves. It turns on one fact: Ubuntu 24.04 ships `python3-paramiko`
**2.12.0**, and `pyproject.toml` declares `paramiko>=3.4`. So a pure-archive
.deb requires lowering that floor.

Checked whether that would be honest. `sshexec.py` uses `SSHClient`,
`AutoAddPolicy`, `load_system_host_keys`, `connect(key_filename=...)`,
`exec_command(timeout=)`, `stdin.channel.shutdown_write()` and
`open_sftp().put(callback=)`. All present and behaviourally identical in
2.12; RSA-SHA2 and OpenSSH-format Ed25519 keys landed back in 2.9. So
`paramiko>=2.12` would be a truthful floor, and the .deb is viable.

**A self-contained binary (PyInstaller).** No Python needed at all, but
glibc-pinned to the build host, ~20 MB, opaque to security updates, and it
re-extracts to /tmp on every run.

**A zipapp.** One executable file, ~2.3 MB, no install step, no root, runs on
any CPython >= 3.10 regardless of architecture.

Chose the zipapp. The .deb's advantages are real but only start paying once
the tool is installed on enough machines that lifecycle management matters,
and it costs a second build path to keep in sync. The floor stays at
`paramiko>=3.4`: there is no reason to weaken a declared dependency for a
package we are not building. If the .deb is ever wanted, this entry is the
prerequisite work: lower the floor to 2.12, `Architecture: all`,
`Depends: python3 (>= 3.10), python3-paramiko, python3-yaml, python3-rich,
python3-jinja2`.

What the zipapp cannot carry is compiled extension modules: `zipimport` has
no mechanism to load a `.so` out of a zip. That rules out `cryptography`,
`bcrypt` and `nacl`, which come from apt instead. This was going to be a
footnote about a limitation, but it is the better arrangement on its own
merits: a tool that provisions identity infrastructure should not be shipping
a frozen copy of `cryptography` that nobody re-cuts for six months. PyYAML's
`_yaml` and MarkupSafe's `_speedups` are stripped for the same reason and fall
back to pure Python automatically, slower, and irrelevant at the volume of
YAML and templating this does.

Two build details that cost time and are not obvious:

- The vendored `.dist-info` directories must survive. The first build deleted
  them as dead weight and paramiko 5.0 died at import with
  `PackageNotFoundError: No package metadata was found for paramiko`; it
  reads its own version through `importlib.metadata`.
- `pip install --target` compiles bytecode by default. `--no-compile` cuts
  the artifact from 5.8 MB to 2.3 MB; `__pycache__` was more than half the
  file.

The release workflow builds on Python 3.10, not the newest available. Building
on 3.12 risks pip resolving a dependency wheel that requires 3.12, producing
an artifact that imports fine in CI and fails on a host running the version
`requires-python` claims to support.

## `templates/` was a namespace package, and only a zip noticed (Sep 2026)

Found immediately on first running the zipapp build: every command died at
import with

```
NotADirectoryError: MultiplexedPath only supports directories
```

`akropolis/templates/` had no `__init__.py`, making it an implicit namespace
package (PEP 420). `importlib.resources.files("akropolis.templates")` resolves
a namespace package to a `MultiplexedPath`, since a namespace package can span
several directories. That works when the package is an ordinary directory on
disk, and cannot work inside a zip.

The failure is total rather than partial: `remote.py`, `haproxy_phase`,
`nginx_keepalived_phase` and `patroni_phase` all read templates through
`resources.files()`, and `remote.py` is imported by every phase.

Fix is one empty file. Worth recording because the bug is invisible in every
normal invocation: `pip install -e .`, `pip install .` and running from a
checkout all work fine, and nothing in the test path would ever have caught
it. The rule is that any directory shipped as package data needs an
`__init__.py` if its contents are read through `importlib.resources`. The
release workflow now asserts the template count readable from the built zip,
so removing the file fails the release instead of publishing a binary that
dies on first run.

---

## Fixing the emitter does not fix what it emitted (Sep 2026)

v0.16.11 corrected both monitor bugs at the source: handoff stopped emitting
`verify_tls: true`, and the base phase's UFW list gained 8080. The running
cluster did not care. The monitor config was already on the workstation with
the old value, the firewall rules were already written, and nothing in a
normal `provision` run goes back to reconcile either; completed phases skip,
which is the whole point of the state file.

So the fix landed in two places at two different times: the code, and then
the cluster, by hand, node by node. Both were needed and neither implied the
other.

This is a category akropolis has not really named before. Most of what a
phase does is inside the phase's own domain and is re-asserted when it
re-runs. These two are different: one output lives on the *workstation* after
the run (the emitted monitor config, a file akropolis writes once and then
has no further relationship with), and the other lives in a system whose
state a later phase never re-reads (UFW rules, written by `base`, which is
`done` forever afterwards). A provisioner's outputs drift away from the
provisioner the moment they are written, and the fix for a bug in the
emitting code does nothing for the population already emitted. The README now
carries the two remediation commands for anyone in that population.

The `--replay base` path exists and would write the port rule, but it writes
one combined multi-port rule while the manual fix adds a standalone one, so
taking it later leaves both. Worth knowing before someone reads `ufw status`
on a node and wonders which rule is authoritative.

Worth keeping too, because it cost nothing and settled things fast: curl exit
codes separate the layers cleanly when a monitor panel is red. 60 is a
certificate that will not verify, 28 is a firewall dropping silently, 7 is
nothing listening, 0 means the path is fine and the problem is in the monitor
config. Two curls told us which of two unrelated bugs owned which panel,
before changing anything.

Both topologies have now been provisioned end to end, including a database
restore on the HA cluster, with every monitor panel green afterwards. The
provisioning path does what it was built to do. What is thin is everything
around it: restore against varied dumps, `clean`, per-provider TLS coverage.

Rule: when a fix changes what a phase *emits*, ask what already exists that
was emitted by the old code. If the answer is "files and firewall rules on
machines that will not be touched again", the patch is not finished until the
README tells that operator what to do.

---

## The monitor said nginx was down on a cluster that was serving traffic (Sep 2026)

First HA cluster provisioned end to end, everything healthy, and ak-monitor
showed all three nodes nginx-down, keepalived FAULT, VIP UNREACHABLE.

Handoff derives one of the values it emits from the certificate provider:

```python
verify_tls=cfg.tls.provider in ("acme", "import"),
```

The monitor probes each node individually, which means by IP
(`https://10.20.0.11/monitor`), and the certificate is issued for
`auth-tmp.example.com`. `requests` raises on the hostname mismatch, the probe's
bare `except` records nginx as down, and from there it cascades: the
keepalived panel does not read keepalived, it infers state from nginx
reachability, so every node became FAULT; the VIP holder check probes the VIP
the same way, so no node could be MASTER. One unsatisfiable TLS check, three
panels wrong, cluster perfectly fine.

Confirmed in one line from the monitor host: `curl -s` exits 60, `curl -sk`
returns the identity JSON.

What makes this more than a one-line fix is that `VERIFY_TLS` has exactly two
call sites in monitor.py (the per-node `/monitor` probe and the VIP holder
check), and *both* are address-based by construction. Per-node probing cannot
use the hostname: the hostname resolves to one address, not three. Everything
else in that file hardcodes `verify=False`. So the flag had no configuration
in which it was both true and working. It was not a security control that was
inconveniently strict; it was a switch whose only reachable effect was to
break two panels. Note also that the probes actually carrying the bootstrap
API token are among the ones hardcoded to `verify=False`, so `true` never
protected anything sensitive either.

Now emitted `false` unconditionally, in both topologies, with the reasoning in
the template rather than only here. The key stays in the file rather than
being hardcoded in the monitor: an internal CA issuing certs with IP SANs is
the one setup where `true` is meaningful, and that operator should be able to
flip it.

This is the second bug of exactly this shape in a week: after preflight
demanding the VIP appear in a public DNS record. Both derive a check from a
property adjacent to the real one: *who issued the certificate* instead of
*what address is dialed*; *what the name resolves to* instead of *whether the
name reaches the VIP*. The adjacent property is usually the one that is easy
to read from the config, which is exactly why it gets reached for. Worth
naming as a pattern rather than filing as two incidents.

Found alongside: `MONITOR_PORTS_HA` did not include 8080, so the NGINX
CONNECTIONS panel was blocked by UFW even though nginx.conf.j2 grants the
monitor IP an ACL exemption on that same server block. Two allowances for one
path, in two different systems, and only one of them was updated when the
panel was added. UFW drops first, so nginx logs nothing; silent by
construction. Both lists now carry 8080.

Rule: when a check can only ever fail, the bug is the check, not the
environment. Ask what configuration would make it pass before shipping it.

---

## Preflight demanded the VIP in a public DNS record (Sep 2026)

A real HA provision against `auth-tmp.example.com` failed preflight before touching
anything:

```
✔ (cluster) VIP unclaimed: no reply (good)
✘ (cluster) DNS auth-tmp.example.com → VIP: resolves to ['203.0.113.10']
```

Both lines were produced by the same phase and only one of them was right.
The VIP check is the timing check: nothing may own `10.20.0.10` before
`nginx-keepalived` runs, and it passed. The DNS check was a plain `getent`
requiring the literal VIP string in the answer, and no amount of provisioning
would ever have satisfied it: `auth-tmp.example.com` is a public record, the VIP is
RFC1918, and public DNS cannot return a private address. Unsatisfiable, not
premature. The actual path is `auth-tmp.example.com` → `203.0.113.10` → pfSense
DNAT `:80/:443` → `10.20.0.10`, which is exactly what HTTP-01 needs, and
entirely invisible from inside the cluster.

The bug is a conflated pair of properties: *the name reaches the VIP* versus
*the name resolves to the VIP*. Only the first matters, and the check tested
the second. It was strictly worse than useless in both directions: it failed
a correct NAT deployment, and in the direct-resolution case a VIP match still
proved nothing, since resolving to an address says nothing about anything
answering on it.

Telling is that the single-node branch of the same function already had this
right. With no VIP to compare against, whoever wrote it had to face the
question honestly and settled on: assert the name resolves at all, then say
plainly that NAT means akropolis cannot confirm the target and the operator
must check by hand. The HA branch had a VIP in scope and reached for it as a
proxy for reachability: a tighter check measuring the wrong thing. Available
data is not the same as relevant data.

v0.16.9 makes HA behave like single: resolution is hard for `acme` (a typo or
an unregistered name genuinely does kill issuance), a VIP match is reported as
information when it occurs, and the detail line names the NAT hop as the
operator's to verify. The check keeps the failure it was built to prevent:
finding out at `nginx-keepalived`, seven phases deep, with etcd, Patroni,
HAProxy and a placeholder cert already on disk, without inventing one.

Also fixed alongside: `PHASES_BY_TOPOLOGY["ha"]` listed neither `tls` nor
`restore`, so preflight's state-aware `done` set could never contain them and
`midlife` was computed from an incomplete pipeline. Harmless in practice:
neither phase owns a port in `PHASE_PORTS`, and `base` is done in any scenario
that would reach them, but the `single` tuple lists its `restore` and this
one silently didn't. A lookup table that has to stay in step with
`cli.py`'s pipelines will drift again; worth deriving from them if a third
topology ever appears.

Rule: a check should assert the property that failure actually depends on. If
the honest version of a check is weaker, ship the weaker one and name what it
cannot see: a precise check on the wrong property costs more than a loose
check on the right one.

---

## The version was the one thing the config didn't say (Sep 2026)

`init` deliberately omitted `authentik.tag`, leaving config.py's
topology-aware default to apply. The reasoning was that the defaults are
correct and an absent key is one less thing to get wrong. In practice it
meant the single most consequential value in the deployment (which authentik
release every node runs) was invisible in the file the operator reviews, and
discoverable only by reading the source or the example config.

`init` now writes it out explicitly, with the topology default as the value,
a footer explaining that it is a placeholder, and a closing line that names
the version before the operator runs `provision`. Not asked interactively:
at init time you often don't know yet, and editing one line beforehand beats
answering a question you'd have to go and look up mid-wizard.

Quoting is not cosmetic here. An unquoted `tag: 2026.10` is parsed by YAML as
the float 2026.1, and `str()` turns that into "2026.1": an existing tag, a
different release, pinned across every node without a word. The wizard emits
the tag quoted, and `load()` now refuses a tag that came back as anything
other than a string.

Rule: defaults belong in the generated file, not only in the code that
generates it. "Absent means correct" is only true for the person who wrote
the default.

---

## `acme.staging: true` is a one-way door without `--force-renewal` (Sep 2026)

The wizard used to hardcode `staging: true` on the reasoning that a first run
should not spend real rate limit. The consequence was worse than the problem:
the node came up with a Let's Encrypt **staging** certificate, which no
browser trusts, and setting `staging: false` and re-running did **not**
replace it. certbot decides whether to reissue on remaining validity alone:
the lineage had ~89 days left, so it reported *not due for renewal* and left
the staging certificate exactly where it was. The only way out was running
certbot by hand with `--force-renewal`.

Two fixes, both needed:

1. `acme.staging` is a real question in the wizard now, defaulting to the
   production directory. Rehearsing is a choice, not the default.
2. Before issuing, the `certs` phase reads the issuer of any certificate
   already at `/etc/letsencrypt/live/<hostname>/` (Let's Encrypt's staging
   intermediates carry `(STAGING)` in the issuer CN) and adds
   `--force-renewal` when it disagrees with what the run is asking for.
   `acme.force_renewal: true` forces it unconditionally.

Rule: whenever a phase is idempotent by asking an external tool "is this
already done?", check that the tool's notion of *done* is the same as ours.
certbot's was "still valid", ours was "issued by the right CA".

---

## `DROP DATABASE` takes provisioning state with it (Sep 2026)

A single-node restore finished with every check green (230 tables, 1317
users, containers healthy, `/-/health/ready/` 200), and the UI was broken:
the dashboard and user list would not load properly. Nothing in the restore
was wrong. The problem is that three things provisioned by earlier phases
live *in the database*, so the dump replaced all of them with the source
instance's versions:

1. the bootstrap API token: the restored database has the old instance's
   tokens instead. `AUTHENTIK_BOOTSTRAP_TOKEN` in the env file does not
   recover it: authentik applies that when it *creates* `akadmin`, and the
   restored database already has one, so the next worker start is a no-op.
2. the default brand's branding (logo/favicon/title).
3. the default brand's `web_certificate`, the damaging one. On single-node
   topology authentik's own webserver terminates TLS and that column decides
   which keypair it presents. Restored, it points at a keypair that does not
   exist here, and the node silently falls back to its self-signed
   certificate.

The phase now re-mints the token via `ak shell` inside the worker container
(the only path that does not itself require a working token), then re-applies
branding and the web certificate. Best-effort: a failure warns and names the
manual fix rather than failing a restore that otherwise worked.

Rule: a phase that replaces the database must inventory what earlier phases
wrote *into* it, not just what they wrote to disk. Green health checks prove
the service is up, not that it is configured.

---

## Sudo failing mid-phase (Sep 2026)

A wrong sudo password first surfaced three checks into the `restore` phase,
at the `docker compose stop server worker` step, immediately before the
destructive part, and after the run had already reported progress. Nothing
was damaged (the phase refuses to touch the database with clients up), but
the operator is left reconstructing how far it got.

`provision` and `clean` now prove sudo works on every node before the first
phase runs, with up to three attempts at the password. Preflight already did
this, but `--only`/`--replay` skip preflight entirely, which is exactly when
a destructive phase runs alone.

Rule: credentials are validated at the top of the run, not wherever they
happen to be needed first.

---

## Single-node was answering HA's questions (Sep 2026)

`akropolis init` asked for a network interface and an expected MTU on
`single` topology, and preflight then checked them. Neither value is read by
any single-node phase: the interface is what keepalived binds the VIP to, and
the MTU is what has to survive between nodes for VRRP/etcd/Patroni. A single
node has neither, so the questions produced answers nothing consulted and a
preflight check that could fail a perfectly good host over a number that did
not matter. Both are HA-only now, and the emitted config has no `network`
section at all for `single`.

chrony stays installed on both: on a single node it is still what keeps TOTP
validation and certificate `notBefore`/`notAfter` honest, but it is no longer
called out in the plan the operator approves, because it is host hygiene
rather than a decision.

Rule: a question worth asking is one whose answer changes something.

---

## pg_dump version skew on restore (Sep 2026)

First real run of the `restore` phase failed with `ERROR: unrecognized
configuration parameter "transaction_timeout"`. Not a data problem: the dump
had been written by a `pg_dump` 17 binary, whose header emits
`SET transaction_timeout = 0;`, a GUC that does not exist before PostgreSQL
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

Right after the GUC-skew fix, the same restore failed again, this time on
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
only after the worker reports healthy. Nodes 2 and 3 are unaffected (they
find a migrated schema), so they keep the plain `up -d`.

Also added: whenever a health gate expires, akropolis prints the tail of the
container log instead of advising the operator to go and read it.


## Monitor config assumed TLS everywhere (Sep 2026)

The emitted monitor config hardcoded `ports.authentik: 9443` and said nothing
about scheme, while ak-monitor probes `https://<node-ip>/monitor` for the
nginx/keepalived panel and `/-/health/live/` on 9443. On a `tls: none` lab
site nginx serves plain HTTP on :80, so every node shows DOWN for a reason
that has nothing to do with the cluster.

Second problem in the same place: the Authentik **worker** does not listen on
9443 at all: since v0.7.10 it binds its own port (9081) so it cannot squat
the server's. Probing the worker on 9443 reaches the *server*, so a dead
worker looks alive: the exact failure mode the worker healthcheck exists to
catch.

Handoff (v0.10.4) now emits a `scheme:` block: `nginx` (http/https) and
`nginx_port` derived from tls.provider, `authentik`/`authentik_worker`
schemes, `verify_tls` (true only for acme/import; self-signed and lab certs
must not be verified), plus `ports.authentik_worker: 9081`.

**ak-monitor must be taught to read these keys**: emitting them is only half
the fix. Until then, a tls: none site needs the scheme edited by hand.


## Health gate accepted "unhealthy" (Sep 2026): gate correctness

`wait_one_healthy` (introduced v0.10.3) polled with `expect="healthy"`, a
SUBSTRING test. `"healthy" in "unhealthy"` is True in Python, so the gate
reported "worker healthy (migrations complete)" for a container Docker had
marked **unhealthy**. The phase then tried to start the server, compose
refused on the dependency condition, and the failure appeared to contradict
the line printed immediately above it.

The same trap existed in the older pair gate's polling (`grep -v healthy`
filters out "unhealthy" lines too); it survived only because a separate exact
`== "healthy"` confirmation ran afterwards.

Both now use shell string equality against the deduplicated status list:
`[ "$(docker inspect -f '{{.State.Health.Status}}' ... | sort -u)" = healthy ]`,
which cannot match a substring. Verified against a real shell for healthy /
unhealthy / starting / empty.

**Rule for this codebase: never test container health with a substring or
grep match.** A gate that lies is worse than no gate: it moves the failure
somewhere unrelated and makes the operator distrust correct output.

Second fix in the same version: the server is now started with
`up -d --no-deps server`. A plain `up -d` re-evaluates depends_on, RESTARTS
the freshly-gated worker (health resets to "starting") and then waits on it
with compose's own ~150s clock, reintroducing the exact failure the phase
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

- **verify passed.** It counted tables and users: reads only. The app's first
  action is a WRITE. Verify now does a real write as the app role and asserts
  no public table is owned by anyone else.
- **the liveness endpoint kept answering 200** while the Python process was
  dead, so `/-/health/live/` on the worker said nothing useful (already known
  for the server; equally true here). Container health, not the endpoint, is
  the signal; and container health must be compared exactly (see v0.10.5).


## Restore invalidates the bootstrap API token (Sep 2026)

Obvious in hindsight: the authentik phase proves the bootstrap token against
the live API, then the restore phase replaces the entire database with a dump
from another system. The token the monitor was handed at handoff no longer
exists, and the failure surfaces far from its cause: as "unauthorized" in
the dashboard's Workers panels, long after the restore reported success.

The restore phase now checks the token after bringing authentik back and, if
it is dead, says exactly what to do (new token in akadmin > Directory >
Tokens, admin scope, into the monitor config) as a warning rather than a
failure; the cluster itself is healthy.


## Monitor host blocked by the stub_status ACL (Sep 2026)

The nginx panel read UNREACHABLE for every node while nginx was fine: the
stub_status server on :8080 allows loopback and the node subnet only, and the
monitor runs from a workstation outside it, so nginx answered 403. monitor.ip
is now added to `stub_status_allow` automatically: the same host that gets
the UFW opening in the base phase.


## SFTP cannot sudo (Sep 2026)

First run with branding failed on `[Errno 13] Permission denied`. `conn.put()`
is SFTP, which runs as the SSH user; `sudo` applies to `run()` only. The
branding directory is created root-owned by a privileged `run()`, and the
SFTP write into it is then refused under `become: true`.

The restore phase never hit this because it uploads to /tmp, which is world
writable, so the flaw shipped hidden behind the one call site that happened
to be safe.

push_binary now stages in /tmp and installs with a privileged
`mkdir && mv && chmod`, and re-checks the checksum AFTER the move (a
truncated transfer would otherwise be bind-mounted into the container and
serve a broken asset). The bare OSError is also wrapped: "[Errno 13]
Permission denied" named neither the node, the file, nor which end refused.

**Rule: any write outside /tmp must go through run(), not put().**


## Documented in the README, missing from the example config (Sep 2026)

`base.apt_upgrade` and `network.trusted_proxies` were both implemented and
described in the README, but absent from `config.example.yml`, so an operator
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


## Single-node topology: network_mode: host wasn't needed at all (Sep 2026)

Supersedes the two entries directly below (worker squatting 443, then the
server needing CAP_NET_BIND_SERVICE); both were real, both got fixed, and
both turned out to be unnecessary complexity in the first place once the
actual official reference compose (docs.goauthentik.io/compose.yml) got
compared against what akropolis was generating.

The reference doesn't use network_mode: host at all. Ordinary bridge
networking: each container gets its own isolated network namespace,
containers reach each other by Docker's own compose-network DNS (service
name), and the one port that needs to reach the outside world is published
the normal Docker way (`ports: ["443:9443"]` on `server`), a host-level
operation performed by dockerd, not a bind() call made by the containerized
process. Once that's the design, an entire category of problems stops being
reachable:

- the worker can't squat the server's ports: they're not in the same
  network namespace, there's nothing to squat
- the server doesn't need CAP_NET_BIND_SERVICE: its own internal port
  (9443) was never privileged; Docker's port-publish is what maps 443,
  running as root at the daemon level, external to the container entirely
- no AUTHENTIK_LISTEN__* overrides needed anywhere, for either container

single-node's network_mode: host was inherited from the HA cluster's
compose by pattern-matching, not by an actual reason that applies here: the
HA cluster needs it for HAProxy routing (each node's Authentik has to reach
`127.0.0.1:5000`, its own local HAProxy) and per-node identification
(nginx's `/monitor` endpoint reporting which node answered). Single-node has
neither: no HAProxy, and no nginx anymore either (see "no nginx after all"
below), there was never a reason for it once nginx left the design, it just
hadn't been reconsidered.

PostgreSQL also drops its loopback publish (`127.0.0.1:5432:5432`) entirely
now: matches the reference exactly, and `server`/`worker` were always going
to reach it by service name once bridge networking was in play regardless;
the loopback publish was leftover host-networking thinking (it was needed
under network_mode: host because that was the only way server/worker could
reach a loopback-bound container port; under bridge networking, Docker's
internal DNS makes that unnecessary and it becomes attack surface for
nothing).

Lesson, stated plainly: a design copied from a working setup for consistency
needs its OWN reason to exist in the new context, not just the old context's
reason. HA's network_mode: host is correct there because HAProxy/nginx need
it. Nothing about single-node ever needed it: the two bugs below were the
cost of not checking that before building on top of the assumption.

Not changed in this patch, flagged for later: restore_single_phase.py still
manually starts the worker alone, gates on it, then starts the server
--no-deps: a choreography written to dodge compose's own ~150s dependency-
health-wait ceiling. That ceiling doesn't apply anymore now that server
never depends on worker at all (see authentik_single_phase.py's compose:
both start concurrently, coordinating via Authentik's own internal database
lock, confirmed in a real boot log: "waiting to acquire database lock").
The manual choreography is still safe, just possibly solving a problem that
no longer exists on this topology; worth revisiting once single-node has
been through a real restore.


## Single-node topology: server still couldn't bind 443: needed a capability, not just the free port (Sep 2026)

**Superseded by the entry above**: kept for the record of how the
investigation actually went, not as current behavior. The fix described
here (`cap_add: NET_BIND_SERVICE`) worked and was correct for the
network_mode: host design it was written for, but that design itself is
gone as of the entry above; single-node no longer binds a privileged port
inside any container at all, so there is nothing for this capability to be
needed for anymore.



Follow-up to the worker-squatting entry above. Fixing that (worker's HTTPS
moved to 9444) surfaced a SECOND, separate problem behind it: with 443
actually free, the server still crashed, now with an explicit error instead
of a silent arbiter shutdown:

    Permission denied (os error 13)
    task.name=authentik_axum::server::run_tls(server, 0.0.0.0:443)

Also, earlier in the same boot, `"Not running as root, disabling
permission fixes"`. 443 is a privileged port (<1024); binding it needs
`CAP_NET_BIND_SERVICE` or root, and the server container's process runs as
a non-root user inside the image by default. This was the original
suspicion before the worker-squatting bug was found and fixed first: both
were real, independent problems stacked on top of each other, and the first
one's crash was masking the second's until it was cleared out of the way.

Fix: `cap_add: [NET_BIND_SERVICE]` on the `server` service only: the
minimum privilege needed, not full root the way the worker gets it for the
docker socket. The HA cluster never hits this at all: its server binds 9443
(nginx owns the real 443 externally), never a privileged port, so there was
no precedent for it in that template to check against.

Port 80 is unaffected: certbot's standalone bind happens as a host-level
process over SSH (root/sudo), not inside a container, so the same class of
restriction doesn't apply there.


## Single-node topology: worker squats the server's 443, fatally (Sep 2026)

**Superseded by "network_mode: host wasn't needed at all" above**: same
record-of-investigation note as the entry between this one and that one.

Found on a real run: `authentik-server-1` crash-looped forever ("Up Less
than a second (health: starting)" on repeat) while the worker sat healthy.
`docker logs authentik-server-1` showed the arbiter shutting itself down
gracefully about 0.3s after "starting tls watcher", no explicit error in the
visible tail.

Cause: the worker's environment overrides AUTHENTIK_LISTEN__HTTP and
AUTHENTIK_LISTEN__METRICS but not AUTHENTIK_LISTEN__HTTPS, so it inherits
443 from the shared .env: the exact same class of bug already documented
for the HA cluster's worker (config.example.yml: "listen ports pinned off
HAProxy's 9000"), which squats the server's HTTP/metrics ports by starting
first under network_mode: host. The HA case is non-fatal: the Go router's
binds fail silently and requests land on the worker's liveness endpoint
instead. This one crashes the whole arbiter: a failed TLS bind is
apparently fatal in a way a failed plain-HTTP bind isn't, and 443 is the
server's ONLY listener on single-node (there's no separate 9443-vs-443
split the way HA has). Fix: give the worker its own HTTPS port too: 9444,
otherwise unused.

The HA cluster's worker has the identical gap (no AUTHENTIK_LISTEN__HTTPS
override), just masked by the non-fatal failure mode. Left unfixed there
for now: this patch is scoped to the actual crash, and touching the
already-deployed HA compose template deserves its own look rather than a
same-day tag-along fix. Worth doing before it becomes a live incident there
too, not just a documented oddity.


## Single-node topology: the init wizard never knew it existed (Sep 2026)

Real gap, found the way these are always found: an operator ran `akropolis
init` expecting a single-node config and got an HA one instead. Every
single-node patch so far touched config.py, the phases, the templates, and
never `init_wizard.py`, which is the actual front door. It hardcoded
`range(1, 4)` for the node loop, always asked for a VIP, and always wrote
`authentik.tag: "2026.5.6"` explicitly into the generated file: the last
one being its own small trap: config.py's topology-aware tag default
(2026.5.6 for ha, 2026.8.1 for single) only applies when `authentik.tag` is
*absent*, so a wizard-generated single-node config would have silently
carried the HA pin regardless of topology.

Fix: ask topology first, right after environment, and thread it through
everything that follows: node count, whether the VIP question is asked at
all, the network block (no `vip`/`vrrp` keys written for single), and the
monitor-IP question (skipped entirely for single, matching base_setup.py:
there is nothing left for a monitor IP to unlock there; see the "stale
monitor port" entry above). `authentik.tag` is no longer written by the
wizard at all, for either topology: omitting it is what lets config.py's
own default logic apply; hardcoding any value here would have re-created
exactly this class of bug the next time a default changes.

Tested both paths end-to-end with scripted answers (`unittest.mock.patch
builtins.input`) rather than just reading the diff: the exact sequence and
count of `input()` calls is precisely the kind of thing that's easy to get
subtly wrong (an extra or missing prompt shifts every answer after it) and
hard to catch by inspection alone.


## Single-node topology: restore and clean (Sep 2026)

Completes the single-node pipeline. Both turned out considerably simpler
than expected once actually written, for the same underlying reason: one
node, one containerized postgres, no DCS to coordinate with.

`restore` drops the HA phase's Patroni-leader lookup (there is exactly one
postgres, always reached the same way, via `docker exec` into
`authentik-postgresql-1` rather than `sudo -u postgres psql` on bare metal)
and, more interestingly, drops the ownership-normalisation step entirely.
The HA phase's "restored objects owned by postgres, not the app role" trap
exists because psql runs there as the postgres SUPERUSER while the app
connects as a separate `authentik` role; a dump loaded without explicit
`OWNER TO` ends up unwritable by the app. On single-node this structural
mismatch cannot occur: `POSTGRES_USER=authentik` in
authentik-single-env.j2 means the postgres container's *only* superuser is
already named authentik: there is no separate postgres role to accidentally
own anything. One topology difference quietly closed an entire category of
bug rather than needing code to work around it.

Deliberately did NOT carry over `restore.database`/`restore.owner` as
configurable overrides, unlike the HA phase. Both are fixed to "authentik"
in the single-node phase, matching PG_DB/PG_USER in
authentik-single-env.j2, neither of which is itself configurable yet. A
`restore.owner` override would silently not match what the container was
actually initialised with, which is worse than not offering the knob.

`clean` needed far less new code than expected: STEPS_SINGLE and GONE_SINGLE
turned out to mostly be "the same paths, minus the ones that never existed."
The `/opt/authentik` teardown step already correctly handles the
containerized postgres too, since `authentik` (single) deliberately reuses
that exact compose project directory (see the "authentik + tls phases"
entry above), `docker compose down -v` drops the named `database` volume
along with the containers, no separate step needed. `/etc/letsencrypt`
already covered single-node's own certbot renewal hook the same way it
covers the HA cluster's certbot distribution key, for the same reason: both
live under that one directory. The only genuinely new step is a shorter
`STEPS_SINGLE`/`GONE_SINGLE` pair: the original STEPS/GONE would have
*worked* against a single-node host too (every HA-only command is
`2>/dev/null`-guarded and ends in `; true`, so it silently no-ops on paths
that never existed), but an operator would have seen a green "keepalived
down (VIP released)" checkmark on a host that never had keepalived, which
is the kind of small dishonesty this codebase has otherwise been careful to
avoid (see preflight's whole state-aware design).


## Single-node topology: handoff phase, and a stale monitor port (Sep 2026)

Two things, found while wiring up the handoff phase.

First, a latent bug from the 9443→443 port change two entries above: the
`base` phase's `MONITOR_PORTS_SINGLE` was still `"9443"`: a port nothing
listens on anymore. Worse, it was solving a problem that no longer exists:
443 is already public via the base allow-80/443 rule, so a monitor host
never needed a special punch-through for it in the first place. Removed the
whole monitor.ip prompt/rule for single topology rather than just fixing the
port number; there is nothing left on this topology that a monitor needs
UFW's help to reach.

Second, writing a test config without `tls.hostname` set (valid, it's only
required for acme/import) surfaced an empty `admin URL: https://` on the
landing card. The HA handoff phase has a fallback for exactly this case:
`tls: none` prints `http://{vip}`, but single-node has no VIP to fall back
to, and unlike HA, authentik here always answers HTTPS regardless of
provider (self-signed by default), so the fallback needed to be the node's
own IP instead: `https://{hostname or node.ip}`. Caught by actually running
`apply()` + `verify()` against a fake context before calling the phase done,
not by reading the code; worth remembering as a case for actually dry-running
new phases rather than trusting the plan()/apply() text alone.

New handoff_single_phase.py otherwise mirrors the HA one closely: same
plan → emit config → landing card → verify shape, much smaller by
construction (no VIP, no keepalived priorities, no HAProxy/postgres
credentials; PostgreSQL never leaves the loopback interface, so a remote
monitor couldn't use those credentials even if handed them).


## Single-node topology: no nginx after all: authentik serves TLS directly (Sep 2026)

Revised the previous entry's plan before writing the `nginx` phase it
described. The pfsense 1:1 NAT for the single-node target does no port
translation (checked directly against the actual rule: source port and NAT
port are both `*`), so whatever port the node listens on IS what the public
hits, and putting nginx in front to terminate 443 and hand off to authentik
on 9443 would just be moving the same problem one hop later for no benefit.

authentik already has the pieces to serve real HTTPS itself: a `certs`
directory mounted at `/certs` on the **worker** container triggers automatic
certificate *discovery* (matches certbot's own directory convention: a
folder named after the domain containing `fullchain.pem`/`privkey.pem`
imports as a keypair named after that folder), and each brand has a **Web
Certificate** field controlling which keypair authentik's core webserver
presents. Confirmed against the current docs
(docs.goauthentik.io/sys-mgmt/certificates/) rather than assumed, including
the exact API field name (`web_certificate`, a nullable UUID on the Brand
model). This patches through the same `core/brands/` endpoint and PATCH
mechanism already used for the branding logo/favicon, refactored into a
shared `find_default_brand_uuid`/`patch_brand` pair in `authentik_phase.py`
(additive: `port` parameter, default 9443, so the HA phase's calls are
unaffected; the new `certs` phase calls it at 443).

Consequence for the port map: `AUTHENTIK_LISTEN__HTTPS` moves from `9443` to
`443` for single-node. `AUTHENTIK_LISTEN__HTTP` stays at `9080`, deliberately
NOT `80`: that keeps port 80 free for certbot's `--standalone` mode, both
for initial ACME issuance and every renewal via a deploy hook (re-copy +
restart worker, same job the HA cluster's deploy hook does for nginx).

`tls.provider` keeps its meaning but the mechanics change: `none`/
`self_signed` are now a genuine no-op for single-node: authentik's own
auto-generated self-signed cert (valid 1 year, regenerated by authentik
itself) already does exactly what akropolis's HA `self_signed` provider does
manually for the 3-node cluster, so there's nothing to add. `acme`/`import`
place a real cert in authentik's discovery folder instead of nginx's cert
directory, restart the worker so discovery runs immediately rather than on
its own schedule, then PATCH the brand.

The `tls` phase itself is no longer part of `PIPELINE_SINGLE` at all; it's
fundamentally an `/opt/nginx/certs` phase, meaningless without nginx.
`preflight`'s `REQUIRED_FREE_PORTS_SINGLE` and `PHASE_PORTS` needed splitting
into a topology-nested structure once single's `authentik` stopped sharing
HA's port set (9443 → 443); same phase *name*, different footprint, so the
one dict that used to serve both would have been silently wrong for whichever
topology looked it up second.

Not yet handled: the crypto certificatekeypairs lookup (`GET
/api/v3/crypto/certificatekeypairs/`, filtered client-side by name) assumes
discovery finished within a fixed 15s sleep after the worker restart; fine
for a single small file, but worth turning into an actual poll if it ever
proves flaky in practice. `docker compose restart worker` also isn't checked
against a health gate before the discovery lookup runs, unlike everywhere
else in this codebase that touches a container.


## Single-node topology: authentik + tls phases (Sep 2026)

Second slice of `site.topology: single`, following the config/preflight/base
scaffolding above. `tls` needed one real fix (not just a topology branch):
its self-signed SAN list unconditionally appended `IP:{cfg.network.vip}`,
which single-node leaves as `""`: `openssl req -addext` with an empty IP
SAN would have produced either a malformed cert or a hard failure depending
on the openssl version. Fixed to only add the VIP SAN when one exists; a
couple of plan()-text lines that assumed 3 nodes were adjusted alongside it.

The new `authentik` (single) phase deliberately diverges from the real
running `auth-tmp` instance's compose in one respect: no external
`reverse_proxy` Docker network. That instance's networking follows from it
doubling as a general-purpose test box sitting behind an existing reverse
proxy; the actual single-node target is a dedicated VM reachable by 1:1 NAT
with nothing upstream, so akropolis owns TLS termination itself (the `nginx`
phase, not yet written) the same way it does on the HA cluster. Consequence:
`server`/`worker` run on `network_mode: host` like the HA cluster's
containers, not the bridge networks in the reference compose; which also
means the exact same container names (`authentik-server-1`,
`authentik-worker-1`, project directory `/opt/authentik` on both
topologies), so the HA phase's health-gate/log-dump helpers work unchanged
via a plain import, no duplication needed for those.

PostgreSQL keeps the reference's container-not-Patroni approach, but isn't
on a fully internal network either: it publishes `127.0.0.1:5432:5432`,
loopback only, never reachable off the host, so the host-networked
server/worker can reach it exactly the way an HA node reaches its own local
HAProxy on `127.0.0.1:5000`. Same "always local, never a remote IP" pattern
as the lesson that produced that HA rule in the first place.

`AUTHENTIK_ERROR_REPORTING__ENABLED` (the open guide-vs-code mismatch: guide
says true, the HA `.env` template hardcodes false) is resolved for
single-node by making it a real setting instead of picking a side:
`authentik.error_reporting` in the site config, or asked interactively once
and pinned in state, identical resolution order to `monitor.ip` and the
SMTP block. The HA phase is untouched for now and still hardcodes `false`;
unifying the two is the same follow-up noted in `authentik_single_phase.py`
that also covers the duplicated `_email`/`_branding_volumes`/`_acfg` methods.

Not yet handled: a single-node bootstrap with a genuinely slow migration
(e.g. a large `restore.sql_file`) could hit the same "dependency failed to
start" ceiling documented under "Restored database vs compose's dependency
wait" above; fine for a fresh empty-schema bootstrap (migrates in seconds),
but the single-node `restore` phase, when it exists, will need the identical
`--no-deps worker` alone-first fix already proven on the HA cluster.


## Single-node topology: scaffolding (Sep 2026)

First slice of `site.topology: single`: config validation, `preflight`,
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
  private IP via pfsense, no port translation) with nothing upstream. At the
  time this was written that meant "so akropolis needs an nginx phase to
  terminate TLS itself"; turned out to be wrong once the actual NAT rule was
  checked and authentik's own certificate-discovery/Web-Certificate mechanism
  was found; see "no nginx after all" further up. What did hold: keepalived
  is not part of single-node, since there is nothing to fail over to.
- **DNS→VIP becomes DNS→"resolves at all."** The HA preflight's DNS check
  confirms the hostname resolves to the VIP specifically. Single-node has no
  VIP, and NAT means akropolis running on the node itself has no reliable way
  to confirm the hostname resolves to *this* node's public IP rather than
  something else entirely. Rather than fake a check it can't actually make,
  preflight only confirms the hostname resolves to something, and says so:
  the operator is expected to verify the target by hand.
- **Required free ports drop out entirely**, not just get shorter: no
  etcd/Patroni/HAProxy ports at all, because PostgreSQL is a container on an
  internal Docker network and is never published to the host.

Still to do: the `authentik` phase itself (postgres-in-container + server +
worker, generated `.env`/compose derived from a real running `auth-tmp`
instance's files), a keepalived-less `nginx` phase, and a single-node-aware
`clean` (different on-disk paths: no `/etc/patroni`, no `/opt/haproxy`).


## Branding is two halves, not one (Sep 2026)

Noticed while testing a restore against a populated database: the logo only
appeared after the source database was loaded. Mounting a file changes
nothing by itself: Authentik serves the stock logo until the **brand row**
in the database references the asset. The restore brought the source
system's brand row with it, which is why it suddenly worked; on a fresh
cluster the same (correct) configuration looks broken.

The reverse case is worse: a dump whose brand references an asset this cluster
never mounted serves a broken image on the login page.

v0.11.3 closes the loop: after the cluster is healthy, the default brand is
PATCHed via /api/v3/core/brands/ to /static/dist/assets/{icons,images}/<name>.
Brand fields only accept the /static prefix for absolute paths
(goauthentik #19557), which is exactly where /web/dist/assets is served from.
The restore phase re-applies it after loading a dump. Failures warn rather
than fail: the cluster is healthy either way, and a logo is not worth
aborting a provision over.


## Repository history reset at v1.0.1 (Sep 2026)

The public repo's commit history now starts fresh at v1.0.1: a single
genesis commit carrying the working tree as it stood at that tag, pushed to
a new `ktsouvalis/akropolis`. The prior commit history (everything that led
up to and including 1.0.0/1.0.1 development) is archived internally under
`ktsouvalis/akropolis-archive`, now private, full history intact.

The `v1.0.0` and `v1.0.1` tags and their release assets are unchanged — the
1.0.1 release was re-cut on the new genesis commit reusing the exact
previously-published artifact bytes (downloaded, not rebuilt), so
`SHA256SUMS` and the install instructions still match. `v1.0.0` was not
carried forward onto the new history; it remains reachable only in the
archive.


## Third-party licenses were assumed covered, weren't checked (Sep 2026)

The README's architecture table credits everything akropolis *orchestrates*
(Authentik, PostgreSQL, Patroni, etcd, HAProxy, nginx, Keepalived, Docker,
Python, Ubuntu) with a logo, a link, and a trademark disclaimer. That part
was fine — none of those are redistributed in code form, so a credit line is
all that's owed.

What wasn't checked: `build_pyz.sh` vendors paramiko, Jinja2, PyYAML, and
rich *as source* directly into the release binary. That's actual code
redistribution, and paramiko is LGPL-2.1 — a different obligation than a
credit line, and one the repo had never actually verified it was meeting.

Checking it meant building the artifact and looking inside it rather than
reasoning about what the build script *should* do. Two things fell out:

- The license texts were already there. Modern wheels (all the ones
  currently pulled in) carry their license under `dist-info/licenses/`,
  which sits below the prune step's `find -maxdepth 2`, so it had been
  surviving into every release all along, just with nothing pointing at it.
- One wasn't: `mdurl` 0.1.2 (a transitive dep of rich, via markdown-it-py)
  ships its `LICENSE` as a bare file directly in `dist-info/`, not under
  `licenses/`. The prune step's allow-list (`METADATA`, `entry_points.txt`)
  deleted it from every 1.0.x release artifact without anyone noticing,
  since nothing checked the artifact's contents against what the wheels
  actually shipped.

Fix was two parts: widen the prune's keep-list to any `LICENSE*` /
`COPYING*` / `NOTICE*` / `AUTHORS*` file regardless of where it sits, and
generate `THIRD_PARTY_LICENSES.md` from the bundled dist-info metadata at
build time (not hand-maintained — it would have gone stale the first time a
dependency version changed) so the license set is visible without having to
unzip the binary to find it.

Also surfaced: paramiko 5.0.0 added `invoke` as an unconditional dependency
(previously a `[develop]`/testing extra, going by upstream history). It's a
task-runner, unused at runtime by anything akropolis does with paramiko —
it's pulled in and bundled purely because paramiko now declares it
unconditionally. Not a bug, just worth knowing next time the bundle size or
the license table changes shape without a corresponding akropolis change.
