"""handoff (single) — the pipeline's last phase, same job as the HA one:
emit the monitoring tool's config on the WORKSTATION and print the landing
card. Nothing touches the node here.

Much smaller than the HA version by construction: no VIP, no keepalived
priorities, no HAProxy/postgres credentials to hand out (PostgreSQL never
leaves the loopback interface — a remote monitor can't reach it directly
regardless of what credentials it's given). Just one node, one URL, one API
token that authentik_single_phase.py already proved working.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from ..remote import base_url as _base_url
from ..remote import render
from .base import Phase, PhaseContext, console


class HandoffSinglePhase(Phase):
    name = "handoff"

    def _out_path(self, ctx: PhaseContext) -> Path:
        mon = ctx.cfg.raw.get("monitor") or {}
        return Path(mon.get("output") or f"./config.{ctx.cfg.name}.monitor.yml")

    def _url(self, ctx: PhaseContext) -> str:
        return _base_url(ctx.cfg)

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        return [
            f"emit the monitor config to {self._out_path(ctx)} on THIS workstation "
            "(no node is touched)",
            "fill it from state: the proven Authentik API token, cert expiry "
            "if tls.provider is import",
            "print the landing card: admin URL and the akadmin bootstrap "
            "password (shown ONCE in this terminal — change it after first login)",
        ]

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        gen = ctx.state.data["generated"]
        out = self._out_path(ctx)
        node = cfg.nodes[0]

        content = render(
            "monitor-config-single.yml.j2",
            site_name=cfg.name,
            site_title=f"{cfg.name} — Authentik (single-node)",
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            node=node,
            ssh_user=cfg.ssh.user,
            ssh_key_file=cfg.ssh.key_file
            or "CHANGE_ME  # path to the private key the monitor should use",
            authentik_url=self._url(ctx),
            authentik_port=443,
            # self-signed (none/self_signed both mean this here) must not be
            # verified; only a real cert (acme/import) can be.
            verify_tls=cfg.tls.provider in ("acme", "import"),
            api_token=gen.get("authentik_bootstrap_token", ""),
            cert_expiry=gen.get("tls_cert_expiry", ""),
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        out.chmod(0o600)
        ctx.record("workstation", f"monitor config written: {out}", True, "mode 0600")

        # landing card
        console.rule("single-node ready")
        console.print(f"  admin URL : [bold]{self._url(ctx)}[/bold]")
        console.print("  username  : [bold]akadmin[/bold]")
        console.print(f"  password  : [bold]{gen.get('authentik_bootstrap_password', '?')}[/bold] "
                      "[yellow](shown once — change it after first login)[/yellow]")
        console.print(f"  monitor   : run your monitoring tool with [bold]{out}[/bold]")
        transcript = getattr(ctx.fleet, "transcript", None)
        if transcript is not None:
            console.print(f"  transcript: [bold]{transcript.path}[/bold] (every command "
                          "run on this node this session, secrets best-effort "
                          "redacted — mode 0600)")
        if not cfg.ssh.key_file:
            console.print("  [yellow]NOTE: ssh.key_file was not set in the site config — "
                          f"edit [bold]{out}[/bold] and replace the CHANGE_ME placeholder "
                          "under 'ssh:' with the private key path the monitor should "
                          "use, or its SSH-based checks will fail.[/yellow]")
        if cfg.tls.provider in ("none", "self_signed"):
            console.print("  [yellow]NOTE: serving authentik's own auto-generated "
                          "self-signed certificate (valid 1 year, authentik renews it "
                          "itself) — browsers will warn on first visit.[/yellow]")
        if cfg.tls.provider == "import" and gen.get("tls_cert_expiry"):
            console.print(f"  [yellow]NOTE: imported certificate has no auto-renewal — "
                          f"expires {gen['tls_cert_expiry']}.[/yellow]")

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        out = self._out_path(ctx)
        try:
            data = yaml.safe_load(out.read_text())
        except Exception as exc:  # noqa: BLE001
            ctx.record("workstation", "emitted file parses as YAML", False, str(exc))
            return False
        ctx.record("workstation", "emitted file parses as YAML", True, "")

        required = ["site_name", "nodes", "ports", "ssh", "scheme",
                    "services", "authentik", "credentials"]
        missing = [k for k in required if k not in (data or {})]
        ctx.record("workstation", "schema keys present", not missing,
                   f"missing: {missing}" if missing else f"{len(required)} top-level keys")
        token_ok = bool((data.get("credentials") or {}).get("authentik_api_token"))
        ctx.record("workstation", "API token embedded", token_ok,
                   "" if token_ok else "empty token — did the authentik phase run?")
        one_node = len((data.get("nodes") or {}).get("authentik", [])) == 1
        ctx.record("workstation", "1 node in the authentik group", one_node, "")
        return not missing and token_ok and one_node
