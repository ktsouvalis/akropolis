"""Site configuration: load, validate, and expose as typed objects.

The file is the record; prompts are the fallback. Anything missing or invalid
is reported all at once so the operator can fix the file in one pass.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_ENVIRONMENTS = {"lab", "production"}
VALID_TLS_PROVIDERS = {"none", "self_signed", "acme", "import"}
VALID_SSH_AUTH = {"key", "agent", "password"}
VALID_TOPOLOGIES = {"ha", "single"}

# Default Authentik image tag per topology. Kept separate deliberately: the
# 3-node HA cluster stays pinned to 2026.5.6 (2026.8.0 hit an embedded-outpost
# restart loop specific to multi-node deployments — see akropolis NOTES.md /
# the HA guide changelog); a single node has no multi-node outpost topology
# to trigger that bug, so it can run current.
DEFAULT_AUTHENTIK_TAG = {"ha": "2026.5.6", "single": "2026.8.1"}

# Ports that must be free on every node before provisioning (SSH excluded).
# HA: full stack (etcd, Patroni, HAProxy, nginx+keepalived, Authentik).
REQUIRED_FREE_PORTS = [80, 443, 2379, 2380, 5432, 5000, 5001, 8008, 9000, 9080, 9081, 9300, 9301, 9443]
# single: no etcd/Patroni/HAProxy at all — PostgreSQL never leaves the internal
# Docker network. No nginx either (see NOTES.md: authentik's own core
# webserver serves HTTPS directly — Web Certificate + /certs discovery —
# since the target deployment is reached by NAT with no port translation, so
# whatever the node listens on IS what the public sees). Server HTTPS moves
# 9443 -> 443; 80 stays free for certbot's standalone ACME challenge, not
# bound by anything akropolis itself renders. 9444 is the worker's own
# HTTPS listener — under network_mode: host it would otherwise inherit and
# squat the server's 443 by starting first, which is fatal here (a failed
# TLS bind crashes the Rust arbiter), not the silent HTTP-only misroute the
# HA cluster's worker causes on 9080 — see NOTES.md.
REQUIRED_FREE_PORTS_SINGLE = [80, 443, 9080, 9081, 9300, 9301, 9444]


class ConfigError(Exception):
    """Raised with a list of human-readable validation problems."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class Node:
    name: str
    ip: str
    bootstrap_leader: bool = False


@dataclass
class SSHConfig:
    user: str = "root"
    become: bool = False
    auth: str = "key"
    key_file: str | None = None
    port: int = 22


@dataclass
class NetworkConfig:
    vip: str = ""
    interface: str = "ens18"
    expected_mtu: int = 1400
    vrrp_router_id: int = 51
    check_l2_adjacency: bool = True


@dataclass
class TLSConfig:
    provider: str = "self_signed"
    hostname: str = ""
    acme: dict = field(default_factory=dict)
    import_: dict = field(default_factory=dict)


@dataclass
class SiteConfig:
    name: str
    environment: str
    topology: str
    nodes: list[Node]
    ssh: SSHConfig
    network: NetworkConfig
    tls: TLSConfig
    state_file: Path
    refuse_existing: bool = True
    authentik_tag: str = "2026.5.6"
    raw: dict = field(default_factory=dict)

    @property
    def bootstrap_leader(self) -> Node:
        return next(n for n in self.nodes if n.bootstrap_leader)


def _get(d: dict, path: str, default=None):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def load(path: str | Path) -> SiteConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError([f"Config file not found: {path}"])

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    problems: list[str] = []

    # --- site ---
    name = _get(raw, "site.name")
    if not name:
        problems.append("site.name is required")
    environment = _get(raw, "site.environment", "lab")
    if environment not in VALID_ENVIRONMENTS:
        problems.append(f"site.environment must be one of {sorted(VALID_ENVIRONMENTS)}, got {environment!r}")

    topology = _get(raw, "site.topology", "ha")
    if topology not in VALID_TOPOLOGIES:
        problems.append(f"site.topology must be one of {sorted(VALID_TOPOLOGIES)}, got {topology!r}")
        topology = "ha"  # fall back so the rest of validation has something to check against

    # --- nodes ---
    nodes_raw = raw.get("nodes") or []
    nodes: list[Node] = []
    expected_node_count = 1 if topology == "single" else 3
    if len(nodes_raw) != expected_node_count:
        problems.append(f"topology {topology!r} requires exactly {expected_node_count} "
                        f"node(s), got {len(nodes_raw)}")
    seen_ips: set[str] = set()
    for i, n in enumerate(nodes_raw):
        nname = n.get("name") or f"node-{i + 1}"
        nip = str(n.get("ip", ""))
        try:
            ipaddress.ip_address(nip)
        except ValueError:
            problems.append(f"nodes[{i}] ({nname}): invalid or placeholder IP: {nip!r}")
        if nip in seen_ips:
            problems.append(f"nodes[{i}] ({nname}): duplicate IP {nip}")
        seen_ips.add(nip)
        nodes.append(Node(name=nname, ip=nip, bootstrap_leader=bool(n.get("bootstrap_leader", False))))

    leaders = [n for n in nodes if n.bootstrap_leader]
    if len(leaders) == 0 and nodes:
        nodes[0].bootstrap_leader = True  # default: first node
    elif len(leaders) > 1:
        problems.append("exactly one node may have bootstrap_leader: true")

    # --- ssh ---
    ssh = SSHConfig(
        user=_get(raw, "ssh.user", "root"),
        become=bool(_get(raw, "ssh.become", False)),
        auth=_get(raw, "ssh.auth", "key"),
        key_file=_get(raw, "ssh.key_file"),
        port=int(_get(raw, "ssh.port", 22)),
    )
    if ssh.auth not in VALID_SSH_AUTH:
        problems.append(f"ssh.auth must be one of {sorted(VALID_SSH_AUTH)}, got {ssh.auth!r}")
    if ssh.auth == "key":
        if not ssh.key_file:
            problems.append("ssh.auth is 'key' but ssh.key_file is not set")
        elif not Path(os.path.expanduser(ssh.key_file)).exists():
            problems.append(f"ssh.key_file does not exist: {ssh.key_file}")

    # --- network ---
    net = NetworkConfig(
        vip=str(_get(raw, "network.vip", "")),
        interface=_get(raw, "network.interface", "ens18"),
        expected_mtu=int(_get(raw, "network.expected_mtu", 1400)),
        vrrp_router_id=int(_get(raw, "network.vrrp.router_id", 51)),
        check_l2_adjacency=bool(_get(raw, "network.check_l2_adjacency", True)),
    )
    # VIP / VRRP only apply to the HA topology — a single node has nothing to
    # fail over to, so network.vip is neither required nor validated here.
    if topology == "ha":
        try:
            vip_addr = ipaddress.ip_address(net.vip)
            for n in nodes:
                try:
                    node_addr = ipaddress.ip_address(n.ip)
                    # crude but useful: same /24 as the VIP → VRRP plausible
                    if isinstance(vip_addr, ipaddress.IPv4Address) and isinstance(node_addr, ipaddress.IPv4Address):
                        if vip_addr.packed[:3] != node_addr.packed[:3]:
                            problems.append(
                                f"VIP {net.vip} and node {n.name} ({n.ip}) are not in the same /24 — "
                                f"VRRP requires L2 adjacency; double-check this is intentional"
                            )
                except ValueError:
                    pass
        except ValueError:
            problems.append(f"network.vip: invalid or placeholder IP: {net.vip!r}")
        if net.vip in seen_ips:
            problems.append(f"network.vip {net.vip} collides with a node IP")

    # --- tls ---
    tls = TLSConfig(
        provider=_get(raw, "tls.provider", "self_signed"),
        hostname=_get(raw, "tls.hostname", ""),
        acme=_get(raw, "tls.acme", {}) or {},
        import_=_get(raw, "tls.import", {}) or {},
    )
    if tls.provider not in VALID_TLS_PROVIDERS:
        problems.append(f"tls.provider must be one of {sorted(VALID_TLS_PROVIDERS)}, got {tls.provider!r}")
    if tls.provider == "none" and environment == "production":
        problems.append("tls.provider 'none' is refused when site.environment is 'production'")
    if tls.provider in {"self_signed", "acme", "import"} and not tls.hostname:
        problems.append(f"tls.hostname is required for provider {tls.provider!r}")
    if tls.provider == "acme":
        if not tls.acme.get("directory_url"):
            problems.append("tls.acme.directory_url is required for provider 'acme'")
        if not tls.acme.get("email"):
            problems.append("tls.acme.email is required for provider 'acme'")
    if tls.provider == "import":
        for k in ("fullchain", "privkey"):
            p = tls.import_.get(k)
            if not p:
                problems.append(f"tls.import.{k} is required for provider 'import'")
            elif not Path(os.path.expanduser(p)).exists():
                problems.append(f"tls.import.{k} does not exist: {p}")

    # --- monitor (optional) ---
    mon_ip = str(_get(raw, "monitor.ip", "") or "").strip()
    if mon_ip:
        try:
            ipaddress.ip_address(mon_ip)
        except ValueError:
            problems.append(f"monitor.ip: invalid IP: {mon_ip!r}")
        if mon_ip in seen_ips:
            problems.append(f"monitor.ip {mon_ip} collides with a node IP — "
                            "node IPs are already fully allowed through UFW")

    if problems:
        raise ConfigError(problems)

    state_file = Path(_get(raw, "provision.state_file", f".state/{name}.json"))

    return SiteConfig(
        name=name,
        environment=environment,
        topology=topology,
        nodes=nodes,
        ssh=ssh,
        network=net,
        tls=tls,
        state_file=state_file,
        refuse_existing=bool(_get(raw, "provision.refuse_existing", True)),
        authentik_tag=str(_get(raw, "authentik.tag", DEFAULT_AUTHENTIK_TAG[topology])),
        raw=raw,
    )
