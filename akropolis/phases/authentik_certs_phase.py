"""certs (single) — point authentik's own core webserver at a real TLS
certificate. topology: single only; there is no nginx phase to terminate TLS
(see NOTES.md) — the target deployment is reached by NAT with no port
translation, so authentik has to serve HTTPS directly on the port the public
actually hits. authentik's built-in mechanisms do the rest: certificate
*discovery* (a certs directory mounted at /certs on the worker container,
scanned for keypairs) and each brand's *Web Certificate* field (which
keypair the core webserver — port 443 here — presents). Same PATCH mechanism
already used for the branding logo/favicon (see authentik_phase.py's
apply_brand / patch_brand), just a different field.

self_signed / none: no-op. authentik already generates and serves its own
self-signed certificate on first boot; there is nothing for akropolis to add.

acme: certbot in --standalone mode — nothing else listens on :80 (see
authentik-single-env.j2) — obtains the cert; a renewal deploy hook re-copies
it into authentik's discovery folder and restarts the worker on every future
renewal, same as the HA cluster's deploy hook does for nginx.

import: the same validation tls_phase.py runs for the HA cluster (key<->cert
match, SAN coverage, expiry) for one file pair, one node, one destination.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
import time

from ..remote import push_file
from .authentik_phase import patch_brand
from .base import Phase, PhaseContext

CERT_PORT = 443  # authentik's own HTTPS listener, single-node topology


def set_web_certificate(ctx: PhaseContext, conn, token: str, hostname: str,
                        cert_dir: str = "") -> bool:
    """Point the default brand's `web_certificate` at the discovered keypair.

    Module-level rather than a method because the `restore` phase has to run
    it again: on single-node topology authentik's OWN webserver terminates
    TLS, and which certificate it presents is a column on the brand row — so
    `DROP DATABASE` takes that setting with it and the restored dump brings
    the old instance's brand back in its place, pointing at a keypair that
    does not exist here. The node then silently falls back to authentik's
    self-signed certificate, which is exactly the sort of failure that shows
    up as "the UI is broken" rather than as anything cert-shaped.
    """
    node = conn.node.name
    r = conn.run(
        f"curl -sk -H {shlex.quote('Authorization: Bearer ' + token)} "
        f"{shlex.quote(f'https://127.0.0.1:{CERT_PORT}/api/v3/crypto/certificatekeypairs/?page_size=200')} "
        f"| jq -r --arg n {shlex.quote(hostname)} '.results[] | select(.name==$n) | .pk' | head -1",
        timeout=60)
    cert_uuid = r.out.strip()
    if not r.ok or not cert_uuid:
        ctx.record(node, "certificate discovered", False,
                   f"no keypair named {hostname!r} found"
                   + (f" — check {cert_dir} and " if cert_dir else " — check ")
                   + "'docker compose logs worker' on the node", warn=True)
        return False
    ctx.record(node, "certificate discovered", True, f"keypair {cert_uuid}")

    code = patch_brand(ctx, conn, token, {"web_certificate": cert_uuid}, port=CERT_PORT)
    ok = code == "200"
    ctx.record(node, "web certificate set on default brand", ok,
               "" if ok else f"HTTP {code} — set it manually in System > Brands",
               warn=not ok)
    return ok


class AuthentikCertsPhase(Phase):
    name = "certs"

    def _cert_dir(self, ctx: PhaseContext) -> str:
        # certbot-convention discovery: a folder named after the domain,
        # containing fullchain.pem + privkey.pem, imports as a keypair named
        # after the folder — see docs.goauthentik.io/sys-mgmt/certificates.
        return f"/opt/authentik/certs/{ctx.cfg.tls.hostname}"

    def _token(self, ctx: PhaseContext) -> str:
        return ctx.state.data["generated"].get("authentik_bootstrap_token", "")

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        p = ctx.cfg.tls.provider
        if p in ("none", "self_signed"):
            return [f"provider {p!r}: no-op — authentik already serves its own "
                    "auto-generated self-signed certificate, nothing to install"]
        lines = []
        if p == "acme":
            acme = ctx.cfg.tls.acme or {}
            lines.append(f"certbot --standalone against "
                        f"{acme.get('directory_url', '?')}"
                        + (" [yellow]--staging (certificate will NOT be browser-trusted)"
                           "[/yellow]" if acme.get("staging") else "")
                        + " (binds :80 briefly — free by construction); deploy hook "
                          "re-copies and restarts the worker on every future renewal")
            lines.append("reissue is forced automatically when the certificate already "
                        "on the node was issued by the other environment (staging vs "
                        "production) — certbot would otherwise decline it as not due "
                        "for renewal and leave the wrong certificate in place")
        else:  # import
            lines.append(f"validate {ctx.cfg.tls.import_.get('fullchain')} + privkey "
                        "on this workstation (key↔cert match, SAN coverage, expiry)")
        lines += [
            f"place fullchain.pem + privkey.pem under {self._cert_dir(ctx)}/ "
            "(authentik's certbot-convention discovery folder)",
            "restart the worker container so certificate_discovery runs now, "
            "not on its own schedule",
            f"PATCH the default brand's web_certificate to the discovered "
            f"keypair (authentik's own core webserver, port {CERT_PORT})",
        ]
        return lines

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        p = cfg.tls.provider
        conn = ctx.fleet.conns[0]
        node = conn.node.name

        if p in ("none", "self_signed"):
            ctx.record(node, "certs", True,
                      "no-op — using authentik's own default certificate")
            return

        cert_dir = self._cert_dir(ctx)
        conn.run(f"mkdir -p {shlex.quote(cert_dir)}")

        if p == "acme":
            self._acme(ctx, conn, cert_dir)
        else:
            self._import_cert(ctx, conn, cert_dir)

        ctx.begin(node, "restarting worker", "so certificate_discovery runs now")
        r = conn.run("cd /opt/authentik && docker compose restart worker", timeout=120)
        ctx.record(node, "worker restarted", r.ok, r.err if not r.ok else "")
        if r.ok:
            time.sleep(15)  # discovery runs on worker startup; give it a moment

        set_web_certificate(ctx, conn, self._token(ctx), cfg.tls.hostname, cert_dir)

    # ------------------------------------------------------------ providers
    def _acme(self, ctx: PhaseContext, conn, cert_dir: str) -> None:
        cfg = ctx.cfg
        node = conn.node.name
        r = conn.run("command -v certbot || "
                     "(DEBIAN_FRONTEND=noninteractive apt-get -y -qq install certbot)",
                     timeout=600)
        ctx.record(node, "certbot installed", r.ok, r.err if not r.ok else "")

        acme = cfg.tls.acme or {}
        staging = bool(acme.get("staging"))
        staging_flag = " --staging" if staging else ""
        live = f"/etc/letsencrypt/live/{cfg.tls.hostname}"

        # Why --force-renewal is not just a config toggle: certbot refuses to
        # reissue a lineage that is not within 30 days of expiry, and it makes
        # that decision on validity ALONE — it does not care that the cert on
        # disk was issued by the staging CA and the run now asks for the real
        # one. Rehearse with staging, flip the flag, re-run, and certbot says
        # "not due for renewal" and leaves the untrusted certificate in place;
        # the only way out was to run certbot by hand. So: read the issuer of
        # whatever is already there, and force the reissue when it disagrees
        # with what this run is asking for.
        force = bool(acme.get("force_renewal"))
        reason = "acme.force_renewal is set in the site config" if force else ""
        r = conn.run(f"test -f {shlex.quote(live)}/fullchain.pem && "
                     f"openssl x509 -noout -issuer -in {shlex.quote(live)}/fullchain.pem")
        if r.ok and r.out:
            # Let's Encrypt's staging intermediates carry "(STAGING)" in the
            # issuer CN — e.g. "(STAGING) Wannabe Watercress R11".
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

        ctx.begin(node, "certbot --standalone", "binds :80 briefly")
        cmd = (f"certbot certonly --standalone --non-interactive --agree-tos "
              f"-m {shlex.quote(acme.get('email', ''))} "
              f"-d {shlex.quote(cfg.tls.hostname)} "
              f"--server {shlex.quote(acme.get('directory_url', ''))}{staging_flag} "
              f"--cert-name {shlex.quote(cfg.tls.hostname)}"
              + (" --force-renewal" if force else ""))
        r = conn.run(cmd, timeout=180)
        ctx.record(node, "certbot issuance", r.ok,
                   r.err.splitlines()[-1] if (not r.ok and r.err) else "")
        if not r.ok:
            raise RuntimeError("certbot failed — see output above")

        r = conn.run(f"cp {live}/fullchain.pem {live}/privkey.pem {shlex.quote(cert_dir)}/ && "
                     f"chmod 600 {shlex.quote(cert_dir)}/privkey.pem")
        ctx.record(node, "cert copied to authentik discovery folder", r.ok,
                   r.err if not r.ok else "")

        # deploy hook: re-copy + restart worker on every future renewal — the
        # same job the HA cluster's certbot deploy hook does for nginx.
        hook_path = "/etc/letsencrypt/renewal-hooks/deploy/akropolis-authentik.sh"
        hook = ("#!/bin/sh\n"
               f"cp \"$RENEWED_LINEAGE/fullchain.pem\" \"$RENEWED_LINEAGE/privkey.pem\" "
               f"{shlex.quote(cert_dir)}/\n"
               f"chmod 600 {shlex.quote(cert_dir)}/privkey.pem\n"
               "cd /opt/authentik && docker compose restart worker\n")
        push_file(conn, hook, hook_path, mode="0755")
        ctx.record(node, "renewal deploy hook installed", True, hook_path)

    def _import_cert(self, ctx: PhaseContext, conn, cert_dir: str) -> None:
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

        push_file(conn, chain_bytes.decode(), f"{cert_dir}/fullchain.pem", mode="0644")
        push_file(conn, key_bytes.decode(), f"{cert_dir}/privkey.pem", mode="0600")
        ctx.record(node, "cert pushed to authentik discovery folder", True, cert_dir)
        ctx.state.data["generated"]["tls_cert_expiry"] = expiry.date().isoformat()
        ctx.state.save()

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        if ctx.cfg.tls.provider in ("none", "self_signed"):
            return True
        conn = ctx.fleet.conns[0]
        node = conn.node.name
        r = conn.run(f"curl -sk -o /dev/null -w '%{{http_code}}' "
                     f"https://127.0.0.1:{CERT_PORT}/-/health/ready/")
        ok = r.out in ("200", "204")
        ctx.record(node, "verify: authentik still healthy after cert restart", ok,
                   f"HTTP {r.out}")
        return ok
