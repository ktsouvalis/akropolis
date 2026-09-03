"""Preflight — read-only validation of all three nodes before anything is changed.

Encodes the failure modes that historically cost the most time to diagnose
after the fact: wrong MTU on the overlay, no L2 path for VRRP, clock skew,
occupied ports, DNS not pointing at the VIP, and — most importantly — running
against a host that already carries a cluster.
"""

from __future__ import annotations

import time

from ..config import REQUIRED_FREE_PORTS, REQUIRED_FREE_PORTS_SINGLE
from .base import Phase, PhaseContext

# What each completed phase legitimately occupies. Preflight consults the
# state file so a mid-lifecycle re-run (resume, --replay of a later phase)
# doesn't fail on the cluster's own footprint.
PHASE_PORTS: dict[str, set[int]] = {
    "etcd": {2379, 2380},
    "patroni": {5432, 8008},
    "haproxy": {5000, 5001, 9000},
    "nginx-keepalived": {80, 443},
    "nginx": {80, 443},              # single topology's keepalived-less nginx
    "authentik": {9080, 9081, 9300, 9301, 9443},
}
PHASE_CONTAINERS: dict[str, str] = {
    "etcd": "etcd",
    "haproxy": "haproxy",
    "nginx-keepalived": "nginx",
    "nginx": "nginx",
    "authentik": "authentik",
}

# Phase names that can legitimately have already run, per topology — used to
# compute the "expected footprint" a mid-lifecycle preflight should not
# complain about. Single topology's authentik/nginx phases don't exist yet
# (next patch); listed here so preflight is forward-compatible once they do.
PHASES_BY_TOPOLOGY: dict[str, tuple[str, ...]] = {
    "ha": ("base", "etcd", "patroni", "haproxy", "nginx-keepalived", "authentik", "handoff"),
    "single": ("base", "authentik", "nginx", "handoff"),
}


class PreflightPhase(Phase):
    name = "preflight"
    read_only = True

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        ports = REQUIRED_FREE_PORTS if cfg.topology == "ha" else REQUIRED_FREE_PORTS_SINGLE
        lines = [
            f"SSH to {len(cfg.nodes)} node(s) as {cfg.ssh.user!r} (auth: {cfg.ssh.auth}) and run read-only checks:",
            "reachability + sudo, OS release, interface "
            f"{cfg.network.interface!r} exists with MTU {cfg.network.expected_mtu}",
            f"clock skew across nodes ≤ 5s, ≥ 20 GB free on /",
            f"required ports free: {', '.join(map(str, ports))}",
        ]
        if cfg.topology == "ha":
            lines.append(f"VIP {cfg.network.vip} is unclaimed")
            lines.append("inter-node MTU path (ping with DF bit at expected MTU)")
        if cfg.refuse_existing:
            artifacts = "/etc/patroni, ak containers, keepalived" if cfg.topology == "ha" \
                else "ak containers (authentik/postgres)"
            lines.append(f"REFUSE any node with existing cluster artifacts ({artifacts})")
        lines.append("state-aware: footprint of already-completed phases (ports, containers"
                     + (", VIP" if cfg.topology == "ha" else "") + ") is expected, not a failure")
        if cfg.topology == "ha":
            if cfg.tls.provider == "acme":
                lines.append(f"DNS: {cfg.tls.hostname} must resolve to the VIP (hard requirement for ACME)")
            elif cfg.tls.provider in ("self_signed", "import"):
                lines.append(f"DNS: {cfg.tls.hostname} resolving to the VIP (warning only for {cfg.tls.provider})")
        elif cfg.tls.provider != "none" and cfg.tls.hostname:
            note = "hard requirement" if cfg.tls.provider == "acme" else "warning only"
            lines.append(f"DNS: {cfg.tls.hostname} resolves to *something* ({note}) — NAT means "
                        "akropolis cannot verify it resolves to THIS node's public IP; check that by hand")
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        clocks: dict[str, int] = {}
        required_ports = REQUIRED_FREE_PORTS if cfg.topology == "ha" else REQUIRED_FREE_PORTS_SINGLE

        done = {p for p in PHASES_BY_TOPOLOGY[cfg.topology]
                if ctx.state.phase_status(p) == "done"}
        midlife = bool(done)  # some phase completed — we own these hosts now
        expected_ports: set[int] = set()
        for p in done:
            expected_ports |= PHASE_PORTS.get(p, set())

        for conn in ctx.fleet:
            node = conn.node.name

            # 1. reachability + sudo
            try:
                conn.connect()
                r = conn.run("id -u")
                if r.ok and r.out == "0":
                    ctx.record(node, "ssh + root/sudo", True, f"uid 0 as {cfg.ssh.user}")
                elif cfg.ssh.become:
                    r2 = conn.run("id -u", sudo=True)
                    ctx.record(node, "ssh + root/sudo", r2.ok and r2.out == "0",
                               "sudo -n works" if r2.ok else f"sudo failed: {r2.err}")
                else:
                    ctx.record(node, "ssh + root/sudo", False,
                               f"connected as uid {r.out} without become=true")
                    continue
            except Exception as exc:  # noqa: BLE001
                ctx.record(node, "ssh + root/sudo", False, str(exc))
                continue

            # 2. OS release (warn-only)
            r = conn.run(". /etc/os-release && echo $ID $VERSION_ID")
            expected = "ubuntu 24.04"
            ctx.record(node, "os release", r.out == expected, r.out or r.err, warn=(r.out != expected))

            # 3. interface + MTU
            r = conn.run(f"cat /sys/class/net/{cfg.network.interface}/mtu 2>/dev/null")
            if not r.ok or not r.out:
                ctx.record(node, f"interface {cfg.network.interface}", False, "interface not found")
            else:
                mtu = int(r.out)
                ctx.record(node, f"MTU on {cfg.network.interface}", mtu == cfg.network.expected_mtu,
                           f"{mtu} (expected {cfg.network.expected_mtu})")

            # 4. clock — store the OFFSET from our own clock at the moment of
            # sampling, so sequential collection time cancels out and only
            # genuine skew remains
            r = conn.run("date +%s")
            if r.ok:
                clocks[node] = int(r.out) - time.time()

            # 5. disk space on / — hard requirement on a virgin host; after
            # base has installed packages/images the space is spent by design,
            # so mid-lifecycle it degrades to a warning
            r = conn.run("df --output=avail -BG / | tail -1 | tr -dc 0-9")
            if r.ok and r.out:
                free_gb = int(r.out)
                low = free_gb < 20
                ctx.record(node, "free disk on /", not low,
                           f"{free_gb} GB free (need ≥ 20"
                           + (", warning only — cluster already provisioned)" if low and midlife else ")"),
                           warn=(low and midlife))
            else:
                ctx.record(node, "free disk on /", False, r.err or "df failed")

            # 6. required ports free
            r = conn.run("ss -Htln | awk '{print $4}'")
            listening: set[int] = set()
            for addr in r.out.splitlines():
                try:
                    listening.add(int(addr.rsplit(":", 1)[-1]))
                except ValueError:
                    pass
            occupied = sorted((set(required_ports) & listening) - expected_ports)
            owned = sorted(set(required_ports) & listening & expected_ports)
            detail = f"occupied: {occupied}" if occupied else "all free"
            if owned:
                detail += f" (ignoring {owned} — owned by completed phases)"
            ctx.record(node, "required ports free", not occupied, detail)

            # 7. existing-cluster artifacts — artifacts of completed phases are
            # our own; anything else is a hard stop on a virgin host, a warning
            # mid-lifecycle (e.g. a wiped-for-replay phase leaving its config)
            if cfg.refuse_existing:
                artifacts: list[str] = []
                if "patroni" not in done and conn.run("test -e /etc/patroni").ok:
                    artifacts.append("/etc/patroni")
                r = conn.run(
                    "command -v docker >/dev/null && "
                    "docker ps --format '{{.Names}}' | grep -Ei 'authentik|etcd|haproxy|nginx' || true"
                )
                owned_pats = [PHASE_CONTAINERS[p] for p in done if p in PHASE_CONTAINERS]
                foreign = [name for name in r.out.splitlines()
                           if name and not any(pat in name for pat in owned_pats)]
                if foreign:
                    artifacts.append(f"containers: {', '.join(foreign)}")
                if "nginx-keepalived" not in done and \
                        conn.run("systemctl is-enabled keepalived >/dev/null 2>&1").ok:
                    artifacts.append("keepalived enabled")
                ctx.record(node, "no existing cluster artifacts", not artifacts,
                           "; ".join(artifacts) if artifacts else "clean host",
                           warn=(bool(artifacts) and midlife))

            # 8. inter-node MTU path (DF bit; payload = mtu - 28 for IPv4+ICMP headers)
            payload = cfg.network.expected_mtu - 28
            for other in ctx.fleet:
                if other.node.name == node:
                    continue
                r = conn.run(f"ping -c 2 -W 2 -M do -s {payload} {other.node.ip}")
                ctx.record(node, f"MTU path → {other.node.name}", r.ok,
                           f"{payload}B payload, DF set" if r.ok else "fragmentation or no path")

        # clock skew across nodes: spread of per-node offsets from our clock.
        # SSH round-trip adds well under a second of noise per sample.
        if len(clocks) >= 2:
            skew = max(clocks.values()) - min(clocks.values())
            ctx.record("cluster", "clock skew", skew <= 5,
                       f"{skew:.1f}s across nodes (≤ 5s)")

        # cluster-level checks run from node 1 — skip cleanly if it's unreachable
        first = ctx.fleet.conns[0]
        reachable = any(c.node == first.node.name and c.name == "ssh + root/sudo" and c.ok
                        for c in ctx.checks)
        if not reachable:
            ctx.record("cluster", "VIP / DNS checks", False,
                       f"skipped — {first.node.name} unreachable")
            return

        if cfg.topology == "ha":
            # VIP: unclaimed before nginx-keepalived has run; answering after
            try:
                r = first.run(f"ping -c 1 -W 1 {cfg.network.vip}")
                if "nginx-keepalived" in done:
                    ctx.record("cluster", "VIP owned by cluster", r.ok,
                               "answering (good — keepalived deployed)" if r.ok
                               else f"{cfg.network.vip} not answering — keepalived unhealthy?")
                else:
                    ctx.record("cluster", "VIP unclaimed", not r.ok,
                               "no reply (good)" if not r.ok
                               else f"{cfg.network.vip} is answering — something owns it")
            except Exception as exc:  # noqa: BLE001
                ctx.record("cluster", "VIP check", False, str(exc), warn=True)

            # DNS → VIP
            if cfg.tls.provider != "none" and cfg.tls.hostname:
                hard = cfg.tls.provider == "acme"
                try:
                    r = first.run(f"getent ahostsv4 {cfg.tls.hostname} | awk '{{print $1}}' | sort -u")
                    resolved = r.out.splitlines() if r.ok else []
                    ok = cfg.network.vip in resolved
                    ctx.record("cluster", f"DNS {cfg.tls.hostname} → VIP", ok,
                               f"resolves to {resolved or 'nothing'}", warn=(not ok and not hard))
                except Exception as exc:  # noqa: BLE001
                    ctx.record("cluster", f"DNS {cfg.tls.hostname} → VIP", False, str(exc), warn=not hard)

        else:
            # single: no VIP to check against. NAT (public IP → this node's
            # private IP via pfsense) means akropolis has no reliable way to
            # confirm the hostname resolves to *this* node from here — so it
            # only confirms the hostname resolves to something at all, and
            # says so plainly rather than pretending to verify more than it can.
            if cfg.tls.provider != "none" and cfg.tls.hostname:
                hard = cfg.tls.provider == "acme"
                try:
                    r = first.run(f"getent ahostsv4 {cfg.tls.hostname} | awk '{{print $1}}' | sort -u")
                    resolved = r.out.splitlines() if r.ok else []
                    ctx.record("cluster", f"DNS {cfg.tls.hostname} resolves", bool(resolved),
                               f"resolves to {resolved or 'nothing'} — NAT: verify this is the "
                               "right target by hand", warn=(not resolved and not hard))
                except Exception as exc:  # noqa: BLE001
                    ctx.record("cluster", f"DNS {cfg.tls.hostname} resolves", False, str(exc), warn=not hard)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        failures = [c for c in ctx.checks if not c.ok and not c.warn]
        warnings = [c for c in ctx.checks if not c.ok and c.warn]
        if warnings:
            print(f"\npreflight warnings: {len(warnings)} (review above)")
        if failures:
            print(f"preflight FAILED: {len(failures)} blocking problem(s):")
            for c in failures:
                print(f"  ✘ [{c.node}] {c.name} — {c.detail}")
            return False
        print(f"\npreflight passed: {len(ctx.checks)} checks, {len(warnings)} warning(s).")
        return True
