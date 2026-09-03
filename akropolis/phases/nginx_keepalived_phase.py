"""nginx-keepalived — guide Step 6: TLS termination + least_conn load balancing,
and the VRRP virtual IP. Order matters and is enforced: nginx on all nodes and
verified individually FIRST, keepalived after.

nginx.conf is identical on all nodes except the /monitor location, which
returns per-node JSON so the monitoring tool (and you, with curl) can always
tell which node currently holds the VIP.

keepalived encodes the field learnings rather than the guide's first draft:
track-script weight -25 (eliminates priority ties under single failures) and
nopreempt in its canonical form — all instances state BACKUP with differing
priorities — so a recovered node never steals the VIP back and causes a second
flap. auth_pass is silently truncated by keepalived to 8 chars, so exactly 8
are generated and pinned in state.

nginx.conf changes on a running container are applied with `down && up -d`
(single-file bind-mount inode trap, guide 6.B.7 warning) — never `nginx -s
reload` after a conf push.

When the tls provider is `acme` and the staging marker is set, this phase
finalizes after the VIP is up: dedicated root SSH keypair for cert
distribution, certbot issuance over the webroot (the VIP-holding node serves
the HTTP-01 challenge), an akropolis-managed deploy hook for automatic
multi-node distribution on every renewal, and one manual hook run to swap the
placeholder immediately.
"""

from __future__ import annotations

import ipaddress
import secrets as pysecrets
import shlex
from importlib import resources

from ..remote import push_file, render, wait_for
from .base import Phase, PhaseContext


class NginxKeepalivedPhase(Phase):
    name = "nginx-keepalived"

    # ------------------------------------------------------------------ util
    def _tls_enabled(self, ctx: PhaseContext) -> bool:
        return ctx.cfg.tls.provider != "none"

    def _check_port(self, ctx: PhaseContext) -> int:
        return 443 if self._tls_enabled(ctx) else 80

    def _subnet(self, ctx: PhaseContext) -> str:
        ip = ctx.cfg.nodes[0].ip
        return ".".join(ip.split(".")[:3]) + ".0/24"

    def _priorities(self, ctx: PhaseContext) -> list[int]:
        raw = ((ctx.cfg.raw.get("network") or {}).get("vrrp") or {})
        prios = raw.get("priorities") or [100, 90, 80]
        return [int(p) for p in prios]

    def _auth_pass(self, ctx: PhaseContext) -> str:
        # keepalived silently truncates auth_pass to 8 chars — generate exactly 8
        return ctx.state.get_or_generate("keepalived_auth_pass",
                                         lambda: pysecrets.token_hex(4))

    def _acme_pending(self, ctx: PhaseContext) -> bool:
        return (ctx.cfg.tls.provider == "acme"
                and ctx.state.data["generated"].get("tls_acme_pending", False))

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        prios = self._priorities(ctx)
        lines = [
            "create /opt/nginx/{conf,certs,logs} and the certbot webroot on all nodes",
            "render nginx.conf per node (identical except the per-node /monitor JSON): "
            + ("HTTP->HTTPS redirect with ACME exception, TLS proxy :443 -> "
               "least_conn authentik_backend :9443"
               if self._tls_enabled(ctx) else
               "plain HTTP :80 proxy (provider 'none' — testing only)")
            + ", stub_status on :8080 for the monitor",
            "start nginx on all nodes (down && up on conf change — inode trap), "
            "verify each node individually BEFORE keepalived",
            "install keepalived + netcat; render keepalived.conf: track chk_nginx "
            f"(:{self._check_port(ctx)}, weight -25), nopreempt (canonical all-BACKUP form), "
            f"priorities {prios}, router_id {cfg.network.vrrp_router_id}, "
            "8-char auth_pass pinned in state",
            "start keepalived on all nodes; verify exactly one node holds the VIP "
            f"and https://{cfg.network.vip}/monitor answers",
        ]
        if self._acme_pending(ctx):
            a = cfg.tls.acme
            lines += [
                "ACME FINALIZATION (staged earlier): dedicated root SSH keypair for cert "
                "distribution, authorized on the other nodes (marker-managed)",
                f"certbot certonly --webroot for {cfg.tls.hostname} against "
                f"{a.get('directory_url')}" + (" [STAGING]" if a.get("staging") else ""),
                "install akropolis deploy hook (auto-distribution on every renewal), "
                "run it once to swap the placeholder cert now, pin expiry in state",
            ]
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        compose = resources.files("akropolis.templates") \
            .joinpath("nginx-compose.yml").read_text()

        # --- nginx on all nodes -------------------------------------------
        for conn in ctx.fleet:
            node = conn.node.name
            r = conn.run("mkdir -p /opt/nginx/conf /opt/nginx/certs /opt/nginx/logs "
                         "/var/www/certbot/.well-known/acme-challenge")
            ctx.record(node, "directories", r.ok, r.err if not r.ok else "")

            conf = render("nginx.conf.j2",
                          node=conn.node, nodes=cfg.nodes,
                          hostname=cfg.tls.hostname or "_",
                          tls_enabled=self._tls_enabled(ctx),
                          nodes_subnet=self._subnet(ctx),
                          stub_status_allow=list(
                              (cfg.raw.get("network") or {}).get("stub_status_allow", []) or []),
                          trusted_proxies=list(
                              (cfg.raw.get("network") or {}).get("trusted_proxies", []) or []))
            conf_changed = push_file(conn, conf, "/opt/nginx/conf/nginx.conf")
            compose_changed = push_file(conn, compose, "/opt/nginx/docker-compose.yml")
            running = conn.run("docker ps --filter status=running --format '{{.Names}}' | grep -qx nginx").ok

            if not running or conf_changed or compose_changed:
                # conf changes also get down && up: single-file bind-mount inode trap
                ctx.begin(node, "nginx compose up", "first start pulls the image")
                r = conn.run("cd /opt/nginx && docker compose down 2>/dev/null; "
                             "cd /opt/nginx && docker compose up -d", timeout=300)
                ctx.record(node, "nginx up", r.ok, r.err if not r.ok else "")
            else:
                ctx.record(node, "nginx unchanged", True, "")

        # --- verify each node individually BEFORE keepalived (guide 6.B.7) --
        scheme = "https" if self._tls_enabled(ctx) else "http"
        for conn in ctx.fleet:
            node = conn.node.name
            ctx.begin(node, "waiting for per-node /monitor identity")
            ok = wait_for(conn,
                          f"curl -sk {scheme}://{conn.node.ip}/monitor",
                          expect=f'"node":"{node}"', timeout=60, interval=5,
                          tick=lambda el: ctx.tick(f"{int(el)}s / 60s"))
            ctx.record(node, "per-node /monitor answers with own identity", ok,
                       "" if ok else f"{scheme}://{conn.node.ip}/monitor wrong or absent")
            if not ok:
                raise RuntimeError(f"nginx on {node} not serving correctly — "
                                   "stopping before keepalived")

        # --- keepalived ----------------------------------------------------
        prios = self._priorities(ctx)
        auth = self._auth_pass(ctx)
        for conn, prio in zip(ctx.fleet, prios):
            node = conn.node.name
            ctx.begin(node, "installing keepalived", "no-op when present")
            r = conn.run("command -v keepalived >/dev/null && command -v nc >/dev/null || "
                         "DEBIAN_FRONTEND=noninteractive apt-get -y -qq install "
                         "keepalived netcat-openbsd", timeout=600)
            ctx.record(node, "keepalived installed", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")

            kconf = render("keepalived.conf.j2",
                           check_port=self._check_port(ctx),
                           interface=cfg.network.interface,
                           router_id=cfg.network.vrrp_router_id,
                           priority=prio, auth_pass=auth, vip=cfg.network.vip)
            changed = push_file(conn, kconf, "/etc/keepalived/keepalived.conf", mode="0600")
            r = conn.run("systemctl enable --now keepalived && "
                         + ("systemctl reload keepalived" if changed else "true"))
            ctx.record(node, "keepalived running", r.ok, r.err if not r.ok else "")

        # --- VIP convergence ------------------------------------------------
        first = ctx.fleet.conns[0]
        vip_up = wait_for(first,
                          f"curl -sk -o /dev/null -w '%{{http_code}}' "
                          f"{scheme}://{cfg.network.vip}/monitor",
                          expect="200", timeout=60, interval=5)
        ctx.record("cluster", "VIP answers /monitor", vip_up,
                   "" if vip_up else "VIP never converged within 60s")
        if not vip_up:
            raise RuntimeError("VIP did not come up — check journalctl -u keepalived")

        # --- ACME finalization ---------------------------------------------
        if self._acme_pending(ctx):
            self._finalize_acme(ctx)

    # ----------------------------------------------------- acme finalization
    def _finalize_acme(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        a = cfg.tls.acme
        leader = next(c for c in ctx.fleet if c.node.bootstrap_leader)
        others = [c for c in ctx.fleet if not c.node.bootstrap_leader]

        # dedicated distribution keypair on the certbot node
        r = leader.run("test -f /root/.ssh/id_ed25519_certbot || "
                       "ssh-keygen -t ed25519 -N '' -q -f /root/.ssh/id_ed25519_certbot")
        pub = leader.run("cat /root/.ssh/id_ed25519_certbot.pub").out.strip()
        ctx.record(leader.node.name, "distribution keypair", r.ok and bool(pub),
                   r.err if not r.ok else "")
        for conn in others:
            marker_add = (
                "mkdir -p /root/.ssh && touch /root/.ssh/authorized_keys && "
                "sed -i '/# akropolis-certbot/d' /root/.ssh/authorized_keys && "
                f"echo {shlex.quote(pub + ' # akropolis-certbot')} >> /root/.ssh/authorized_keys && "
                "chmod 600 /root/.ssh/authorized_keys")
            r = conn.run(marker_add)
            ctx.record(conn.node.name, "distribution key authorized", r.ok,
                       r.err if not r.ok else "")

        # issuance — the VIP holder serves the challenge; certbot lives on the leader
        cmd = (f"certbot certonly --webroot -w /var/www/certbot "
               f"-d {shlex.quote(cfg.tls.hostname)} "
               f"--email {shlex.quote(a['email'])} --agree-tos --no-eff-email "
               f"--server {shlex.quote(a['directory_url'])} "
               f"--non-interactive --keep-until-expiring"
               + (" --staging" if a.get("staging") else ""))
        ctx.begin(leader.node.name, "certbot issuance", "HTTP-01 via the VIP webroot")
        r = leader.run(cmd, timeout=600)
        issued = r.ok
        ctx.record(leader.node.name,
                   "certbot issuance" + (" [STAGING]" if a.get("staging") else ""),
                   issued, (r.err or r.out).splitlines()[-1] if not issued else "")
        if not issued:
            raise RuntimeError("certbot issuance failed — placeholder cert remains in "
                               "place; fix DNS/reachability and --replay this phase")

        # deploy hook: automatic distribution on every future renewal
        hook = render("certbot-deploy-hook.sh.j2",
                      hostname=cfg.tls.hostname,
                      other_nodes=[c.node for c in others])
        push_file(leader, hook,
                  "/etc/letsencrypt/renewal-hooks/deploy/akropolis-nginx.sh", mode="0755")
        r = leader.run("bash -n /etc/letsencrypt/renewal-hooks/deploy/akropolis-nginx.sh")
        ctx.record(leader.node.name, "deploy hook installed (syntax-checked)", r.ok,
                   r.err if not r.ok else "")

        # run it once now to swap the placeholder everywhere
        r = leader.run("/etc/letsencrypt/renewal-hooks/deploy/akropolis-nginx.sh",
                       timeout=180)
        ctx.record("cluster", "placeholder swapped for issued cert", r.ok,
                   r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("deploy hook failed — issued cert not distributed")

        expiry = leader.run(
            f"openssl x509 -in /opt/nginx/certs/fullchain.pem -noout -enddate "
            f"| cut -d= -f2").out
        ctx.state.data["generated"]["tls_acme_pending"] = False
        ctx.state.data["generated"]["tls_cert_expiry"] = expiry
        ctx.state.save()
        if a.get("staging"):
            ctx.record("cluster", "staging cert in place", True,
                       "browsers will NOT trust it — set tls.acme.staging: false and "
                       "--replay nginx-keepalived for the real one", warn=False)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        cfg = ctx.cfg
        scheme = "https" if self._tls_enabled(ctx) else "http"
        ok = True

        for conn in ctx.fleet:
            node = conn.node.name
            r = conn.run(f"curl -sk {scheme}://{conn.node.ip}/monitor")
            good = f'"node":"{node}"' in r.out
            ctx.record(node, "verify: /monitor identity", good, r.out if not good else "")
            ok = ok and good
            r = conn.run("curl -s http://127.0.0.1:8080/nginx_status | grep -q 'Active connections'")
            ctx.record(node, "verify: stub_status :8080", r.ok, r.err if not r.ok else "")
            ok = ok and r.ok

        # exactly one node must hold the VIP
        holders = []
        for conn in ctx.fleet:
            if conn.run(f"ip -4 addr show {cfg.network.interface} "
                        f"| grep -qw {cfg.network.vip}").ok:
                holders.append(conn.node.name)
        one = len(holders) == 1
        ctx.record("cluster", "exactly one VIP holder", one,
                   f"holders: {holders or 'none'}")
        ok = ok and one

        r = ctx.fleet.conns[0].run(
            f"curl -sk {scheme}://{cfg.network.vip}/monitor")
        vip_ok = '"node"' in r.out
        ctx.record("cluster", "VIP /monitor", vip_ok, r.out if vip_ok else "no answer")
        return ok and vip_ok
