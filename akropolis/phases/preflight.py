"""Preflight — read-only validation of all three nodes before anything is changed.

Encodes the failure modes that historically cost the most time to diagnose
after the fact: wrong MTU on the overlay, no L2 path for VRRP, clock skew,
occupied ports, DNS not pointing at the VIP, and — most importantly — running
against a host that already carries a cluster.
"""

from __future__ import annotations

import time

from ..config import REQUIRED_FREE_PORTS
from .base import Phase, PhaseContext


class PreflightPhase(Phase):
    name = "preflight"
    read_only = True

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        lines = [
            f"SSH to {len(cfg.nodes)} nodes as {cfg.ssh.user!r} (auth: {cfg.ssh.auth}) and run read-only checks:",
            "reachability + sudo, OS release, interface "
            f"{cfg.network.interface!r} exists with MTU {cfg.network.expected_mtu}",
            f"clock skew across nodes ≤ 5s, ≥ 20 GB free on /",
            f"required ports free: {', '.join(map(str, REQUIRED_FREE_PORTS))}",
            f"VIP {cfg.network.vip} is unclaimed",
            "inter-node MTU path (ping with DF bit at expected MTU)",
        ]
        if cfg.refuse_existing:
            lines.append("REFUSE any node with existing cluster artifacts (/etc/patroni, ak containers, keepalived)")
        if cfg.tls.provider == "acme":
            lines.append(f"DNS: {cfg.tls.hostname} must resolve to the VIP (hard requirement for ACME)")
        elif cfg.tls.provider in ("self_signed", "import"):
            lines.append(f"DNS: {cfg.tls.hostname} resolving to the VIP (warning only for {cfg.tls.provider})")
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        clocks: dict[str, int] = {}

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

            # 4. clock (collected, compared after the loop)
            r = conn.run("date +%s")
            if r.ok:
                clocks[node] = int(r.out)

            # 5. disk space on /
            r = conn.run("df --output=avail -BG / | tail -1 | tr -dc 0-9")
            if r.ok and r.out:
                free_gb = int(r.out)
                ctx.record(node, "free disk on /", free_gb >= 20, f"{free_gb} GB free (need ≥ 20)")
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
            occupied = sorted(set(REQUIRED_FREE_PORTS) & listening)
            ctx.record(node, "required ports free", not occupied,
                       f"occupied: {occupied}" if occupied else "all free")

            # 7. existing-cluster artifacts
            if cfg.refuse_existing:
                artifacts: list[str] = []
                if conn.run("test -e /etc/patroni").ok:
                    artifacts.append("/etc/patroni")
                r = conn.run(
                    "command -v docker >/dev/null && "
                    "docker ps --format '{{.Names}}' | grep -Ei 'authentik|etcd|haproxy|nginx' || true"
                )
                if r.out:
                    artifacts.append(f"containers: {r.out.replace(chr(10), ', ')}")
                if conn.run("systemctl is-enabled keepalived >/dev/null 2>&1").ok:
                    artifacts.append("keepalived enabled")
                ctx.record(node, "no existing cluster artifacts", not artifacts,
                           "; ".join(artifacts) if artifacts
                           else "clean host")

            # 8. inter-node MTU path (DF bit; payload = mtu - 28 for IPv4+ICMP headers)
            payload = cfg.network.expected_mtu - 28
            for other in ctx.fleet:
                if other.node.name == node:
                    continue
                r = conn.run(f"ping -c 2 -W 2 -M do -s {payload} {other.node.ip}")
                ctx.record(node, f"MTU path → {other.node.name}", r.ok,
                           f"{payload}B payload, DF set" if r.ok else "fragmentation or no path")

        # clock skew across nodes (uses collected samples; loop time inflates
        # skew slightly, so the 5s threshold is deliberately generous)
        if len(clocks) >= 2:
            skew = max(clocks.values()) - min(clocks.values())
            ctx.record("cluster", "clock skew", skew <= 5, f"{skew}s across nodes (≤ 5s)")

        # cluster-level checks run from node 1 — skip cleanly if it's unreachable
        first = ctx.fleet.conns[0]
        reachable = any(c.node == first.node.name and c.name == "ssh + root/sudo" and c.ok
                        for c in ctx.checks)
        if not reachable:
            ctx.record("cluster", "VIP / DNS checks", False,
                       f"skipped — {first.node.name} unreachable")
            return

        # VIP must be unclaimed
        try:
            r = first.run(f"ping -c 1 -W 1 {cfg.network.vip}")
            ctx.record("cluster", "VIP unclaimed", not r.ok,
                       "no reply (good)" if not r.ok else f"{cfg.network.vip} is answering — something owns it")
        except Exception as exc:  # noqa: BLE001
            ctx.record("cluster", "VIP unclaimed", False, str(exc), warn=True)

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
