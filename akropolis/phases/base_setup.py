"""base — guide Step 1: hostname, /etc/hosts, packages, chrony, Docker, UFW.

Idempotent by construction: the /etc/hosts block is marker-managed, apt installs
are no-ops when satisfied, UFW rules can be re-added freely. `apt upgrade` is
deliberately NOT run here (slow, and package drift belongs to the operator's
patching policy, not the provisioner); it can be enabled via raw config
`base.apt_upgrade: true`.

For the same reason, Ubuntu's own `unattended-upgrades` (apt-daily-upgrade.timer)
is masked by default here too: it is the same package-drift-under-a-running-
Patroni risk `apt_upgrade` avoids, except silent and on the OS's own schedule
instead of the provisioner's. Set `base.unattended_upgrades: true` to leave the
OS default (enabled) alone.
"""

from __future__ import annotations

from ..config import is_valid_ip_or_cidr, resolved_monitor_ips
from ..remote import push_file
from .base import Phase, PhaseContext

# Everything ak-monitor polls on the HA cluster: etcd client, PG via HAProxy
# (primary/replicas), Patroni REST, HAProxy stats CSV, Authentik health/API,
# and nginx stub_status. 8080 is easy to forget because nginx.conf.j2 already
# grants the monitor an ACL exemption on that server block — but the ACL is
# inside nginx and UFW drops the packet first, so the NGINX CONNECTIONS panel
# stays blank with nothing in any log to say why. The two allowances have to
# agree.
MONITOR_PORTS_HA = "2379,5000,5001,8008,8080,9000,9443"
# single: no etcd/Patroni/HAProxy, PostgreSQL never leaves the internal Docker
# network (loopback-only), and the bare-metal nginx's public port is already
# open via the base allow-80/443 rule below. The one thing left that needs a
# monitor-specific UFW punch-through is nginx's stub_status (:8080,
# nginx_single_phase.py) — same reasoning as HA's 8080 entry above: the ACL
# is inside nginx, but UFW drops the packet first if this rule is missing.
MONITOR_PORTS_SINGLE = "8080"

PACKAGES = ("curl wget gnupg2 ca-certificates lsb-release "
            "apt-transport-https software-properties-common "
            "htop iotop net-tools dnsutils tcpdump "
            "chrony vim git jq unzip "
            "python3 python3-pip python3-venv libpq-dev")

APT = "DEBIAN_FRONTEND=noninteractive apt-get -y -qq"


class BasePhase(Phase):
    name = "base"

    # ------------------------------------------------------------- monitor ip
    # Resolution: monitor.ips in the site config → interactive
    # prompt, answer (including the decision to skip, stored as []) pinned in
    # state so a --replay never re-asks. The monitor host is NOT one of the
    # nodes, so without this rule UFW's default-deny silently blanks every
    # dashboard column that isn't plain HTTPS.
    def _monitor_ports(self, ctx: PhaseContext) -> str:
        return MONITOR_PORTS_HA if ctx.cfg.topology == "ha" else MONITOR_PORTS_SINGLE

    def _monitor_ips(self, ctx: PhaseContext) -> list[str]:
        gen = ctx.state.data["generated"]
        ips = resolved_monitor_ips(ctx.cfg.raw, gen)
        if ips or "monitor_ips" in gen or "monitor_ip" in gen:
            return ips  # config had none, but a prior run already pinned an (possibly empty) answer

        def ask() -> list[str]:
            while True:
                v = input("monitoring host IP(s)/CIDR(s) to allow through UFW "
                          f"(ports {self._monitor_ports(ctx)}; comma-separated, "
                          "Enter to skip): ").strip()
                if not v:
                    return []
                entries = [e.strip() for e in v.split(",") if e.strip()]
                bad = [e for e in entries if not is_valid_ip_or_cidr(e)]
                if bad:
                    print(f"  not a valid IP or CIDR: {', '.join(bad)}")
                    continue
                return entries

        return ctx.state.get_or_generate("monitor_ips", ask)

    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        upgrade = bool((cfg.raw.get("base") or {}).get("apt_upgrade", False))
        disable_uu = not bool((cfg.raw.get("base") or {}).get("unattended_upgrades", False))
        # plan must not prompt: show the config value or announce the question
        mon_ips = resolved_monitor_ips(cfg.raw, ctx.state.data["generated"])
        ports = self._monitor_ports(ctx)
        ufw_base = ("UFW: default deny incoming / allow outgoing; allow ssh, 80, 443, 9000; "
                   "allow all traffic from each node IP; --force enable" if cfg.topology == "ha"
                   else "UFW: default deny incoming / allow outgoing; allow ssh, 80, 443; "
                   "--force enable (no inter-node rule — single host, nothing to route to "
                   "itself over the network)")
        lines = [
            f"set hostname on each node ({', '.join(n.name for n in cfg.nodes)})"
            if cfg.topology == "ha" else f"set the hostname to {cfg.nodes[0].name}",
        ]
        if cfg.topology == "ha":
            lines.append("manage an akropolis-marked block in /etc/hosts with all node entries")
        # chrony is installed on both topologies but only called out here for
        # HA, where clock skew is a cluster-correctness issue (Patroni leader
        # leases, etcd terms) the operator has to care about. On a single node
        # it still matters — TOTP validation and certificate notBefore/notAfter
        # both depend on the clock — but it is ordinary host hygiene, not a
        # decision, so it stays in the package list and in verify() instead of
        # taking up a line in a plan the operator is being asked to approve.
        lines += [
            f"apt update{' && apt upgrade' if upgrade else ''} && install baseline packages"
            + (" + chrony" if cfg.topology == "ha" else ""),
            "mask unattended-upgrades + apt-daily-upgrade.timer (OS auto-updates can "
            "replace packages under a running cluster on their own schedule; set "
            "base.unattended_upgrades: true to leave them alone)" if disable_uu else
            "leave OS unattended-upgrades as configured (base.unattended_upgrades: true)",
            "install Docker CE from download.docker.com (keyring + repo + packages)",
            ufw_base,
        ]
        lines.append(f"UFW: allow monitor host(s) {', '.join(mon_ips)} to ports {ports}"
                    if mon_ips else
                    "UFW: no monitor host in config — you will be asked interactively "
                    "(Enter to skip; the answer is pinned in state)")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        upgrade = bool((cfg.raw.get("base") or {}).get("apt_upgrade", False))
        disable_uu = not bool((cfg.raw.get("base") or {}).get("unattended_upgrades", False))
        mon_ips = self._monitor_ips(ctx)  # may prompt — before any node is touched

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

            ctx.begin(node, "installing baseline packages",
                      "chrony, jq, pg client deps, ..." if cfg.topology == "ha"
                      else "jq, pg client deps, ...")
            r = conn.run(f"{APT} install {PACKAGES}", timeout=900)
            ctx.record(node, "baseline packages", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")
            conn.run("systemctl enable --now chrony")

            # Masking (not just disabling) stops `systemctl start` — including
            # a package upgrade's postinst re-enabling the timer — from ever
            # bringing it back without an explicit unmask. Both the service
            # and its timer trigger are covered; -daily.timer (list refresh
            # only, no upgrade) is left alone.
            if disable_uu:
                r = conn.run("systemctl disable --now unattended-upgrades.service "
                             "apt-daily-upgrade.timer 2>/dev/null; "
                             "systemctl mask unattended-upgrades.service "
                             "apt-daily-upgrade.timer")
                ctx.record(node, "unattended-upgrades masked", r.ok, r.err if not r.ok else "")

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
            monitor_rule = "".join(
                f" && ufw allow from {ip} to any port {self._monitor_ports(ctx)} "
                f"proto tcp comment 'akropolis monitor'" for ip in mon_ips)
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
                       + (f" (+ monitor {', '.join(mon_ips)})" if mon_ips else ""),
                       r.ok, r.err if not r.ok else "")

    def verify(self, ctx: PhaseContext) -> bool:
        ok = True
        disable_uu = not bool((ctx.cfg.raw.get("base") or {}).get("unattended_upgrades", False))
        mon_ips = resolved_monitor_ips(ctx.cfg.raw, ctx.state.data["generated"])
        for conn in ctx.fleet:
            node = conn.node.name
            checks = [
                ("docker compose available", "docker compose version >/dev/null"),
                ("ufw active", "ufw status | grep -q 'Status: active'"),
                *[(f"monitor ip {ip} in ufw", f"ufw status | grep -qF {ip}")
                  for ip in mon_ips],
                ("chrony running", "systemctl is-active chrony >/dev/null"),
                ("hostname applied", f"test \"$(hostname)\" = {conn.node.name}"),
                *([("unattended-upgrades masked",
                    "systemctl is-enabled unattended-upgrades.service 2>&1 | "
                    "grep -q masked")] if disable_uu else []),
            ]
            for label, cmd in checks:
                r = conn.run(cmd)
                ctx.record(node, f"verify: {label}", r.ok, r.err if not r.ok else "")
                ok = ok and r.ok
        return ok
