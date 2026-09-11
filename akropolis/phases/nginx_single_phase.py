"""nginx (single) — bare-metal reverse proxy in front of the one node's
Authentik containers: terminates public TLS and serves a maintenance page
whenever Authentik is unreachable (akropolis shutdown, or an actual outage).

Bare-metal (systemd), not a container — deliberately, for the same reason
keepalived is bare-metal on the HA topology (see nginx_keepalived_phase.py):
the thing that has to keep answering when Docker (or Authentik) is down or
being maintained must not share Docker's own failure modes. Authentik's own
container is unchanged — still listening on its image defaults (9443 https,
self-signed; 9000 http), now published to loopback only instead of host 443
(see authentik-single-compose.yml.j2) — this phase's nginx is the only thing
the public ever reaches.

TLS material lives as plain files on the host (/etc/nginx/akropolis-certs) —
no volume/bind-mount semantics to manage, there is no container.

Condenses the HA topology's tls_phase.py + nginx_keepalived_phase.py for one
node: no VRRP/keepalived, no multi-node distribution keypair, no per-node
/monitor identity, no `down && up -d` inode-trap dance (a bare-metal reload
has no bind-mount, so `nginx -t && systemctl reload nginx` is always safe).

Providers:
  none        — plain HTTP only (testing/lab; refused for production at
                config load). nginx proxies straight to Authentik's own HTTP
                listener (9000); no certificate handling at all.
  self_signed — a 10-year self-signed cert generated locally, CN/SAN =
                tls.hostname or (if blank) this node's IP.
  acme        — the self-signed cert above is generated first as a
                placeholder so nginx's :443 block has files to start with;
                once nginx is serving the ACME challenge path on :80,
                certbot --webroot obtains the real certificate and a deploy
                hook (copy + `systemctl reload nginx`) keeps it current.
  import      — externally issued cert, validated on the controller
                (key<->cert match, SAN coverage, expiry) and placed directly
                — no placeholder needed.

Either way nginx proxies to Authentik's own HTTPS listener (9443, loopback-
only, self-signed — `proxy_ssl_verify off`), the same "trust the loopback
leg, verify the public leg" split the HA topology's nginx already uses
against its 3 backends.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import shlex
from importlib import resources

from ..remote import push_file, render
from .base import Phase, PhaseContext

CERT_DIR = "/etc/nginx/akropolis-certs"
FULLCHAIN = f"{CERT_DIR}/fullchain.pem"
PRIVKEY = f"{CERT_DIR}/privkey.pem"
WEBROOT = "/var/www/akropolis-certbot"
MAINT_ROOT = "/var/www/akropolis-maintenance"
SITE_CONF = "/etc/nginx/sites-available/akropolis.conf"
SITE_LINK = "/etc/nginx/sites-enabled/akropolis.conf"
DEPLOY_HOOK = "/etc/letsencrypt/renewal-hooks/deploy/akropolis-nginx-single.sh"


class NginxSinglePhase(Phase):
    name = "nginx"

    # ------------------------------------------------------------------ util
    def _tls_enabled(self, ctx: PhaseContext) -> bool:
        return ctx.cfg.tls.provider != "none"

    def _cn(self, ctx: PhaseContext) -> str:
        return ctx.cfg.tls.hostname or ctx.cfg.nodes[0].ip

    def _stub_allow(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        allow = list((cfg.raw.get("network") or {}).get("stub_status_allow", []) or [])
        mon = str(((cfg.raw.get("monitor") or {}).get("ip") or "")).strip() \
            or ctx.state.data["generated"].get("monitor_ip", "")
        if mon and mon not in allow:
            allow.append(mon)
        return allow

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        p = ctx.cfg.tls.provider
        lines = ["install nginx (apt, no-op if present); push maintenance.html "
                "to a plain webroot (served whenever Authentik is unreachable — "
                "502/503/504 — with the real status code preserved so "
                "monitoring still sees the outage)"]
        if p == "none":
            lines.append("provider 'none': plain HTTP :80 only, proxied straight "
                         "to Authentik's own HTTP listener (9000, loopback-only) "
                         "— testing only")
        else:
            if p == "self_signed":
                lines.append(f"generate a 10-year self-signed cert locally — "
                             f"CN/SAN {self._cn(ctx)}")
            elif p == "acme":
                acme = ctx.cfg.tls.acme or {}
                lines.append("generate a self-signed placeholder so nginx can "
                             "start; once it's serving :80, certbot --webroot "
                             f"against {acme.get('directory_url', '?')}"
                             + (" [yellow]--staging[/yellow]" if acme.get("staging") else "")
                             + " gets the real certificate; deploy hook re-copies "
                               "and reloads nginx on every future renewal")
                lines.append("reissue is forced automatically when the certificate "
                             "already on the node was issued by the other "
                             "environment (staging vs production) — certbot would "
                             "otherwise decline it as not due for renewal and leave "
                             "the wrong certificate in place")
            else:  # import
                lines.append(f"validate {ctx.cfg.tls.import_.get('fullchain')} + "
                             "privkey on this workstation (key<->cert match, SAN "
                             "coverage, expiry), place directly — no placeholder needed")
            lines.append("render nginx.conf: :80 -> :443 redirect (ACME exception), "
                         "TLS :443 proxy -> Authentik's own HTTPS listener (9443, "
                         "loopback-only, proxy_ssl_verify off — same trust split "
                         "the HA topology's nginx already uses)")
        lines.append("stub_status on :8080 for the monitor (loopback + monitor.ip)")
        lines.append("nginx -t, then enable/reload (bare metal — no bind-mount, "
                     "reload is always safe, unlike the HA container's inode trap)")
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        p = cfg.tls.provider
        conn = ctx.fleet.conns[0]
        node = conn.node.name

        conn.run(f"mkdir -p {CERT_DIR} {WEBROOT}/.well-known/acme-challenge {MAINT_ROOT}")
        maintenance = resources.files("akropolis.templates").joinpath("maintenance.html").read_text()
        push_file(conn, maintenance, f"{MAINT_ROOT}/maintenance.html")

        if p in ("self_signed", "acme"):
            self._generate_self_signed(ctx, conn)
        elif p == "import":
            self._import_cert(ctx, conn)

        ctx.begin(node, "installing nginx", "no-op when present")
        r = conn.run("command -v nginx >/dev/null || "
                     "(DEBIAN_FRONTEND=noninteractive apt-get -y -qq install nginx)",
                     timeout=600)
        ctx.record(node, "nginx installed", r.ok,
                   r.err.splitlines()[-1] if (not r.ok and r.err) else "")

        conf = render("nginx-single.conf.j2",
                      hostname=cfg.tls.hostname or "_",
                      tls_enabled=self._tls_enabled(ctx),
                      stub_status_allow=self._stub_allow(ctx))
        conf_changed = push_file(conn, conf, SITE_CONF)
        r = conn.run(f"ln -sf {SITE_CONF} {SITE_LINK} && "
                     "rm -f /etc/nginx/sites-enabled/default")
        ctx.record(node, "site enabled, default disabled", r.ok, r.err if not r.ok else "")

        r = conn.run("nginx -t")
        ctx.record(node, "nginx config valid", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("nginx -t failed — see output above")

        running = conn.run("systemctl is-active --quiet nginx").ok
        if not running or conf_changed:
            r = conn.run("systemctl enable --now nginx && systemctl reload nginx")
            ctx.record(node, "nginx running", r.ok, r.err if not r.ok else "")
        else:
            ctx.record(node, "nginx unchanged", True, "")

        if p == "acme":
            self._finalize_acme(ctx, conn)

    # ------------------------------------------------------------ providers
    def _generate_self_signed(self, ctx: PhaseContext, conn) -> None:
        node = conn.node.name
        cn = self._cn(ctx)

        # skip when a matching, non-expiring cert is already in place
        r = conn.run(
            f"test -s {FULLCHAIN} && test -s {PRIVKEY} && "
            f"openssl x509 -in {FULLCHAIN} -noout -checkend 2592000 && "
            f"openssl x509 -in {FULLCHAIN} -noout -ext subjectAltName "
            f"| grep -q {shlex.quote(cn)}")
        if r.ok:
            ctx.record(node, "self-signed cert", True,
                       "existing cert matches and is valid >30d — kept")
            return

        try:
            ipaddress.ip_address(cn)
            sans = [f"IP:{cn}"]
        except ValueError:
            sans = [f"DNS:{cn}"]
        if f"IP:{conn.node.ip}" not in sans:
            sans.append(f"IP:{conn.node.ip}")
        san = ",".join(sans)
        cmd = (f"cd {CERT_DIR} && openssl req -x509 -nodes -days 3650 "
              f"-newkey rsa:2048 -keyout privkey.pem -out fullchain.pem "
              f"-subj '/C=GR/O=akropolis/CN={cn}' "
              f"-addext 'subjectAltName={san}' && chmod 600 privkey.pem")
        ctx.begin(node, "generating self-signed cert", "10y")
        r = conn.run(cmd, timeout=120)
        ctx.record(node, "self-signed cert generated", r.ok,
                   r.err.splitlines()[-1] if (not r.ok and r.err) else "")
        if not r.ok:
            raise RuntimeError("openssl generation failed")

    def _import_cert(self, ctx: PhaseContext, conn) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cfg = ctx.cfg
        node = conn.node.name
        chain_path = os.path.expanduser(cfg.tls.import_["fullchain"])
        key_path = os.path.expanduser(cfg.tls.import_["privkey"])
        chain_bytes = open(chain_path, "rb").read()
        key_bytes = open(key_path, "rb").read()

        cert = x509.load_pem_x509_certificate(chain_bytes)
        key = serialization.load_pem_private_key(key_bytes, password=None)
        spki = lambda k: k.public_bytes(  # noqa: E731
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        match = spki(cert.public_key()) == spki(key.public_key())
        ctx.record("controller", "private key matches certificate", match,
                   "" if match else "SubjectPublicKeyInfo mismatch")
        if not match:
            raise RuntimeError("privkey does not match fullchain — wrong file pair?")

        try:
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            san = []
        covered = cfg.tls.hostname in san or any(
            h.startswith("*.") and cfg.tls.hostname.endswith(h[1:]) for h in san)
        ctx.record("controller", f"SAN covers {cfg.tls.hostname}", covered, f"SANs: {san}")
        if not covered:
            raise RuntimeError("certificate SAN does not cover the configured hostname")

        expiry = cert.not_valid_after_utc
        days_left = (expiry - dt.datetime.now(dt.timezone.utc)).days
        ctx.record("controller", "certificate validity", days_left > 0,
                   f"expires {expiry.date()} ({days_left} days)",
                   warn=(0 < days_left <= 30))
        if days_left <= 0:
            raise RuntimeError("certificate is already expired")

        push_file(conn, chain_bytes.decode(), FULLCHAIN, mode="0644")
        push_file(conn, key_bytes.decode(), PRIVKEY, mode="0600")
        ctx.record(node, "cert pushed", True, CERT_DIR)
        ctx.state.data["generated"]["tls_cert_expiry"] = expiry.date().isoformat()
        ctx.state.save()

    # ----------------------------------------------------- acme finalization
    def _finalize_acme(self, ctx: PhaseContext, conn) -> None:
        cfg = ctx.cfg
        node = conn.node.name
        a = cfg.tls.acme or {}
        r = conn.run("command -v certbot >/dev/null || "
                     "(DEBIAN_FRONTEND=noninteractive apt-get -y -qq install certbot)",
                     timeout=600)
        ctx.record(node, "certbot installed", r.ok,
                   r.err.splitlines()[-1] if (not r.ok and r.err) else "")

        # See tls_phase.py / nginx_keepalived_phase.py: certbot refuses to
        # reissue a lineage that isn't within 30 days of expiry regardless of
        # which CA issued it, so a staging<->production flip needs a forced
        # reissue or the untrusted/wrong certificate is silently kept.
        staging = bool(a.get("staging"))
        live = f"/etc/letsencrypt/live/{cfg.tls.hostname}"
        force = bool(a.get("force_renewal"))
        reason = "acme.force_renewal is set in the site config" if force else ""
        r = conn.run(f"test -f {shlex.quote(live)}/fullchain.pem && "
                     f"openssl x509 -noout -issuer -in {shlex.quote(live)}/fullchain.pem")
        if r.ok and r.out:
            existing_staging = "STAGING" in r.out.upper()
            if existing_staging != staging:
                force = True
                reason = (f"existing certificate is {'staging' if existing_staging else 'production'}, "
                          f"this run asks for {'staging' if staging else 'production'}")
            ctx.record(node, "existing certbot lineage", True,
                       f"{'staging' if existing_staging else 'production'} issuer"
                       + (f" — forcing reissue ({reason})" if force else " — matches this run"),
                       warn=force)
        if force and reason:
            ctx.record(node, "forcing certificate reissue", True, reason, warn=True)

        cmd = (f"certbot certonly --webroot -w {shlex.quote(WEBROOT)} "
               f"-d {shlex.quote(cfg.tls.hostname)} "
               f"--email {shlex.quote(a.get('email', ''))} --agree-tos --no-eff-email "
               f"--server {shlex.quote(a.get('directory_url', ''))} "
               f"--non-interactive"
               + (" --staging" if staging else "")
               + (" --force-renewal" if force else " --keep-until-expiring"))
        ctx.begin(node, "certbot issuance", "HTTP-01 via nginx's own webroot")
        r = conn.run(cmd, timeout=180)
        ctx.record(node, "certbot issuance" + (" [STAGING]" if staging else ""), r.ok,
                   (r.err or r.out).splitlines()[-1] if not r.ok else "")
        if not r.ok:
            raise RuntimeError("certbot failed — placeholder cert remains in place; "
                               "fix DNS/reachability and --replay this phase")

        hook = ("#!/bin/sh\n"
               f"cp \"$RENEWED_LINEAGE/fullchain.pem\" \"$RENEWED_LINEAGE/privkey.pem\" "
               f"{shlex.quote(CERT_DIR)}/\n"
               f"chmod 600 {shlex.quote(PRIVKEY)}\n"
               "systemctl reload nginx\n")
        push_file(conn, hook, DEPLOY_HOOK, mode="0755")
        ctx.record(node, "renewal deploy hook installed", True, DEPLOY_HOOK)

        r = conn.run(f"bash {shlex.quote(DEPLOY_HOOK)}", timeout=60)
        ctx.record(node, "placeholder swapped for issued cert", r.ok, r.err if not r.ok else "")
        if not r.ok:
            raise RuntimeError("deploy hook failed — issued cert not installed")

        expiry = conn.run(f"openssl x509 -in {FULLCHAIN} -noout -enddate | cut -d= -f2").out
        ctx.state.data["generated"]["tls_cert_expiry"] = expiry
        ctx.state.save()
        if staging:
            ctx.record(node, "staging cert in place", True,
                       "browsers will NOT trust it — set tls.acme.staging: false and "
                       "--replay nginx for the real one", warn=False)

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        conn = ctx.fleet.conns[0]
        node = conn.node.name
        ok = True

        r = conn.run("systemctl is-active --quiet nginx")
        ctx.record(node, "verify: nginx active", r.ok, "")
        ok = ok and r.ok

        if self._tls_enabled(ctx):
            r = conn.run(f"test -s {FULLCHAIN} && test -s {PRIVKEY} && "
                         f"test \"$(openssl x509 -in {FULLCHAIN} -noout -pubkey)\" = "
                         f"\"$(openssl pkey -in {PRIVKEY} -pubout)\"")
            ctx.record(node, "verify: cert+key present and matching", r.ok,
                       r.err if not r.ok else "")
            ok = ok and r.ok
            scheme = "https"
        else:
            scheme = "http"

        r = conn.run(f"curl -sk -o /dev/null -w '%{{http_code}}' "
                     f"{scheme}://127.0.0.1/-/health/ready/")
        ready = r.out in ("200", "204")
        ctx.record(node, "verify: end-to-end through nginx", ready, f"HTTP {r.out}")
        ok = ok and ready

        r = conn.run("curl -s http://127.0.0.1:8080/nginx_status | grep -q 'Active connections'")
        ctx.record(node, "verify: stub_status :8080", r.ok, r.err if not r.ok else "")
        ok = ok and r.ok
        return ok
