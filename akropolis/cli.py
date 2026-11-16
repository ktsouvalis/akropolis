"""akropolis — provision and monitor an Authentik cluster (3-node HA or single-node).

    akro init                      # interactive wizard → config.<site>.yml
    akro provision config.yml      # phase runner (resumable)
    akro provision config.yml --replay preflight
    akro status    config.yml      # phase-by-phase status (state file only, no SSH)
    akro state encrypt config.yml  # encrypt an old plaintext state file
    akro state set config.yml KEY  # re-pin one value (hidden prompt)
    akro shutdown  config.yml      # gracefully stop the authentik backend(s)
    akro start      config.yml     # bring them back — requires a prior shutdown
    akro clean     config.yml      # tear the site down to bare VMs
    akro ldap-reconcile config.yml # repoint drifted LDAP source identifiers
    akro monitor   config.yml      # real-time cluster health dashboard
    akro logs      config.yml      # cluster-wide log viewer (SSH), --save to download
    akro update                    # install the latest release (zipapp binary only)
    akro check-update              # check for a newer release without installing it
    akro whats-new                 # show the changelog for the installed version
    akro licenses                  # show third-party license info
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from . import __version__
from . import changelog
from .config import ConfigError, SiteConfig, load
from .init_wizard import run_wizard
from .transcript import Transcript
from .update import check_for_update, check_update_now, self_update
from .phases.base import PhaseContext, run_phases
from .phases.authentik_phase import AuthentikPhase
from .phases.authentik_single_phase import AuthentikSinglePhase
from .phases.authentik_lifecycle import AuthentikShutdownPhase, AuthentikStartPhase
from .phases.restore_single_phase import RestoreSinglePhase
from .phases.handoff_single_phase import HandoffSinglePhase
from .phases.base_setup import BasePhase
from .phases.clean_phase import CleanPhase
from .phases.etcd_phase import EtcdPhase
from .phases.handoff_phase import HandoffPhase
from .phases.haproxy_phase import HAProxyPhase
from .phases import ldap_reconcile
from .phases.nginx_keepalived_phase import NginxKeepalivedPhase
from .phases.nginx_single_phase import NginxSinglePhase
from .phases.tls_phase import TLSPhase
from .phases.patroni_phase import PatroniPhase
from .phases.preflight import PreflightPhase
from .phases.restore_phase import RestorePhase
from .sshexec import Fleet
from .state import State, StateLocked, passphrase_source

console = Console()

PASSPHRASE_HELP = ("read the state passphrase from FILE (default: the "
                   "AKRO_STATE_PASSPHRASE env var, else a prompt)")

# Ordered phase pipeline — topology-dependent. `ha` is the full 3-node stack;
# `single` drops etcd/Patroni/HAProxy/keepalived entirely (see config.py
# DEFAULT_AUTHENTIK_TAG / REQUIRED_FREE_PORTS_SINGLE for the reasoning).
# Both topologies now get a reverse proxy that terminates public TLS and
# serves a maintenance page while `akro shutdown` has authentik down:
# `ha`'s is containerized + VRRP (nginx_keepalived_phase.py), `single`'s is
# bare-metal (nginx_single_phase.py) — condensed for one node, no VRRP/
# keepalived, no distribution keypair. `clean` is topology-aware too (see
# clean_phase.py) — invoked as its own subcommand, not part of either
# pipeline below.
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
    NginxSinglePhase(),
    RestoreSinglePhase(),   # no-op unless restore.sql_file is set
    HandoffSinglePhase(),
]


def pipeline_for(topology: str) -> list:
    return PIPELINE_SINGLE if topology == "single" else PIPELINE_HA


def _transcript_path(cfg: SiteConfig, command: str) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%S")
    return cfg.state_file.parent / f"{cfg.name}.{command}.{ts}.transcript.log"


def _preauth_sudo(cfg: SiteConfig, fleet: Fleet, password: str | None) -> bool:
    """Prove sudo works on every node BEFORE the first phase runs.

    Without this the credential is first exercised wherever a phase happens
    to need root — which, for `restore`, is the `docker compose stop server
    worker` step, three checks deep and immediately before the destructive
    part. A mistyped password there aborts a run that had already reported
    progress, and the operator has to work out how much of it happened. Three
    attempts, then give up: better to re-run the command than to sit at a
    prompt an automated invocation can never answer.
    """
    if not cfg.ssh.become:
        return True
    for attempt in range(3):
        bad: list[str] = []
        for conn in fleet:
            try:
                conn.connect()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]cannot reach {conn.node.name} ({conn.node.ip}):[/red] {exc}")
                return False
            if conn.run("id -u").out == "0":
                continue  # already root, nothing to escalate
            if not conn.run("true", sudo=True).ok:
                bad.append(conn.node.name)
        if not bad:
            return True
        console.print(f"[yellow]sudo failed on: {', '.join(bad)}[/yellow]")
        if attempt == 2:
            console.print("[red]sudo authentication failed three times — stopping before "
                          "any phase runs.[/red]")
            return False
        hint = "Enter = reuse SSH password" if password else "Enter = try passwordless sudo"
        retry = getpass.getpass(f"sudo password for {cfg.ssh.user} ({hint}): ") or password
        for conn in fleet:
            conn._sudo_password = retry
    return False


def _unlocked_state(cfg: SiteConfig, args: argparse.Namespace) -> State | None:
    """Load the site state and decrypt its pinned values (asks for the
    passphrase, or a new one on a site with nothing encrypted yet)."""
    state = State(cfg.state_file, cfg.name,
                  passphrase_source(getattr(args, "passphrase_file", None), cfg.name))
    try:
        state.unlock()
    except (StateLocked, OSError) as exc:
        console.print(f"[red]{exc}[/red]")
        return None
    return state


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

    state = _unlocked_state(cfg, args)
    if state is None:
        return 2
    transcript = Transcript(_transcript_path(cfg, "provision"))
    console.print(f"[dim]transcript: {transcript.path} "
                  "(every command run on every node this session — mode 0600)[/dim]")

    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}: ")

    sudo_password = None
    if cfg.ssh.become:
        hint = ("Enter = reuse SSH password" if password
                else "Enter = try passwordless sudo")
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} ({hint}): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password, transcript=transcript)
    if not _preauth_sudo(cfg, fleet, password):
        fleet.close()
        transcript.close()
        return 2
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
        transcript.close()
    return 0 if ok else 1


_STATUS_ICON = {
    "done": "[green]✔ done[/green]",
    "failed": "[red]✘ failed[/red]",
    "declined": "[yellow]⦸ declined[/yellow]",
    "skipped": "[yellow]⦸ skipped[/yellow]",
    "pending": "[dim]… pending[/dim]",
}


def phase_rows(pipeline: list, state: State) -> list[dict]:
    """One row per pipeline phase, in run order, merging in whatever `state`
    recorded for it. A phase absent from state.json is "pending" — it has
    never been attempted. Only "done" is skipped by the next `provision`;
    every other status (including "failed"/"declined"/"skipped") re-runs.
    """
    rows = []
    for phase in pipeline:
        entry = state.data["phases"].get(phase.name, {})
        rows.append({
            "name": phase.name,
            "status": entry.get("status", "pending"),
            "updated_at": entry.get("updated_at", ""),
            # state files from before 2.9.2 can carry a stale error on a done
            # phase (a failed attempt, then a successful replay)
            "error": entry.get("error", "") if entry.get("status") != "done" else "",
            "optional": phase.optional,
        })
    return rows


def cmd_status(args: argparse.Namespace) -> int:
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        return 2

    state = State(cfg.state_file, cfg.name)
    rows = phase_rows(pipeline_for(cfg.topology), state)

    console.print(f"[bold]{cfg.name}[/bold] [dim]({cfg.topology}, {cfg.environment})[/dim]")
    # Two separate print()s, not one line — a long state_file path must never
    # be able to word-wrap into the note and split it across lines (bit us in
    # CI, where the runner's tmp path is long enough to wrap a one-line
    # version of this against Rich's default console width).
    console.print(f"state file: {cfg.state_file}")
    if not cfg.state_file.exists():
        console.print("[dim](doesn't exist yet — nothing provisioned)[/dim]")
    elif state.plaintext_on_disk:
        console.print("[yellow]pinned secrets are in plain text — run "
                      f"`akro state encrypt {args.config}`[/yellow]")
    elif state.encrypted:
        console.print(f"[dim]pinned values (encrypted): "
                      f"{', '.join(state.generated_keys())}[/dim]")
    console.print()

    table = Table(box=None, pad_edge=False)
    table.add_column("phase")
    table.add_column("status")
    table.add_column("updated")
    table.add_column("detail")
    for row in rows:
        name = row["name"] + (" [dim](optional)[/dim]" if row["optional"] else "")
        table.add_row(name, _STATUS_ICON.get(row["status"], row["status"]),
                      row["updated_at"], row["error"])
    console.print(table)

    remaining = [r for r in rows if r["status"] != "done"]
    console.print()
    if remaining:
        nxt = remaining[0]
        console.print(f"next up: [bold]{nxt['name']}[/bold] (currently {nxt['status']}) "
                       "— runs on the next `provision`")
    else:
        console.print("[green]all phases done.[/green]")
    return 0


def _lifecycle_cmd(args: argparse.Namespace, phase, command: str) -> int:
    """Shared driver for `shutdown`/`start`: same connect/confirm/run shape as
    `provision`, but for a single ad-hoc phase outside the pipeline. Works on
    both topologies — the phase itself scopes the docker compose command
    (unscoped on ha, `server worker` on single, to leave postgresql running).
    """
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        return 2

    state = State(cfg.state_file, cfg.name)
    transcript = Transcript(_transcript_path(cfg, command))
    console.print(f"[dim]transcript: {transcript.path} "
                  "(every command run on every node this session — mode 0600)[/dim]")

    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}: ")
    sudo_password = None
    if cfg.ssh.become:
        hint = ("Enter = reuse SSH password" if password
                else "Enter = try passwordless sudo")
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} ({hint}): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password, transcript=transcript)
    if not _preauth_sudo(cfg, fleet, password):
        fleet.close()
        transcript.close()
        return 2
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)

    try:
        # replay=True: these are ad-hoc operational commands, not resumable
        # pipeline steps — a prior "done" must never make a later invocation
        # a silent no-op.
        ok = run_phases([phase], ctx, replay=True)
    finally:
        fleet.close()
        transcript.close()
    return 0 if ok else 1


def cmd_shutdown(args: argparse.Namespace) -> int:
    return _lifecycle_cmd(args, AuthentikShutdownPhase(), "shutdown")


def cmd_start(args: argparse.Namespace) -> int:
    return _lifecycle_cmd(args, AuthentikStartPhase(), "start")


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
    transcript = Transcript(_transcript_path(cfg, "clean"))
    console.print(f"[dim]transcript: {transcript.path} "
                  "(every command run on every node this session — mode 0600)[/dim]")
    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}: ")
    sudo_password = None
    if cfg.ssh.become:
        hint = ("Enter = reuse SSH password" if password
                else "Enter = try passwordless sudo")
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} ({hint}): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password, transcript=transcript)
    if not _preauth_sudo(cfg, fleet, password):
        fleet.close()
        transcript.close()
        return 2
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)
    phase = CleanPhase()
    fleet.current_phase = phase.name
    transcript.note(f"phase: {phase.name}")

    console.rule("clean")
    console.print("[bold]plan:[/bold]")
    for line in phase.plan(ctx):
        console.print(f"  • {line}")
    # destruction earns the typed-name gate in EVERY environment
    console.print(f"[bold red]type the site name to tear it down:[/bold red]")
    if input("> ").strip() != cfg.name:
        console.print("[yellow]not confirmed — nothing touched.[/yellow]")
        fleet.close()
        transcript.close()
        return 1

    try:
        phase.apply(ctx)
        ok = phase.verify(ctx)
    finally:
        ctx.end_status()
        fleet.close()
        transcript.close()
    return 0 if ok else 1


def cmd_ldap_reconcile(args: argparse.Namespace) -> int:
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        return 2

    state = State(cfg.state_file, cfg.name)
    transcript = Transcript(_transcript_path(cfg, "ldap-reconcile"))
    console.print(f"[dim]transcript: {transcript.path} "
                  "(every command run on every node this session — mode 0600)[/dim]")

    password = None
    if cfg.ssh.auth == "password":
        password = getpass.getpass(f"SSH password for {cfg.ssh.user}: ")
    sudo_password = None
    if cfg.ssh.become:
        hint = ("Enter = reuse SSH password" if password
                else "Enter = try passwordless sudo")
        sudo_password = getpass.getpass(
            f"sudo password for {cfg.ssh.user} ({hint}): ") or password

    fleet = Fleet(cfg.nodes, cfg.ssh, password, sudo_password, transcript=transcript)
    if not _preauth_sudo(cfg, fleet, password):
        fleet.close()
        transcript.close()
        return 2
    fleet.current_phase = "ldap-reconcile"
    transcript.note("phase: ldap-reconcile")
    ctx = PhaseContext(cfg=cfg, state=state, fleet=fleet)

    try:
        rc = ldap_reconcile.run(ctx, source=args.source)
    finally:
        ctx.end_status()
        fleet.close()
        transcript.close()
    return rc


def cmd_monitor(args: argparse.Namespace) -> int:
    # Imported here, not at module level: textual/requests/urllib3/psycopg2
    # are only needed by this subcommand, and `--help`/every other command
    # should not pay for importing them.
    from .monitor import dashboard

    dashboard.run(args.config)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    from .monitor import logs

    logs.run(args.config, args.last, args.save, args.level)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    return self_update(__version__)


def cmd_check_update(args: argparse.Namespace) -> int:
    return check_update_now(__version__)


def cmd_licenses(args: argparse.Namespace) -> int:
    from . import licenses

    console.print(Markdown(licenses.report()))
    return 0


def cmd_whats_new(args: argparse.Namespace) -> int:
    text = changelog.load()
    if args.all:
        console.print(Markdown(text))
        return 0

    version = args.version or __version__
    entry = changelog.entry_for(version, text)
    if entry is None:
        console.print(f"[yellow]no changelog entry for {version}.[/yellow] "
                      "Run `akro whats-new --all` for the full history.")
        return 1
    console.print(Markdown(entry))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """encrypt / passwd / set / get on the pinned values of one site."""
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        console.print("[red]config problems:[/red]")
        for p in exc.problems:
            console.print(f"  ✘ {p}")
        return 2
    if not cfg.state_file.exists():
        console.print(f"[red]no state file at {cfg.state_file}[/red]")
        return 2
    state = _unlocked_state(cfg, args)
    if state is None:
        return 2
    gen = state.data["generated"]

    if args.action == "encrypt":
        state.save()
        console.print(f"[green]{cfg.state_file}: {len(gen)} pinned value(s) encrypted.[/green] "
                      "Delete or encrypt any plain copies (.bak, .cleaned-*), and "
                      "remember older copies stay in backups until they expire.")
        return 0
    if args.action == "passwd":
        state.change_passphrase()
        console.print("[green]passphrase changed.[/green]")
        return 0

    if args.key not in gen:
        console.print(f"[red]no pinned value {args.key!r}.[/red] Pinned: "
                      + ", ".join(sorted(gen)))
        return 2
    if args.action == "get":
        value = gen[args.key]
        print(value if isinstance(value, str) else json.dumps(value))
        return 0
    # set
    if not isinstance(gen[args.key], str):
        console.print(f"[red]{args.key} is not a plain string value; refusing to "
                      "overwrite it from a prompt.[/red]")
        return 2
    value = getpass.getpass(f"new value for {args.key}: ")
    if not value or getpass.getpass("repeat it: ") != value:
        console.print("[yellow]empty or not matching — nothing changed.[/yellow]")
        return 1
    gen[args.key] = value
    state.save()
    console.print(f"[green]{args.key} re-pinned.[/green] Replay the phase that "
                  "renders it (e.g. `--replay authentik`) to apply it on the nodes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="akro", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"akropolis {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="interactive wizard → write a site config file")
    p_init.add_argument("-o", "--output", help="output path (default: config.<site>.yml)")
    p_init.set_defaults(func=cmd_init)

    p_prov = sub.add_parser("provision", help="run the phase pipeline against a site")
    p_prov.add_argument("config", help="path to config.<site>.yml")
    p_prov.add_argument("--passphrase-file", metavar="FILE", help=PASSPHRASE_HELP)
    p_prov.add_argument("--replay", nargs="+", metavar="PHASE",
                        help="re-run specific completed phase(s)")
    p_prov.add_argument("--only", nargs="+", metavar="PHASE",
                        help="run only the named phase(s), e.g. --only preflight")
    p_prov.set_defaults(func=cmd_provision)

    p_status = sub.add_parser("status", help="phase-by-phase provisioning status for "
                              "a site (reads the state file only — no SSH, no prompts)")
    p_status.add_argument("config", help="path to config.<site>.yml")
    p_status.set_defaults(func=cmd_status)

    p_state = sub.add_parser("state", help="manage the encrypted pinned values of a "
                             "site: encrypt an old plaintext file, change the "
                             "passphrase, re-pin or show one value")
    p_state.add_argument("action", choices=["encrypt", "passwd", "set", "get"])
    p_state.add_argument("config", help="path to config.<site>.yml")
    p_state.add_argument("key", nargs="?", help="pinned value name (set / get)")
    p_state.add_argument("--passphrase-file", metavar="FILE", help=PASSPHRASE_HELP)
    p_state.set_defaults(func=cmd_state)

    p_shutdown = sub.add_parser("shutdown", help="gracefully stop the authentik "
                                "server+worker (ha: on all 3 nodes, other services "
                                "left running; single: postgresql left running)")
    p_shutdown.add_argument("config", help="path to config.<site>.yml")
    p_shutdown.set_defaults(func=cmd_shutdown)

    p_start = sub.add_parser("start", help="start the authentik backend(s) again — "
                             "refuses unless `shutdown` last completed gracefully")
    p_start.add_argument("config", help="path to config.<site>.yml")
    p_start.set_defaults(func=cmd_start)

    p_clean = sub.add_parser("clean", help="tear the site down to bare VMs "
                             "(reverse build order; typed site-name confirmation)")
    p_clean.add_argument("config", help="path to config.<site>.yml")
    p_clean.add_argument("--i-know-this-is-production", action="store_true",
                         help="required additionally when site.environment is production")
    p_clean.set_defaults(func=cmd_clean)

    p_ldap = sub.add_parser("ldap-reconcile", help="repoint Authentik LDAP source "
                            "identifiers after an out-of-band change to a source's "
                            "own object uniqueness field, e.g. entryUUID (same user "
                            "account, every configured source, never touches LDAP "
                            "itself)")
    p_ldap.add_argument("config", help="path to config.<site>.yml")
    p_ldap.add_argument("--source", metavar="SLUG",
                        help="only reconcile this LDAP source slug (default: "
                        "every configured LDAP source)")
    p_ldap.set_defaults(func=cmd_ldap_reconcile)

    p_mon = sub.add_parser("monitor", help="real-time TUI dashboard for the full "
                           "Authentik HA stack (ha topology only)")
    p_mon.add_argument("config", help="path to config.<site>.monitor.yml (the "
                       "handoff phase emits this, not config.<site>.yml)")
    p_mon.set_defaults(func=cmd_monitor)

    p_logs = sub.add_parser("logs", help="cluster-wide log viewer over SSH "
                            "(ha topology only)")
    p_logs.add_argument("config", help="path to config.<site>.monitor.yml (the "
                        "handoff phase emits this, not config.<site>.yml)")
    p_logs.add_argument("--last", type=int, default=24, metavar="HOURS",
                        help="hours of logs to fetch (default: 24)")
    p_logs.add_argument("--save", metavar="FILE",
                        help="write a plain-text report to FILE instead of showing the TUI")
    p_logs.add_argument("--level", default="warning",
                        choices=["debug", "info", "warning", "error"],
                        help="minimum severity to include (default: warning)")
    p_logs.set_defaults(func=cmd_logs)

    p_update = sub.add_parser("update", help="download and install the latest akropolis "
                              "release (zipapp binary only)")
    p_update.set_defaults(func=cmd_update)

    p_check_update = sub.add_parser("check-update", help="check (bypassing the cache) "
                                    "whether a newer akropolis release exists, without "
                                    "installing it; exit 1 if one is available")
    p_check_update.set_defaults(func=cmd_check_update)

    p_licenses = sub.add_parser("licenses", help="show third-party license info for "
                                "every package actually bundled/installed right now")
    p_licenses.set_defaults(func=cmd_licenses)

    p_whats_new = sub.add_parser("whats-new", help="show the CHANGELOG.md entry for "
                                 "the installed version")
    p_whats_new.add_argument("--all", action="store_true",
                             help="show the full changelog instead of just this version")
    p_whats_new.add_argument("--version", metavar="VERSION",
                             help="show the entry for a specific version instead of "
                             "the one installed")
    p_whats_new.set_defaults(func=cmd_whats_new)

    args = parser.parse_args(argv)
    if args.command == "state" and args.action in ("set", "get") and not args.key:
        parser.error(f"state {args.action} needs a KEY (see `akro status` for the names)")

    if args.command not in ("update", "check-update"):
        try:
            latest = check_for_update(__version__)
        except Exception:  # noqa: BLE001 -- a version check must never break a real command
            latest = None
        if latest:
            console.print(
                f"[yellow]a new akropolis release is available: "
                f"{__version__} → {latest}[/yellow] [dim](run `akro update`)[/dim]"
            )

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
