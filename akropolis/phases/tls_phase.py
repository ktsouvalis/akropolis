"""tls — certificate provider abstraction (guide Step 6.B.4, generalized).

The only thing that varies between providers is how fullchain.pem + privkey.pem
come to exist; everything downstream (distribute to /opt/nginx/certs on all
nodes, nginx mounts, reload, expiry tracking) is identical.

Providers:
  none        — testing/local dev only (refused for production at config load).
                Nothing is generated; nginx will be configured for plain :80.
  self_signed — 10-year cert generated on the bootstrap leader with SANs
                covering hostname + node names + VIP + node IPs (guide Option A),
                then distributed to all nodes.
  acme        — STAGED here, finalized after nginx is up: this phase installs
                certbot on the leader, prepares the webroot, and puts a
                self-signed placeholder in place so nginx can start and serve
                the ACME challenge. Issuance + deploy hook land in the
                nginx-keepalived phase.
  import      — externally issued cert (e.g. HARICA portal). Validated on the
                controller (key↔cert match, SAN covers hostname incl. wildcard,
                not expired), distributed, and its expiry pinned in state for
                the monitor handoff — imported certs have no renewal timer, so
                alerting on expiry is the monitor's job.

Certificates land at /opt/nginx/certs/{fullchain,privkey}.pem on every node.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex

from ..remote import push_file
from .base import Phase, PhaseContext

CERT_DIR = "/opt/nginx/certs"
FULLCHAIN = f"{CERT_DIR}/fullchain.pem"
PRIVKEY = f"{CERT_DIR}/privkey.pem"


def _hostname_matches(hostname: str, san_names: list[str]) -> bool:
    for name in san_names:
        if name == hostname:
            return True
        if name.startswith("*.") and "." in hostname:
            if hostname.split(".", 1)[1] == name[2:]:
                return True
    return False


class TLSPhase(Phase):
    name = "tls"

    # ------------------------------------------------------------------ plan
    def plan(self, ctx: PhaseContext) -> list[str]:
        cfg = ctx.cfg
        p = cfg.tls.provider
        if p == "none":
            return ["provider 'none': no certificates — nginx will serve plain HTTP :80 "
                    "(testing only; WebAuthn/passkeys will not work without a secure context)"]
        common = [f"distribute fullchain.pem + privkey.pem to {CERT_DIR} on all 3 nodes "
                  "(privkey mode 0600)",
                  "verify on every node: key matches cert, SAN covers the hostname"]
        if p == "self_signed":
            return [f"generate 10-year self-signed cert on {cfg.bootstrap_leader.name} — "
                    f"CN {cfg.tls.hostname}, SANs: hostname + node names + VIP + node IPs",
                    *common]
        if p == "acme":
            return ["STAGING ONLY (issuance happens after nginx is up):",
                    "generate self-signed placeholder so nginx can start and serve challenges",
                    f"install certbot on {cfg.bootstrap_leader.name}; "
                    "create /var/www/certbot/.well-known/acme-challenge",
                    *common,
                    "mark tls_acme_pending in state — the nginx-keepalived phase finalizes"]
        # import
        return [f"validate {cfg.tls.import_.get('fullchain')} + privkey on this workstation "
                "(key↔cert match, SAN coverage, expiry)",
                *common,
                "pin certificate expiry in state for the monitor handoff (no auto-renewal exists)"]

    # ----------------------------------------------------------------- apply
    def apply(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        p = cfg.tls.provider

        if p == "none":
            ctx.record("cluster", "tls provider none", True, "nothing to do")
            ctx.state.data["generated"]["tls_mode"] = "none"
            ctx.state.save()
            return

        for conn in ctx.fleet:
            conn.run(f"mkdir -p {CERT_DIR}")

        if p in ("self_signed", "acme"):
            self._generate_self_signed(ctx)
            if p == "acme":
                self._stage_acme(ctx)
        elif p == "import":
            self._import_cert(ctx)

        ctx.state.data["generated"]["tls_mode"] = p
        ctx.state.save()

    # ------------------------------------------------------------ providers
    def _generate_self_signed(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        leader = next(c for c in ctx.fleet if c.node.bootstrap_leader)

        # skip when a matching, non-expiring cert is already in place
        r = leader.run(
            f"test -s {FULLCHAIN} && test -s {PRIVKEY} && "
            f"openssl x509 -in {FULLCHAIN} -noout -checkend 2592000 && "
            f"openssl x509 -in {FULLCHAIN} -noout -ext subjectAltName "
            f"| grep -q {shlex.quote(cfg.tls.hostname)}")
        if r.ok:
            ctx.record(leader.node.name, "self-signed cert", True,
                       "existing cert matches hostname and is valid >30d — kept")
        else:
            san = ",".join(
                [f"DNS:{cfg.tls.hostname}"]
                + [f"DNS:{n.name}" for n in cfg.nodes]
                + [f"IP:{cfg.network.vip}"]
                + [f"IP:{n.ip}" for n in cfg.nodes])
            cmd = (f"cd {CERT_DIR} && openssl req -x509 -nodes -days 3650 "
                   f"-newkey rsa:2048 -keyout privkey.pem -out fullchain.pem "
                   f"-subj '/C=GR/O=akropolis/CN={cfg.tls.hostname}' "
                   f"-addext 'subjectAltName={san}' && chmod 600 privkey.pem")
            ctx.begin(leader.node.name, "generating self-signed cert", "10y, full SAN set")
            r = leader.run(cmd, timeout=120)
            ctx.record(leader.node.name, "self-signed cert generated", r.ok,
                       r.err.splitlines()[-1] if (not r.ok and r.err) else "")
            if not r.ok:
                raise RuntimeError("openssl generation failed on the leader")

        # distribute leader's pair to the other nodes
        chain = leader.run(f"cat {FULLCHAIN}").out + "\n"
        key = leader.run(f"cat {PRIVKEY}").out + "\n"
        for conn in ctx.fleet:
            if conn.node.bootstrap_leader:
                continue
            push_file(conn, chain, FULLCHAIN, mode="0644")
            push_file(conn, key, PRIVKEY, mode="0600")
            ctx.record(conn.node.name, "cert distributed", True, "")

    def _stage_acme(self, ctx: PhaseContext) -> None:
        cfg = ctx.cfg
        leader = next(c for c in ctx.fleet if c.node.bootstrap_leader)
        ctx.begin(leader.node.name, "installing certbot", "no-op when present")
        r = leader.run("command -v certbot || "
                       "(DEBIAN_FRONTEND=noninteractive apt-get -y -qq install certbot)",
                       timeout=600)
        ctx.record(leader.node.name, "certbot installed", r.ok,
                   r.err.splitlines()[-1] if (not r.ok and r.err) else "")
        r = leader.run("mkdir -p /var/www/certbot/.well-known/acme-challenge")
        ctx.record(leader.node.name, "ACME webroot prepared", r.ok, r.err if not r.ok else "")
        ctx.state.data["generated"]["tls_acme_pending"] = True
        ctx.state.save()
        ctx.record("cluster", "acme issuance deferred", True,
                   "placeholder cert in place; finalized after nginx is up")

    def _import_cert(self, ctx: PhaseContext) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cfg = ctx.cfg
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
        covered = _hostname_matches(cfg.tls.hostname, san)
        ctx.record("controller", f"SAN covers {cfg.tls.hostname}", covered,
                   f"SANs: {san}")
        if not covered:
            raise RuntimeError("certificate SAN does not cover the configured hostname")

        expiry = cert.not_valid_after_utc
        days_left = (expiry - dt.datetime.now(dt.timezone.utc)).days
        ctx.record("controller", "certificate validity", days_left > 0,
                   f"expires {expiry.date()} ({days_left} days)",
                   warn=(0 < days_left <= 30))
        if days_left <= 0:
            raise RuntimeError("certificate is already expired")

        for conn in ctx.fleet:
            push_file(conn, chain_bytes.decode(), FULLCHAIN, mode="0644")
            push_file(conn, key_bytes.decode(), PRIVKEY, mode="0600")
            ctx.record(conn.node.name, "cert distributed", True, "")

        ctx.state.data["generated"]["tls_cert_expiry"] = expiry.date().isoformat()
        ctx.state.save()

    # ---------------------------------------------------------------- verify
    def verify(self, ctx: PhaseContext) -> bool:
        cfg = ctx.cfg
        if cfg.tls.provider == "none":
            return True
        ok = True
        for conn in ctx.fleet:
            node = conn.node.name
            r = conn.run(
                f"test -s {FULLCHAIN} && test -s {PRIVKEY} && "
                f"test \"$(openssl x509 -in {FULLCHAIN} -noout -pubkey)\" = "
                f"\"$(openssl pkey -in {PRIVKEY} -pubout)\"")
            ctx.record(node, "verify: cert+key present and matching", r.ok,
                       r.err if not r.ok else "")
            ok = ok and r.ok
        return ok
