"""Phase framework.

Every phase runs plan → confirm → apply → verify:

  plan    — describe exactly what will happen (rendered diffs where relevant)
  confirm — lab: y/N; production: type the site name; read-only phases skip this
  apply   — do it
  verify  — health-gate; a phase that applies but fails verify is FAILED, and
            the runner stops (never proceeds onto an unhealthy foundation)

The runner is resumable: completed phases are skipped unless --replay.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from rich.console import Console

from ..config import SiteConfig
from ..sshexec import Fleet
from ..state import State

console = Console()


@dataclass
class Check:
    """One named check result inside a phase (used heavily by preflight)."""

    node: str
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False  # ok=False + warn=True → warning, not failure


@dataclass
class PhaseContext:
    cfg: SiteConfig
    state: State
    fleet: Fleet
    checks: list[Check] = field(default_factory=list)
    _status: object = field(default=None, repr=False)
    _status_text: str = field(default="", repr=False)

    # --- live progress ----------------------------------------------------
    # A phase announces what it is ABOUT to do; record() reports how it went.
    # Without this, a 10-minute image pull looks exactly like a hang — the
    # operator only ever saw ✔/✘ after the fact.
    def begin(self, node: str, name: str, detail: str = "") -> None:
        self.end_status()
        self._status_text = f"({node}) {name}" + (f" — {detail}" if detail else "")
        if console.is_terminal:
            try:
                self._status = console.status(f"[cyan]{self._status_text}[/cyan]",
                                              spinner="dots")
                self._status.start()
                return
            except Exception:  # noqa: BLE001 — fall through to the plain line
                self._status = None
        # piped/CI output: a live spinner renders nothing there, so the log
        # would show ✔/✘ with no trace of what was in flight — print instead
        console.print(f"  … {self._status_text}", markup=False, highlight=False)

    def tick(self, detail: str) -> None:
        """Update the live line in place (e.g. elapsed/timeout during a wait)."""
        if self._status is not None:
            self._status.update(f"[cyan]{self._status_text} — {detail}[/cyan]")

    def end_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def record(self, node: str, name: str, ok: bool, detail: str = "", warn: bool = False) -> Check:
        self.end_status()
        c = Check(node=node, name=name, ok=ok, detail=detail, warn=warn)
        self.checks.append(c)
        mark = "[green]✔[/green]" if ok else ("[yellow]⚠[/yellow]" if warn else "[red]✘[/red]")
        console.print(f"  {mark} ({node}) {name}" + (f" — {detail}" if detail else ""), markup=True, highlight=False)
        return c


class Phase(ABC):
    name: str = "unnamed"
    read_only: bool = False
    # OPTIONAL phases are the ones the pipeline can legitimately run without:
    # declining them is a choice, not an abort. Saying "no" to `restore`
    # means "this instance starts empty" — the correct next move is the
    # handoff, not stopping two phases short of a finished site and leaving
    # the operator to work out which --only invocation resumes it. A
    # REQUIRED phase declined still stops the runner: skipping etcd and
    # carrying on would build on a foundation that isn't there.
    optional: bool = False

    @abstractmethod
    def plan(self, ctx: PhaseContext) -> list[str]:
        """Return human-readable lines describing what apply() will do."""

    @abstractmethod
    def apply(self, ctx: PhaseContext) -> None: ...

    @abstractmethod
    def verify(self, ctx: PhaseContext) -> bool: ...

    def needs_confirm(self, ctx: PhaseContext) -> bool:
        """Whether apply() should be gated behind the y/N (or typed-name)
        confirmation. Defaults to "yes, unless read_only" — override this for
        a phase that is OPTIONAL and can be a genuine no-op depending on
        config (e.g. restore with no sql_file set): apply() will do nothing
        destructive in that case, so there is nothing to confirm, and forcing
        the confirmation just gives the operator a chance to accidentally
        halt the whole pipeline on a phase that was never going to touch
        anything. A phase whose plan() says "will be SKIPPED" should return
        False here for that state.
        """
        return not self.read_only


def _confirm(cfg: SiteConfig, phase: Phase, ctx: PhaseContext) -> bool:
    if not phase.needs_confirm(ctx):
        return True
    if cfg.environment == "production":
        console.print(
            f"[bold red]PRODUCTION[/bold red] site [bold]{cfg.name}[/bold] — "
            f"type the site name to apply phase [bold]{phase.name}[/bold]:"
        )
        return input("> ").strip() == cfg.name
    answer = input(f"Apply phase '{phase.name}'? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def run_phases(phases: list[Phase], ctx: PhaseContext, replay: bool = False) -> bool:
    for phase in phases:
        status = ctx.state.phase_status(phase.name)
        if status == "done" and not replay:
            console.print(f"[dim]phase {phase.name}: already done — skipping (use --replay to re-run)[/dim]")
            continue

        console.rule(f"phase: {phase.name}")
        ctx.checks.clear()

        if hasattr(ctx.fleet, "current_phase"):
            ctx.fleet.current_phase = phase.name
        transcript = getattr(ctx.fleet, "transcript", None)
        if transcript is not None:
            transcript.note(f"phase: {phase.name}")

        console.print("[bold]plan:[/bold]")
        for line in phase.plan(ctx):
            console.print(f"  • {line}")

        if not _confirm(ctx.cfg, phase, ctx):
            if phase.optional:
                console.print(f"[yellow]not confirmed — skipping optional phase "
                              f"'{phase.name}' and continuing.[/yellow]")
                ctx.state.mark_phase(phase.name, "skipped")
                continue
            console.print("[yellow]not confirmed — stopping.[/yellow]")
            ctx.state.mark_phase(phase.name, "declined")
            return False

        try:
            phase.apply(ctx)
        except Exception as exc:  # noqa: BLE001 — surface everything, then stop
            ctx.end_status()
            console.print(f"[red]apply failed:[/red] {exc}")
            ctx.state.mark_phase(phase.name, "failed", {"error": str(exc)})
            return False
        finally:
            ctx.end_status()

        if phase.verify(ctx):
            ctx.state.mark_phase(phase.name, "done")
            console.print(f"[green]phase {phase.name}: OK[/green]")
        else:
            ctx.end_status()
            ctx.state.mark_phase(phase.name, "failed")
            console.print(f"[red]phase {phase.name}: verify failed — stopping.[/red]")
            return False
        ctx.end_status()
    return True


class StubPhase(Phase):
    """Placeholder for phases not yet implemented; stops the runner cleanly."""

    def __init__(self, name: str):
        self.name = name

    def plan(self, ctx: PhaseContext) -> list[str]:
        return [f"(not implemented yet — provisioning stops here)"]

    def apply(self, ctx: PhaseContext) -> None:
        raise NotImplementedError(f"phase '{self.name}' is not implemented in this version")

    def verify(self, ctx: PhaseContext) -> bool:
        return False
