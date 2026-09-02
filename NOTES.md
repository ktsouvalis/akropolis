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
