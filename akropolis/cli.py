"""akropolis — provision and monitor an Authentik cluster (3-node HA or single-node).

    akropolis init                      # interactive wizard → config.<site>.yml
    akropolis provision config.yml      # phase runner (resumable)
    akropolis provision config.yml --replay preflight
    akropolis clean     config.yml      # tear the site down to bare VMs
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
from .phases.authentik_single_phase import AuthentikSinglePhase
from .phases.authentik_certs_phase import AuthentikCertsPhase
from .phases.base_setup import BasePhase
from .phases.clean_phase import CleanPhase
from .phases.etcd_phase import EtcdPhase
from .phases.handoff_phase import HandoffPhase
from .phases.haproxy_phase import HAProxyPhase
from .phases.nginx_keepalived_phase import NginxKeepalivedPhase
from .phases.tls_phase import TLSPhase
from .phases.patroni_phase import PatroniPhase
from .phases.preflight import PreflightPhase
from .phases.restore_phase import RestorePhase
from .sshexec import Fleet
from .state import State

console = Console()

# Ordered phase pipeline — topology-dependent. `ha` is the full 3-node stack;
# `single` drops etcd/Patroni/HAProxy/keepalived entirely (see config.py
# DEFAULT_AUTHENTIK_TAG / REQUIRED_FREE_PORTS_SINGLE for the reasoning), and
# has no `tls`/nginx phase either — authentik's own core webserver serves
# HTTPS directly (port 443 — see authentik-single-env.j2), so `certs` talks
# to authentik's own certificate discovery + Web Certificate API instead of
# rendering an nginx cert directory (see authentik_certs_phase.py). NOTE:
# single-node `restore` isn't wired in yet, and `clean` doesn't know single's
# paths yet either — next patches.
PIPELINE_HA = [
    PreflightPhase(),
    BasePhase(),
    EtcdPhase(),
    PatroniPhase(),
    HAProxyPhase(),
    TLSPhase(),
    NginxKeepalivedPhase(),
    AuthentikPhase(),
    RestorePhase(),   # no-op unless restore.sql_file is set
    HandoffPhase(),
]
PIPELINE_SINGLE = [
    PreflightPhase(),
    BasePhase(),
    AuthentikSinglePhase(),
    AuthentikCertsPhase(),
]


def pipeline_for(topology: str) -> list:
    return PIPELINE_SINGLE if topology == "single" else PIPELINE_HA


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
        hint = ("Enter = reuse SSH password" if password
                else "Enter = try passwordless sudo")
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} ({hint}): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password)
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)

    if args.replay:
        for name in args.replay:
            state.mark_phase(name, "pending")

    pipeline = pipeline_for(cfg.topology)
    phases = pipeline
    if args.only:
        phases = [p for p in pipeline if p.name in args.only]
        missing = set(args.only) - {p.name for p in phases}
        if missing:
            console.print(f"[red]unknown phase(s): {', '.join(sorted(missing))}[/red]")
            return 2

    try:
        # --replay works purely by marking the named phases pending above;
        # only --only bypasses the done-skip (it names phases explicitly).
        ok = run_phases(phases, ctx, replay=bool(args.only))
    finally:
        fleet.close()
    return 0 if ok else 1


def cmd_clean(args: argparse.Namespace) -> int:
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        return 2

    if cfg.environment == "production" and not args.i_know_this_is_production:
        console.print("[red]refusing to clean a production site.[/red] If this really "
                      "is a teardown of production, add --i-know-this-is-production.")
        return 2

    state = State(cfg.state_file, cfg.name)
    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}: ")
    sudo_password = None
    if cfg.ssh.become:
        hint = ("Enter = reuse SSH password" if password
                else "Enter = try passwordless sudo")
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} ({hint}): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password)
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)
    phase = CleanPhase()

    console.rule("clean")
    console.print("[bold]plan:[/bold]")
    for line in phase.plan(ctx):
        console.print(f"  • {line}")
    # destruction earns the typed-name gate in EVERY environment
    console.print(f"[bold red]type the site name to tear it down:[/bold red]")
    if input("> ").strip() != cfg.name:
        console.print("[yellow]not confirmed — nothing touched.[/yellow]")
        fleet.close()
        return 1

    try:
        phase.apply(ctx)
        ok = phase.verify(ctx)
    finally:
        ctx.end_status()
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

    p_clean = sub.add_parser("clean", help="tear the site down to bare VMs "
                             "(reverse build order; typed site-name confirmation)")
    p_clean.add_argument("config", help="path to config.<site>.yml")
    p_clean.add_argument("--i-know-this-is-production", action="store_true",
                         help="required additionally when site.environment is production")
    p_clean.set_defaults(func=cmd_clean)

    p_mon = sub.add_parser("monitor", help="run the monitor (stub)")
    p_mon.add_argument("config", help="path to config.<site>.yml")
    p_mon.set_defaults(func=cmd_monitor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
