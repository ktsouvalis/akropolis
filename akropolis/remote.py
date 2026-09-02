"""Shared helpers for write-phases: template rendering, checksummed file push,
and poll-until-healthy waits."""

from __future__ import annotations

import hashlib
import shlex
import time
from importlib import resources

from jinja2 import Environment, StrictUndefined

from .sshexec import NodeConn

_env = Environment(undefined=StrictUndefined, keep_trailing_newline=True,
                   trim_blocks=True, lstrip_blocks=True)


def render(template_name: str, **ctx) -> str:
    src = resources.files("akropolis.templates").joinpath(template_name).read_text()
    return _env.from_string(src).render(**ctx)


def push_file(conn: NodeConn, content: str, remote_path: str,
              mode: str = "0644", owner: str | None = None) -> bool:
    """Write `content` to `remote_path`. Returns True if the file changed.

    Uploads via a shell heredoc-free base64 pipe (works under sudo/become too),
    compares sha256 first so unchanged files are a detected no-op.
    """
    digest = hashlib.sha256(content.encode()).hexdigest()
    r = conn.run(f"sha256sum {shlex.quote(remote_path)} 2>/dev/null | cut -d' ' -f1")
    if r.ok and r.out == digest:
        return False

    import base64
    b64 = base64.b64encode(content.encode()).decode()
    dirpath = remote_path.rsplit("/", 1)[0]
    cmd = (f"mkdir -p {shlex.quote(dirpath)} && "
           f"echo {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)} && "
           f"chmod {mode} {shlex.quote(remote_path)}")
    if owner:
        cmd += f" && chown {owner} {shlex.quote(remote_path)}"
    r = conn.run(cmd, timeout=60)
    if not r.ok:
        raise RuntimeError(f"[{conn.node.name}] failed to write {remote_path}: {r.err}")
    return True


def wait_for(conn: NodeConn, cmd: str, expect: str | None = None,
             timeout: float = 120.0, interval: float = 5.0,
             label: str = "") -> bool:
    """Poll `cmd` over SSH until it exits 0 (and, if given, stdout contains `expect`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = conn.run(cmd, timeout=min(30, timeout))
        if r.ok and (expect is None or expect in r.out):
            return True
        time.sleep(interval)
    return False


def gen_password() -> str:
    import secrets as _s
    return _s.token_urlsafe(24)
