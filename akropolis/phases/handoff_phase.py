"""handoff — the pipeline's last phase. Nothing touches the nodes here.

Emits the monitoring tool's config.yml on the WORKSTATION, filled entirely from
the site config and the pinned state — node groups, ports, keepalived base
priorities and track weight exactly as deployed (the monitor computes effective
VRRP priorities from these, so they must match reality, and here they do by
construction), the HAProxy stats and postgres credentials, and the Authentik
API token that the authentik phase already proved working against the API.

Then prints the operator's landing card: admin URL, the akadmin bootstrap
password (ONCE, to the terminal — it exists so you can log in; change it or
store it properly after first login), and where everything lives.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

from ..remote import base_url as _base_url
from ..remote import render
from .base import Phase, PhaseContext, console


class HandoffPhase(Phase):
    name = "handoff"

    def _out_path(self, ctx: PhaseContext) -> Path:
        mon = ctx.cfg.raw.get("monitor") or {}
        return Path(mon.get("output") or f"./config.{ctx.cfg.name}.monitor.yml")

    def _url(self, ctx: PhaseContext) -> str:
        return _base_url(ctx.cfg)

    def _priorities(self, ctx: PhaseContext) -> list[int]:
        raw = ((ctx.cfg.raw.get("network") or {}).get("vrrp") or {})
        return [int(p) for p in (raw.get("priorities") or [100, 90, 80])]

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        return [
            f"emit the monitor config to {self._out_path(ctx)} on THIS workstation "
            "(no node is touched)",
            "fill it from state: HAProxy stats + postgres credentials, the proven "
            "Authentik API token, keepalived priorities/track-weight as deployed",
            "print the landing card: admin URL and the akadmin bootstrap password "
            "(shown ONCE in this terminal — change it after first login)",
        ]

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        gen = ctx.state.data["generated"]
        out = self._out_path(ctx)

        content = render(
            "monitor-config.yml.j2",
            site_name=cfg.name,
            site_title=f"{cfg.name} — Authentik HA Cluster",
            generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            vip=cfg.network.vip,
            interface=cfg.network.interface,
            nodes=cfg.nodes,
            ssh_user=cfg.ssh.user,
            # Never guess a key path: with ssh.auth 'agent' there is no
            # key_file in the site config, and the monitor tool reads the key
            # from its own config — a wrong guess silently breaks its SSH
            # checks (keepalived state, service logs). Emit a placeholder and
            # tell the operator on the landing card.
            ssh_key_file=cfg.ssh.key_file
            or "CHANGE_ME  # path to the private key the monitor should use",
            authentik_url=self._url(ctx),
            # tls: none → nginx is plain HTTP on :80; anything else → HTTPS :443.
            # Emitting the scheme instead of assuming https is what keeps a lab
            # site's dashboard from showing every node DOWN.
            nginx_scheme="http" if cfg.tls.provider == "none" else "https",
            nginx_port=80 if cfg.tls.provider == "none" else 443,
            authentik_port=9443,
            # self-signed and lab certs must not be verified by the monitor
            verify_tls=cfg.tls.provider in ("acme", "import"),
            haproxy_stats_pass=gen.get("haproxy_stats_password", ""),
            api_token=gen.get("authentik_bootstrap_token", ""),
            postgres_password=gen.get("pg_superuser_password", ""),
            priorities=self._priorities(ctx),
            cert_expiry=gen.get("tls_cert_expiry", ""),
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        out.chmod(0o600)
        ctx.record("workstation", f"monitor config written: {out}", True, "mode 0600")

        # landing card
        console.rule("cluster ready")
        console.print(f"  admin URL : [bold]{self._url(ctx)}[/bold]")
        console.print("  username  : [bold]akadmin[/bold]")
        console.print(f"  password  : [bold]{gen.get('authentik_bootstrap_password', '?')}[/bold] "
                      "[yellow](shown once — change it after first login)[/yellow]")
        console.print(f"  monitor   : run your monitoring tool with [bold]{out}[/bold]")
        transcript = getattr(ctx.fleet, "transcript", None)
        if transcript is not None:
            console.print(f"  transcript: [bold]{transcript.path}[/bold] (every command "
                          "run on every node this session, secrets best-effort "
                          "redacted — mode 0600)")
        mon_ip = str(((cfg.raw.get("monitor") or {}).get("ip") or "")).strip() \
            or gen.get("monitor_ip", "")
        if mon_ip:
            console.print(f"  monitor ip: [bold]{mon_ip}[/bold] allowed through UFW "
                          "(etcd/PG/Patroni/stats/API ports) on every node")
        else:
            console.print("  [yellow]NOTE: no monitor IP was allowed through UFW — "
                          "the monitor's Patroni/etcd/stats columns will show DOWN "
                          "unless it runs from a node. Set monitor.ip and "
                          "--replay base.[/yellow]")
        if not cfg.ssh.key_file:
            console.print("  [yellow]NOTE: ssh.key_file was not set in the site config — "
                          f"edit [bold]{out}[/bold] and replace the CHANGE_ME placeholder "
                          "under 'ssh:' with the private key path the monitor should "
                          "use, or its SSH-based checks will fail.[/yellow]")
        if cfg.tls.provider == "none":
            console.print("  [yellow]NOTE: tls 'none' — nginx serves plain HTTP on :80. "
                          "The emitted config sets scheme.nginx: http; ak-monitor must "
                          "honour it, or every node shows DOWN on an https:// probe.[/yellow]")
        if gen.get("tls_acme_pending"):
            console.print("  [yellow]NOTE: ACME issuance is still pending — the cluster is "
                          "serving the placeholder cert.[/yellow]")
        if cfg.tls.provider == "acme" and (cfg.tls.acme or {}).get("staging"):
            console.print("  [yellow]NOTE: staging ACME cert in place — set "
                          "tls.acme.staging: false and --replay nginx-keepalived.[/yellow]")

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        out = self._out_path(ctx)
        try:
            data = yaml.safe_load(out.read_text())
        except Exception as exc:  # noqa: BLE001
            ctx.record("workstation", "emitted file parses as YAML", False, str(exc))
            return False
        ctx.record("workstation", "emitted file parses as YAML", True, "")

        required = ["site_name", "vip", "nodes", "ports", "ssh", "scheme",
                    "services", "authentik", "credentials", "keepalived"]
        missing = [k for k in required if k not in (data or {})]
        ctx.record("workstation", "schema keys present", not missing,
                   f"missing: {missing}" if missing else f"{len(required)} top-level keys")
        token_ok = bool((data.get("credentials") or {}).get("authentik_api_token"))
        ctx.record("workstation", "API token embedded", token_ok,
                   "" if token_ok else "empty token — did the authentik phase run?")
        groups_ok = all(len(data["nodes"].get(g, [])) == 3
                        for g in ("authentik", "patroni", "etcd", "haproxy"))
        ctx.record("workstation", "3 nodes in every service group", groups_ok, "")
        return not missing and token_ok and groups_ok
