"""Tests for akropolis.remote: template rendering, checksummed file push,
poll-until-healthy waits, and base_url().

All SSH interaction is faked via FakeConn -- no real paramiko/network calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import jinja2
import pytest

from akropolis import remote
from akropolis.sshexec import Result


class FakeConn:
    """Duck-typed stand-in for NodeConn: queued .run() responses, recorded
    .put() calls."""

    def __init__(self, node_name="node1", responses=None):
        self.node = SimpleNamespace(name=node_name)
        self.run_calls = []
        self.put_calls = []
        self._responses = list(responses or [])
        self._put_exception = None

    def run(self, cmd, timeout=30.0):
        self.run_calls.append(cmd)
        if self._responses:
            return self._responses.pop(0)
        return Result(rc=0, out="", err="")

    def put(self, local_path, remote_path, callback=None):
        if self._put_exception:
            raise self._put_exception
        self.put_calls.append((local_path, remote_path))


# --- render() -------------------------------------------------------------

def test_render_static_template_needs_no_context():
    out = remote.render("maintenance.html.j2")
    assert "<html" in out
    assert "<img" not in out


def test_render_maintenance_template_with_logo():
    out = remote.render("maintenance.html.j2", logo_name="maintenance-logo.png")
    assert '<img class="logo" src="/maintenance-logo.png"' in out


def test_render_substitutes_context():
    out = remote.render(
        "keepalived.conf.j2",
        auth_pass="s3cr3t", check_port=9000, interface="ens18",
        priority=100, router_id=51, vip="10.0.0.10",
    )
    assert "s3cr3t" in out
    assert "10.0.0.10" in out


def test_render_raises_on_missing_context():
    with pytest.raises(jinja2.exceptions.UndefinedError):
        remote.render("keepalived.conf.j2")  # no context at all -- StrictUndefined must refuse


# --- push_file() ------------------------------------------------------------

def _digest(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.encode()).hexdigest()


def test_push_file_noop_when_content_and_mode_match():
    content = "hello world\n"
    conn = FakeConn(responses=[
        Result(0, _digest(content), ""),  # sha256sum
        Result(0, "644", ""),             # stat -> mode matches "0644"
    ])
    changed = remote.push_file(conn, content, "/etc/thing.conf", mode="0644")
    assert changed is False
    assert len(conn.run_calls) == 2  # no chmod/chown issued


def test_push_file_fixes_mode_drift_without_rewriting_content():
    content = "hello world\n"
    conn = FakeConn(responses=[
        Result(0, _digest(content), ""),  # sha256sum matches
        Result(0, "600", ""),             # stat -> drifted from 0644
        Result(0, "", ""),                # chmod succeeds
    ])
    changed = remote.push_file(conn, content, "/etc/thing.conf", mode="0644")
    assert changed is False
    assert any("chmod 0644" in c for c in conn.run_calls)
    assert not any("base64" in c for c in conn.run_calls)  # content untouched


def test_push_file_fixes_owner_drift_too():
    content = "hello\n"
    conn = FakeConn(responses=[
        Result(0, _digest(content), ""),
        Result(0, "600", ""),
        Result(0, "", ""),
    ])
    remote.push_file(conn, content, "/etc/thing.conf", mode="0644", owner="authentik:authentik")
    fix_cmd = conn.run_calls[-1]
    assert "chmod 0644" in fix_cmd and "chown authentik:authentik" in fix_cmd


def test_push_file_raises_when_perm_fix_fails():
    content = "hello\n"
    conn = FakeConn(responses=[
        Result(0, _digest(content), ""),
        Result(0, "600", ""),
        Result(1, "", "permission denied"),
    ])
    with pytest.raises(RuntimeError, match="failed to fix perms"):
        remote.push_file(conn, content, "/etc/thing.conf", mode="0644")


def test_push_file_writes_when_content_changed():
    conn = FakeConn(responses=[
        Result(0, "deadbeef", ""),  # sha256sum: doesn't match
        Result(0, "", ""),          # write succeeds
    ])
    changed = remote.push_file(conn, "new content\n", "/etc/thing.conf", mode="0644")
    assert changed is True
    write_cmd = conn.run_calls[-1]
    assert "mkdir -p" in write_cmd
    assert "base64 -d" in write_cmd
    assert "chmod 0644" in write_cmd


def test_push_file_write_includes_owner():
    conn = FakeConn(responses=[Result(1, "", ""), Result(0, "", "")])
    remote.push_file(conn, "content\n", "/etc/thing.conf", owner="root:root")
    assert "chown root:root" in conn.run_calls[-1]


def test_push_file_raises_when_write_fails():
    conn = FakeConn(responses=[Result(1, "", ""), Result(1, "", "disk full")])
    with pytest.raises(RuntimeError, match="failed to write"):
        remote.push_file(conn, "content\n", "/etc/thing.conf")


# --- push_binary() ---------------------------------------------------------

def test_push_binary_noop_when_digest_matches(tmp_path):
    local = tmp_path / "cert.pem"
    local.write_bytes(b"certificate bytes")
    import hashlib
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    conn = FakeConn(responses=[Result(0, digest, "")])
    changed = remote.push_binary(conn, str(local), "/etc/certs/cert.pem")
    assert changed is False
    assert conn.put_calls == []  # unchanged -- must not touch the wire


def test_push_binary_uploads_when_digest_differs(tmp_path):
    local = tmp_path / "cert.pem"
    local.write_bytes(b"certificate bytes")
    import hashlib
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    conn = FakeConn(responses=[
        Result(0, "stale-digest", ""),  # initial sha256sum: mismatch
        Result(0, "", ""),              # mkdir/mv/chmod
        Result(0, digest, ""),          # post-move checksum verify
    ])
    changed = remote.push_binary(conn, str(local), "/etc/certs/cert.pem")
    assert changed is True
    assert len(conn.put_calls) == 1
    local_arg, staging_arg = conn.put_calls[0]
    assert local_arg == str(local)
    assert staging_arg.startswith("/tmp/.akropolis-upload-")


def test_push_binary_wraps_sftp_oserror(tmp_path):
    local = tmp_path / "cert.pem"
    local.write_bytes(b"bytes")
    conn = FakeConn(responses=[Result(0, "stale", "")])
    conn._put_exception = OSError(13, "Permission denied")
    with pytest.raises(RuntimeError, match="SFTP upload"):
        remote.push_binary(conn, str(local), "/etc/certs/cert.pem")


def test_push_binary_cleans_up_staging_on_move_failure(tmp_path):
    local = tmp_path / "cert.pem"
    local.write_bytes(b"bytes")
    conn = FakeConn(responses=[
        Result(0, "stale", ""),
        Result(1, "", "mv: permission denied"),  # mv/chmod fails
    ])
    with pytest.raises(RuntimeError, match="failed to install"):
        remote.push_binary(conn, str(local), "/etc/certs/cert.pem")
    assert any(c.startswith("rm -f") for c in conn.run_calls)


def test_push_binary_detects_post_upload_corruption(tmp_path):
    local = tmp_path / "cert.pem"
    local.write_bytes(b"bytes")
    conn = FakeConn(responses=[
        Result(0, "stale", ""),
        Result(0, "", ""),                 # mv/chmod ok
        Result(0, "corrupted-checksum", ""),  # verify: mismatch
    ])
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        remote.push_binary(conn, str(local), "/etc/certs/cert.pem")


# --- wait_for() -------------------------------------------------------------

def test_wait_for_returns_true_on_immediate_success():
    conn = FakeConn(responses=[Result(0, "ready", "")])
    assert remote.wait_for(conn, "check", expect="ready", timeout=1.0, interval=0.01) is True
    assert len(conn.run_calls) == 1


def test_wait_for_retries_until_expect_matches():
    conn = FakeConn(responses=[
        Result(0, "starting", ""),
        Result(0, "starting", ""),
        Result(0, "ready", ""),
    ])
    assert remote.wait_for(conn, "check", expect="ready", timeout=2.0, interval=0.01) is True
    assert len(conn.run_calls) == 3


def test_wait_for_returns_false_on_timeout():
    conn = FakeConn(responses=[])  # every call returns the FakeConn default: rc=0, out=""
    assert remote.wait_for(conn, "check", expect="never-appears", timeout=0.05, interval=0.02) is False


def test_wait_for_calls_tick_with_elapsed_seconds():
    conn = FakeConn(responses=[Result(0, "ready", "")])
    ticks = []
    remote.wait_for(conn, "check", expect="ready", timeout=1.0, interval=0.01, tick=ticks.append)
    assert ticks and ticks[0] >= 0


# --- base_url() --------------------------------------------------------

def _cfg(topology, tls_provider, tls_hostname="", vip="10.0.0.10", node_ip="10.0.0.1"):
    return SimpleNamespace(
        topology=topology,
        tls=SimpleNamespace(provider=tls_provider, hostname=tls_hostname),
        network=SimpleNamespace(vip=vip),
        nodes=[SimpleNamespace(ip=node_ip)],
    )


def test_base_url_single_self_signed_no_hostname_falls_back_to_node_ip():
    cfg = _cfg("single", "self_signed", tls_hostname="")
    assert remote.base_url(cfg) == "https://10.0.0.1"


def test_base_url_single_with_hostname():
    cfg = _cfg("single", "acme", tls_hostname="site.example")
    assert remote.base_url(cfg) == "https://site.example"


def test_base_url_single_tls_none_is_http():
    cfg = _cfg("single", "none", tls_hostname="")
    assert remote.base_url(cfg) == "http://10.0.0.1"


def test_base_url_ha_tls_none_uses_vip():
    cfg = _cfg("ha", "none", vip="10.0.0.10")
    assert remote.base_url(cfg) == "http://10.0.0.10"


def test_base_url_ha_uses_hostname_https():
    cfg = _cfg("ha", "self_signed", tls_hostname="cluster.example")
    assert remote.base_url(cfg) == "https://cluster.example"


# --- gen_password() ----------------------------------------------------

def test_gen_password_returns_nonempty_varying_strings():
    a = remote.gen_password()
    b = remote.gen_password()
    assert isinstance(a, str) and len(a) >= 24
    assert a != b
