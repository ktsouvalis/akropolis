"""base — guide Step 1: hostname, /etc/hosts, packages, chrony, Docker, UFW.

Idempotent by construction: the /etc/hosts block is marker-managed, apt installs
are no-ops when satisfied, UFW rules can be re-added freely. `apt upgrade` is
deliberately NOT run here (slow, and package drift belongs to the operator's
patching policy, not the provisioner); it can be enabled via raw config
`base.apt_upgrade: true`.
"""

from __future__ import annotations

from ..remote import push_file
from .base import Phase, PhaseContext

PACKAGES = ("curl wget gnupg2 ca-certificates lsb-release "
            "apt-transport-https software-properties-common "
            "htop iotop net-tools dnsutils tcpdump "
            "chrony vim git jq unzip "
            "python3 python3-pip python3-venv libpq-dev")

APT = "DEBIAN_FRONTEND=noninteractive apt-get -y -qq"


class BasePhase(Phase):
    name = "base"

    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        upgrade = bool((cfg.raw.get("base") or {}).get("apt_upgrade", False))
        return [
            f"set hostname on each node ({', '.join(n.name for n in cfg.nodes)})",
            "manage an akropolis-marked block in /etc/hosts with all node entries",
            f"apt update{' && apt upgrade' if upgrade else ''} && install baseline packages + chrony",
            "install Docker CE from download.docker.com (keyring + repo + packages)",
            "UFW: default deny incoming / allow outgoing; allow ssh, 80, 443, 9000; "
            "allow all traffic from each node IP; --force enable",
        ]

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        upgrade = bool((cfg.raw.get("base") or {}).get("apt_upgrade", False))

        hosts_block = "\n".join(f"{n.ip}  {n.name}" for n in cfg.nodes)

        for conn in ctx.fleet:
            node = conn.node.name

            r = conn.run(f"hostnamectl set-hostname {conn.node.name}")
            ctx.record(node, "hostname", r.ok, conn.node.name if r.ok else r.err)

            # marker-managed /etc/hosts block (removable/replaceable on re-run)
            script = (
                "sed -i '/# BEGIN akropolis/,/# END akropolis/d' /etc/hosts && "
                "printf '# BEGIN akropolis\\n%s\\n# END akropolis\\n' "
                f"'{hosts_block}' >> /etc/hosts"
            )
            r = conn.run(script)
            ctx.record(node, "/etc/hosts block", r.ok, r.err if not r.ok else "")

            r = conn.run(f"{APT} update", timeout=300)
            if upgrade and r.ok:
                r = conn.run(f"{APT} upgrade", timeout=1800)
            ctx.record(node, "apt update" + ("+upgrade" if upgrade else ""), r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            r = conn.run(f"{APT} install {PACKAGES}", timeout=900)
            ctx.record(node, "baseline packages", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")
            conn.run("systemctl enable --now chrony")

            # Docker (guide 1.3) — skipped when already present
            if conn.run("command -v docker && docker compose version").ok:
                ctx.record(node, "docker", True, "already installed")
            else:
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

            # UFW (guide 1.4) — ssh rule goes in before enable, always
            allow_from = " && ".join(f"ufw allow from {n.ip} to any" for n in cfg.nodes)
            script = (
                "ufw default deny incoming && ufw default allow outgoing && "
                "ufw allow ssh && ufw allow 80/tcp && ufw allow 443/tcp && "
                f"{allow_from} && ufw allow 9000/tcp && ufw --force enable"
            )
            r = conn.run(script, timeout=120)
            ctx.record(node, "ufw rules + enable", r.ok, r.err if not r.ok else "")

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        for conn in ctx.fleet:
            node = conn.node.name
            checks = [
                ("docker compose available", "docker compose version >/dev/null"),
                ("ufw active", "ufw status | grep -q 'Status: active'"),
                ("chrony running", "systemctl is-active chrony >/dev/null"),
                ("hostname applied", f"test \"$(hostname)\" = {conn.node.name}"),
            ]
            for label, cmd in checks:
                r = conn.run(cmd)
                ctx.record(node, f"verify: {label}", r.ok, r.err if not r.ok else "")
                ok = ok and r.ok
        return ok
