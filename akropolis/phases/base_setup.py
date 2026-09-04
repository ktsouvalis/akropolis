"""base — guide Step 1: hostname, /etc/hosts, packages, chrony, Docker, UFW.

Idempotent by construction: the /etc/hosts block is marker-managed, apt installs
are no-ops when satisfied, UFW rules can be re-added freely. `apt upgrade` is
deliberately NOT run here (slow, and package drift belongs to the operator's
patching policy, not the provisioner); it can be enabled via raw config
`base.apt_upgrade: true`.
"""

from __future__ import annotations

import ipaddress

from ..remote import push_file
from .base import Phase, PhaseContext

# Everything ak-monitor polls on the HA cluster: etcd client, PG via HAProxy
# (primary/replicas), Patroni REST, HAProxy stats CSV, Authentik health/API.
MONITOR_PORTS_HA = "2379,5000,5001,8008,9000,9443"
# single: no etcd/Patroni/HAProxy, PostgreSQL never leaves the internal Docker
# network (loopback-only), and Authentik's own HTTPS listener is port 443 —
# already public via the base allow-80/443 rule below. Nothing left that
# needs a monitor-specific UFW punch-through, so single topology skips the
# whole monitor.ip prompt/rule rather than opening a port nothing uses.

PACKAGES = ("curl wget gnupg2 ca-certificates lsb-release "
            "apt-transport-https software-properties-common "
            "htop iotop net-tools dnsutils tcpdump "
            "chrony vim git jq unzip "
            "python3 python3-pip python3-venv libpq-dev")

APT = "DEBIAN_FRONTEND=noninteractive apt-get -y -qq"


class BasePhase(Phase):
    name = "base"

    # ------------------------------------------------------------- monitor ip
    # Resolution: monitor.ip in the site config → interactive prompt, answer
    # (including the decision to skip, stored as "") pinned in state so a
    # --replay never re-asks. The monitor host is NOT one of the nodes, so
    # without this rule UFW's default-deny silently blanks every dashboard
    # column that isn't plain HTTPS.
    def _monitor_ports(self, ctx: PhaseContext) -> str:
        return MONITOR_PORTS_HA if ctx.cfg.topology == "ha" else ""

    def _monitor_ip(self, ctx: PhaseContext) -> str:
        if ctx.cfg.topology != "ha":
            return ""  # nothing to gate — see MONITOR_PORTS_HA comment above
        ip = str(((ctx.cfg.raw.get("monitor") or {}).get("ip") or "")).strip()
        if ip:
            return ip

        def ask() -> str:
            while True:
                v = input("monitoring host IP to allow through UFW "
                          f"(ports {self._monitor_ports(ctx)}; Enter to skip): ").strip()
                if not v:
                    return ""
                try:
                    ipaddress.ip_address(v)
                    return v
                except ValueError:
                    print(f"  {v!r} is not a valid IP address")

        return ctx.state.get_or_generate("monitor_ip", ask)

    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        upgrade = bool((cfg.raw.get("base") or {}).get("apt_upgrade", False))
        # plan must not prompt: show the config value or announce the question
        mon_ip = "" if cfg.topology != "ha" else (
            str(((cfg.raw.get("monitor") or {}).get("ip") or "")).strip()
            or ctx.state.data["generated"].get("monitor_ip", ""))
        ports = self._monitor_ports(ctx)
        ufw_base = ("UFW: default deny incoming / allow outgoing; allow ssh, 80, 443, 9000; "
                   "allow all traffic from each node IP; --force enable" if cfg.topology == "ha"
                   else "UFW: default deny incoming / allow outgoing; allow ssh, 80, 443; "
                   "--force enable (no inter-node rule, no monitor punch-through — single host, "
                   "nothing beyond the public 80/443 for a monitor to reach)")
        lines = [
            f"set hostname on each node ({', '.join(n.name for n in cfg.nodes)})",
        ]
        if cfg.topology == "ha":
            lines.append("manage an akropolis-marked block in /etc/hosts with all node entries")
        lines += [
            f"apt update{' && apt upgrade' if upgrade else ''} && install baseline packages + chrony",
            "install Docker CE from download.docker.com (keyring + repo + packages)",
            ufw_base,
        ]
        if cfg.topology == "ha":
            lines.append(f"UFW: allow monitor host {mon_ip} to ports {ports}" if mon_ip else
                        "UFW: no monitor host in config — you will be asked interactively "
                        "(Enter to skip; the answer is pinned in state)")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        upgrade = bool((cfg.raw.get("base") or {}).get("apt_upgrade", False))
        mon_ip = self._monitor_ip(ctx)  # may prompt — before any node is touched

        hosts_block = "\n".join(f"{n.ip}  {n.name}" for n in cfg.nodes)

        for conn in ctx.fleet:
            node = conn.node.name

            r = conn.run(f"hostnamectl set-hostname {conn.node.name}")
            ctx.record(node, "hostname", r.ok, conn.node.name if r.ok else r.err)

            if cfg.topology == "ha":
                # marker-managed /etc/hosts block (removable/replaceable on re-run)
                script = (
                    "sed -i '/# BEGIN akropolis/,/# END akropolis/d' /etc/hosts && "
                    "printf '# BEGIN akropolis\\n%s\\n# END akropolis\\n' "
                    f"'{hosts_block}' >> /etc/hosts"
                )
                r = conn.run(script)
                ctx.record(node, "/etc/hosts block", r.ok, r.err if not r.ok else "")

            ctx.begin(node, "apt update" + (" + upgrade" if upgrade else ""))
            r = conn.run(f"{APT} update", timeout=300)
            if upgrade and r.ok:
                ctx.begin(node, "apt upgrade", "can take several minutes")
                r = conn.run(f"{APT} upgrade", timeout=1800)
            ctx.record(node, "apt update" + ("+upgrade" if upgrade else ""), r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            ctx.begin(node, "installing baseline packages", "chrony, jq, pg client deps, ...")
            r = conn.run(f"{APT} install {PACKAGES}", timeout=900)
            ctx.record(node, "baseline packages", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")
            conn.run("systemctl enable --now chrony")

            # Docker (guide 1.3) — skipped when already present
            if conn.run("command -v docker && docker compose version").ok:
                ctx.record(node, "docker", True, "already installed")
            else:
                ctx.begin(node, "installing Docker CE", "keyring + repo + packages")
                script = r"""
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
> /etc/apt/sources.list.d/docker.list
apt-get -qq update
DEBIAN_FRONTEND=noninteractive apt-get -y -qq install docker-ce docker-ce-cli containerd.io \
docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
"""
                r = conn.run(script, timeout=900)
                ctx.record(node, "docker install", r.ok,
                           r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            # UFW (guide 1.4) — ssh rule goes in before enable, always.
            # HA only: inter-node allow-all (Patroni/etcd/HAProxy traffic
            # between the 3 nodes) and the HAProxy stats port. single has
            # neither — one host, nothing to route to itself over the network.
            monitor_rule = (f" && ufw allow from {mon_ip} to any port {self._monitor_ports(ctx)} "
                            f"proto tcp comment 'akropolis monitor'" if mon_ip else "")
            if cfg.topology == "ha":
                allow_from = " && ".join(f"ufw allow from {n.ip} to any" for n in cfg.nodes)
                script = (
                    "ufw default deny incoming && ufw default allow outgoing && "
                    "ufw allow ssh && ufw allow 80/tcp && ufw allow 443/tcp && "
                    f"{allow_from} && ufw allow 9000/tcp{monitor_rule} && ufw --force enable"
                )
            else:
                script = (
                    "ufw default deny incoming && ufw default allow outgoing && "
                    "ufw allow ssh && ufw allow 80/tcp && ufw allow 443/tcp"
                    f"{monitor_rule} && ufw --force enable"
                )
            r = conn.run(script, timeout=120)
            ctx.record(node, "ufw rules + enable"
                       + (f" (+ monitor {mon_ip})" if mon_ip else ""),
                       r.ok, r.err if not r.ok else "")

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        mon_ip = str(((ctx.cfg.raw.get("monitor") or {}).get("ip") or "")).strip() \
            or ctx.state.data["generated"].get("monitor_ip", "")
        for conn in ctx.fleet:
            node = conn.node.name
            checks = [
                ("docker compose available", "docker compose version >/dev/null"),
                ("ufw active", "ufw status | grep -q 'Status: active'"),
                *([( "monitor ip in ufw",
                     f"ufw status | grep -qF {mon_ip}")] if mon_ip else []),
                ("chrony running", "systemctl is-active chrony >/dev/null"),
                ("hostname applied", f"test \"$(hostname)\" = {conn.node.name}"),
            ]
            for label, cmd in checks:
                r = conn.run(cmd)
                ctx.record(node, f"verify: {label}", r.ok, r.err if not r.ok else "")
                ok = ok and r.ok
        return ok
