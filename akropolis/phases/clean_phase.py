"""clean — tear a provisioned cluster back down to bare VMs.

The inverse of the pipeline, for test iteration: run it, and `provision`
starts from a genuinely blank slate (preflight's refuse_existing passes,
secrets are re-generated, nothing left over can mask a bug).

Topology-aware (see STEPS/STEPS_SINGLE below) — invoked as its own
subcommand, not part of either provisioning pipeline, so it reads
`ctx.cfg.topology` directly rather than going through cli.py's
`pipeline_for`.

HA teardown runs in REVERSE build order — the same discipline as building,
for the same reason: the VIP goes first so nothing routes traffic at a
cluster being dismantled, and the database goes down before its DCS.

  keepalived (VIP) → nginx → authentik → haproxy → patroni/PostgreSQL data
  → etcd → TLS material (letsencrypt, webroot, certbot dist key) → UFW reset
  (ssh re-allowed BEFORE re-enable — same rule as building) → /etc/hosts block
  → restore-dump leftovers in /tmp

single-node has far less to remove: authentik + its containerized postgres
share one compose project (`docker compose down -v` also drops the named
postgres volume), there's no keepalived/haproxy/patroni/etcd to have ever
existed, and `/etc/letsencrypt` covers both the HA cluster's certbot
distribution key AND single-node's own renewal deploy hook, so it needed no
new step — just a shorter STEPS list so an operator doesn't see a confusing
"keepalived down" checkmark on a host that never had it.

What it deliberately does NOT touch, either topology:
  - packages (docker, postgresql-16, keepalived, certbot, chrony...) — apt
    state belongs to the operator's patching policy; removing data and config
    is what makes the next provision run honest
  - the hostname — the previous one is unknowable
  - anything outside the paths akropolis itself created

Safety: requires typing the site name in EVERY environment (not just
production — this is the one command whose entire purpose is destruction),
and refuses `site.environment: production` outright unless
`--i-know-this-is-production` is also given. The local state file is archived
to `.state/<site>.json.cleaned-<timestamp>` (pinned secrets kept for the
paper trail, 0600), then removed so the next run regenerates everything.
"""

from __future__ import annotations

import time

from .base import Phase, PhaseContext, console

# Reverse build order. Each entry: (label, command). Commands are idempotent —
# cleaning an already-clean or half-built node is a supported case (that is
# exactly what a failed provision leaves behind).
STEPS: list[tuple[str, str]] = [
    ("keepalived down (VIP released)",
     "systemctl disable --now keepalived 2>/dev/null; "
     "rm -f /etc/keepalived/keepalived.conf; true"),
    ("nginx down + removed",
     "cd /opt/nginx 2>/dev/null && docker compose down -v 2>/dev/null; "
     "rm -rf /opt/nginx; true"),
    ("authentik down + removed",
     "cd /opt/authentik 2>/dev/null && docker compose down -v 2>/dev/null; "
     "rm -rf /opt/authentik; true"),
    ("haproxy down + removed",
     "cd /opt/haproxy 2>/dev/null && docker compose down -v 2>/dev/null; "
     "rm -rf /opt/haproxy; true"),
    ("patroni + postgres data removed",
     "systemctl disable --now patroni 2>/dev/null; "
     "rm -rf /etc/patroni /var/lib/postgresql/16/patroni /opt/patroni "
     "/etc/systemd/system/patroni.service; systemctl daemon-reload; true"),
    ("etcd down + data removed",
     "cd /opt/etcd 2>/dev/null && docker compose down -v 2>/dev/null; "
     "rm -rf /opt/etcd; true"),
    ("TLS material removed",
     "rm -rf /etc/letsencrypt /var/www/certbot "
     "/root/.ssh/id_ed25519_certbot /root/.ssh/id_ed25519_certbot.pub; "
     "test -f /root/.ssh/authorized_keys && "
     "sed -i '/# akropolis-certbot/d' /root/.ssh/authorized_keys; true"),
    # ufw reset disables the firewall and drops every rule; ssh is re-allowed
    # BEFORE re-enabling — losing the session here would strand the node
    ("ufw reset (ssh re-allowed, left enabled)",
     "ufw --force reset && ufw default deny incoming && "
     "ufw default allow outgoing && ufw allow ssh && ufw --force enable"),
    ("/etc/hosts akropolis block removed",
     "sed -i '/# BEGIN akropolis/,/# END akropolis/d' /etc/hosts"),
    ("restore-dump leftovers removed",
     "rm -f /tmp/akropolis-restore-*.sql /tmp/akropolis-restore-*.sql.gz"),
]

# single-node: one compose project (authentik + its containerized postgres,
# `down -v` drops the named postgres volume too), no keepalived/haproxy/
# patroni/etcd ever existed, /etc/letsencrypt covers the renewal deploy hook
# (see authentik_certs_phase.py) the same way it covers the HA cluster's
# certbot key — no extra step needed for it.
STEPS_SINGLE: list[tuple[str, str]] = [
    ("authentik + postgres down + removed (incl. named volume)",
     "cd /opt/authentik 2>/dev/null && docker compose down -v 2>/dev/null; "
     "rm -rf /opt/authentik; true"),
    ("TLS material removed",
     "rm -rf /etc/letsencrypt /var/www/certbot; true"),
    ("ufw reset (ssh re-allowed, left enabled)",
     "ufw --force reset && ufw default deny incoming && "
     "ufw default allow outgoing && ufw allow ssh && ufw --force enable"),
    ("/etc/hosts akropolis block removed",
     "sed -i '/# BEGIN akropolis/,/# END akropolis/d' /etc/hosts"),
    ("restore-dump leftovers removed",
     "rm -f /tmp/akropolis-restore-*.sql /tmp/akropolis-restore-*.sql.gz"),
]

# Nothing akropolis-made may survive. Absence of each is verified per node.
GONE = [
    ("/opt/authentik", "test ! -e /opt/authentik"),
    ("/opt/nginx", "test ! -e /opt/nginx"),
    ("/opt/haproxy", "test ! -e /opt/haproxy"),
    ("/opt/etcd", "test ! -e /opt/etcd"),
    ("/etc/patroni", "test ! -e /etc/patroni"),
    ("postgres data dir", "test ! -e /var/lib/postgresql/16/patroni"),
    ("patroni unit", "test ! -e /etc/systemd/system/patroni.service"),
    ("keepalived conf", "test ! -e /etc/keepalived/keepalived.conf"),
    ("keepalived inactive", "! systemctl is-active --quiet keepalived"),
    ("no ak containers",
     "test -z \"$(docker ps -aq 2>/dev/null "
     "--filter name='authentik|nginx|haproxy|etcd')\""),
]

GONE_SINGLE = [
    ("/opt/authentik", "test ! -e /opt/authentik"),
    ("no ak containers",
     "test -z \"$(docker ps -aq 2>/dev/null --filter name='authentik')\""),
]


class CleanPhase(Phase):
    name = "clean"

    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        lines = [
            f"[red]DESTRUCTIVE[/red]: tear site [bold]{cfg.name}[/bold] "
            f"({', '.join(n.name for n in cfg.nodes)}) down to bare VMs",
        ]
        if cfg.topology == "ha":
            lines.append(
                "reverse build order: keepalived/VIP → nginx → authentik → haproxy → "
                "patroni + ALL postgres data → etcd + data → TLS material → UFW reset "
                "(ssh kept) → /etc/hosts block → /tmp dump leftovers")
        else:
            lines.append(
                "authentik + its containerized postgres (one compose project, "
                "docker compose down -v drops the named volume too) → TLS material "
                "→ UFW reset (ssh kept) → /etc/hosts block → /tmp dump leftovers")
        lines.append(
            "packages (docker, postgresql-16" + (", keepalived, certbot" if cfg.topology == "ha"
            else ", certbot") + ") and the hostname are left alone — data and config "
            "removal is what makes the next provision honest")
        if cfg.topology == "ha":
            lines.append("the VIP is released with the first step — anything still "
                         f"pointing at {cfg.network.vip} goes dark immediately")
        lines.append(f"local state archived to {ctx.cfg.state_file}.cleaned-<ts> then "
                     "removed → next provision regenerates every secret from scratch")
        return lines

    def apply(self, ctx: PhaseContext) -> None:
        steps = STEPS if ctx.cfg.topology == "ha" else STEPS_SINGLE
        for conn in ctx.fleet:
            node = conn.node.name
            for label, cmd in steps:
                ctx.begin(node, label)
                r = conn.run(cmd, timeout=300)
                ctx.record(node, label, r.ok, r.err if not r.ok else "")
                if not r.ok and "ufw" in label:
                    raise RuntimeError(f"{node}: ufw reset failed — stopping before "
                                       "anything can strand the SSH session")

        # local state: archive (secrets are a paper trail), then remove
        sf = ctx.cfg.state_file
        if sf.exists():
            archive = sf.with_name(sf.name + ".cleaned-"
                                   + time.strftime("%Y%m%d-%H%M%S"))
            sf.replace(archive)
            archive.chmod(0o600)
            ctx.record("workstation", "state archived + removed", True, str(archive))
            # this in-memory State would re-save pinned secrets on any later
            # mark; point it at the (now absent) path with a fresh dict
            ctx.state.data = {"site": ctx.cfg.name, "phases": {}, "generated": {}}
        else:
            ctx.record("workstation", "state file", True, "already absent")

    def verify(self, ctx: PhaseContext) -> bool:
        gone = GONE if ctx.cfg.topology == "ha" else GONE_SINGLE
        ok = True
        for conn in ctx.fleet:
            node = conn.node.name
            for label, cmd in gone:
                r = conn.run(cmd, timeout=30)
                ctx.record(node, f"verify gone: {label}", r.ok,
                           "" if r.ok else "still present")
                ok = ok and r.ok
        if ok:
            console.print("[green]nodes are bare — `akropolis provision` starts "
                          "from scratch (all secrets regenerated).[/green]")
        return ok
