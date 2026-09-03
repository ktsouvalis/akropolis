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
