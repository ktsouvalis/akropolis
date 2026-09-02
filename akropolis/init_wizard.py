"""`akropolis init` — interactive wizard that materializes answers into a config file.

The wizard is the front door for first-time use; `provision` only ever reads
the file. Validation happens as you type, and again on load.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import yaml
from rich.console import Console

console = Console()


def _ask(prompt: str, default: str | None = None, validate=None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{prompt}{suffix}: ").strip() or (default or "")
        if not raw:
            console.print("[yellow]a value is required[/yellow]")
            continue
        if validate:
            err = validate(raw)
            if err:
                console.print(f"[yellow]{err}[/yellow]")
                continue
        return raw


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    return _ask(f"{prompt} ({'/'.join(choices)})", default,
                lambda v: None if v in choices else f"must be one of: {', '.join(choices)}")


def _valid_ip(v: str) -> str | None:
    try:
        ipaddress.ip_address(v)
        return None
    except ValueError:
        return f"{v!r} is not a valid IP address"


def run_wizard(output: str | None = None) -> Path:
    console.print("[bold]akropolis init[/bold] — answers are written to a config file; "
                  "provisioning always runs from the file, so you can review or edit it first.\n")

    site = _ask("site name (short, e.g. uop-test)")
    env = _ask_choice("environment", ["lab", "production"], "lab")

    nodes = []
    for i in range(1, 4):
        ip = _ask(f"node {i} IP", validate=_valid_ip)
        name = _ask(f"node {i} name", f"ak-node-{i}")
        nodes.append({"name": name, "ip": ip, **({"bootstrap_leader": True} if i == 1 else {})})

    vip = _ask("VIP (virtual IP for keepalived)", validate=_valid_ip)
    iface = _ask("network interface on the nodes", "ens18")
    mtu = _ask("expected MTU (1400 for VXLAN overlay, 1500 for flat L2)", "1400",
               lambda v: None if v.isdigit() else "must be a number")

    user = _ask("SSH user", "root")
    auth = _ask_choice("SSH auth", ["key", "agent", "password"], "key")
    key_file = _ask("SSH private key file", "~/.ssh/id_ed25519") if auth == "key" else None

    tls_choices = ["none", "self_signed", "acme", "import"]
    default_tls = "self_signed" if env == "lab" else "acme"
    provider = _ask_choice("TLS provider", tls_choices, default_tls)
    while provider == "none" and env == "production":
        console.print("[yellow]'none' is refused for production sites — pick another provider[/yellow]")
        provider = _ask_choice("TLS provider", tls_choices, "acme")

    tls: dict = {"provider": provider}
    if provider != "none":
        tls["hostname"] = _ask("public hostname (FQDN, e.g. auth.example.gr)")
    if provider == "acme":
        tls["acme"] = {
            "directory_url": _ask("ACME directory URL",
                                  "https://acme-v02.api.letsencrypt.org/directory"),
            "email": _ask("ACME account email"),
            "staging": True,
        }
        console.print("[dim]acme.staging is set to true — flip it after one clean end-to-end run[/dim]")
    if provider == "import":
        tls["import"] = {"fullchain": _ask("path to fullchain.pem"),
                         "privkey": _ask("path to privkey.pem")}

    cfg = {
        "site": {"name": site, "environment": env},
        "provision": {"state_file": f".state/{site}.json", "refuse_existing": True},
        "ssh": {"user": user, "become": user != "root", "auth": auth,
                **({"key_file": key_file} if key_file else {}), "port": 22},
        "nodes": nodes,
        "network": {"vip": vip, "interface": iface, "expected_mtu": int(mtu),
                    "vrrp": {"router_id": 51}, "check_l2_adjacency": True},
        "tls": tls,
        "authentik": {"tag": "2026.5.6",
                      "bootstrap": {"create_admin": True, "create_api_token": True}},
        "secrets": {"source": "prompt"},
        "monitor": {"emit": True, "output": f"./config.{site}.monitor.yml"},
    }

    out = Path(output or f"config.{site}.yml")
    if out.exists():
        overwrite = input(f"{out} exists — overwrite? [y/N] ").strip().lower()
        if overwrite not in ("y", "yes"):
            raise SystemExit("aborted — nothing written")
    with open(out, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)
    console.print(f"\n[green]wrote {out}[/green] — review it, then run: "
                  f"[bold]akropolis provision {out}[/bold]")
    return out
