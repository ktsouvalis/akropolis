"""akropolis — provision and monitor a 3-node Authentik HA cluster.

    akropolis init                      # interactive wizard → config.<site>.yml
    akropolis provision config.yml      # phase runner (resumable)
    akropolis provision config.yml --replay preflight
    akropolis monitor   config.yml      # (stub — folds in ak-monitor later)
"""

from __future__ import annotations

import argparse
import getpass
import sys

from rich.console import Console

from . import __version__
from .config import ConfigError, load
from .init_wizard import run_wizard
from .phases.base import PhaseContext, run_phases
from .phases.authentik_phase import AuthentikPhase
from .phases.base_setup import BasePhase
from .phases.etcd_phase import EtcdPhase
from .phases.handoff_phase import HandoffPhase
from .phases.haproxy_phase import HAProxyPhase
from .phases.nginx_keepalived_phase import NginxKeepalivedPhase
from .phases.tls_phase import TLSPhase
from .phases.patroni_phase import PatroniPhase
from .phases.preflight import PreflightPhase
from .sshexec import Fleet
from .state import State

console = Console()

# Ordered phase pipeline — all phases implemented.
PIPELINE = [
    PreflightPhase(),
    BasePhase(),
    EtcdPhase(),
    PatroniPhase(),
    HAProxyPhase(),
    TLSPhase(),
    NginxKeepalivedPhase(),
    AuthentikPhase(),
    HandoffPhase(),
]


def cmd_init(args: argparse.Namespace) -> int:
    run_wizard(args.output)
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        return 2

    state = State(cfg.state_file, cfg.name)

    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}: ")

    sudo_password = None
    if cfg.ssh.become:
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} "
            "(Enter = reuse SSH password, or passwordless sudo): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password)
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)

    if args.replay:
        for name in args.replay:
            state.mark_phase(name, "pending")

    phases = PIPELINE
    if args.only:
        phases = [p for p in PIPELINE if p.name in args.only]
        missing = set(args.only) - {p.name for p in phases}
        if missing:
            console.print(f"[red]unknown phase(s): {', '.join(sorted(missing))}[/red]")
            return 2

    try:
        ok = run_phases(phases, ctx, replay=bool(args.replay or args.only))
    finally:
        fleet.close()
    return 0 if ok else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    console.print("[yellow]monitor: not folded in yet.[/yellow] For now run ak-monitor "
                  "with the emitted config from the handoff phase.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akropolis", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"akropolis {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="interactive wizard → write a site config file")
    p_init.add_argument("-o", "--output", help="output path (default: config.<site>.yml)")
    p_init.set_defaults(func=cmd_init)

    p_prov = sub.add_parser("provision", help="run the phase pipeline against a site")
    p_prov.add_argument("config", help="path to config.<site>.yml")
    p_prov.add_argument("--replay", nargs="+", metavar="PHASE",
                        help="re-run specific completed phase(s)")
    p_prov.add_argument("--only", nargs="+", metavar="PHASE",
                        help="run only the named phase(s), e.g. --only preflight")
    p_prov.set_defaults(func=cmd_provision)

    p_mon = sub.add_parser("monitor", help="run the monitor (stub)")
    p_mon.add_argument("config", help="path to config.<site>.yml")
    p_mon.set_defaults(func=cmd_monitor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
