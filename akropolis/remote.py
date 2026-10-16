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
        # Content already in place — but still converge mode/owner. An earlier
        # run (or an earlier akropolis version) may have written this file with
        # different perms; skipping here would leave that drift in place forever
        # (e.g. haproxy.cfg pushed as 0600 before v0.7.6 → unreadable by the
        # container's non-root user).
        m = conn.run(f"stat -c %a {shlex.quote(remote_path)}")
        try:
            mode_drift = m.ok and int(m.out.strip(), 8) != int(mode, 8)
        except ValueError:
            mode_drift = False
        fix = []
        if mode_drift:
            fix.append(f"chmod {mode} {shlex.quote(remote_path)}")
        if owner:
            fix.append(f"chown {owner} {shlex.quote(remote_path)}")
        if fix:
            r2 = conn.run(" && ".join(fix))
            if not r2.ok:
                raise RuntimeError(
                    f"[{conn.node.name}] failed to fix perms on {remote_path}: {r2.err}")
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


def push_binary(conn: NodeConn, local_path, remote_path: str,
                mode: str = "0644") -> bool:
    """Upload a LOCAL BINARY file (images, certs) to `remote_path` over SFTP.

    push_file() base64-encodes a str through the shell, which is fine for
    configs but wrong for binaries and wasteful for anything large. Returns
    True if the remote file changed; unchanged files are a detected no-op so
    re-runs don't churn the compose stack.

    SFTP runs as the SSH user and CANNOT escalate — `sudo` applies to run()
    only. Writing straight to a root-owned directory therefore fails with
    EACCES under `become: true`. So the payload is staged in /tmp (world
    writable) and moved into place by a privileged run(), which also owns the
    mkdir, the final mode and the ownership.
    """
    import hashlib as _h
    import os as _os
    from pathlib import Path as _P

    local = _P(local_path).expanduser()
    digest = _h.sha256(local.read_bytes()).hexdigest()
    r = conn.run(f"sha256sum {shlex.quote(remote_path)} 2>/dev/null | cut -d' ' -f1")
    if r.ok and r.out.strip() == digest:
        return False

    dirpath = remote_path.rsplit("/", 1)[0]
    staging = f"/tmp/.akropolis-upload-{_os.getpid()}-{local.name}"
    try:
        conn.put(str(local), staging)
    except OSError as exc:
        # a bare "[Errno 13] Permission denied" names neither the node, the
        # file, nor which end of the transfer refused
        raise RuntimeError(
            f"[{conn.node.name}] SFTP upload of {local} to {staging} failed: {exc}"
        ) from exc
    r = conn.run(f"mkdir -p {shlex.quote(dirpath)} && "
                 f"mv {shlex.quote(staging)} {shlex.quote(remote_path)} && "
                 f"chmod {mode} {shlex.quote(remote_path)}", timeout=120)
    if not r.ok:
        conn.run(f"rm -f {shlex.quote(staging)}")
        raise RuntimeError(f"[{conn.node.name}] failed to install {remote_path}: {r.err}")

    # integrity is checked after the move, not before: a truncated transfer
    # would otherwise be mounted into the container and serve a broken asset
    r = conn.run(f"sha256sum {shlex.quote(remote_path)} | cut -d' ' -f1")
    if r.out.strip() != digest:
        raise RuntimeError(f"[{conn.node.name}] {remote_path}: checksum mismatch "
                           "after upload — transfer corrupted")
    return True


def _tree_files(local_dir) -> list[tuple[str, "Path"]]:
    """(./relative path, file) for every regular file under local_dir, in
    byte order (what `LC_ALL=C sort` gives on the node). Anything with a
    dot-component (.git, .DS_Store, editor swap files) is left out."""
    from pathlib import Path
    root = Path(local_dir).expanduser()
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if not p.is_file() or any(part.startswith(".") for part in rel.parts):
            continue
        out.append(("./" + rel.as_posix(), p))
    return sorted(out, key=lambda t: t[0].encode())


def tree_manifest(local_dir) -> str:
    """The `sha256sum` listing of local_dir, in the exact format
    _REMOTE_MANIFEST prints for the node's copy."""
    return "\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {rel}"
                     for rel, p in _tree_files(local_dir))


def _remote_manifest_cmd(remote_dir: str) -> str:
    return (f"cd {shlex.quote(remote_dir)} 2>/dev/null && "
            "find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum")


def push_tree(conn: NodeConn, local_dir, remote_dir: str) -> bool:
    """Make remote_dir an exact mirror of local_dir: same files, same
    content, nothing extra. Returns True if anything changed.

    Compared by a sha256sum listing of both sides, so an unchanged tree is a
    no-op. On a change the whole tree goes over as one tar (via push_binary,
    so it's staged and privileged like any other upload) and replaces the
    directory's CONTENTS. The directory itself is kept, since a container may
    have it bind-mounted. Files land 0644 root-owned, dirs 0755, so a
    container running as a non-root user can read them.
    """
    import io
    import os
    import tarfile
    import tempfile

    want = tree_manifest(local_dir)
    r = conn.run(_remote_manifest_cmd(remote_dir))
    if r.ok and r.out.strip() == want:
        return False

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, p in _tree_files(local_dir):
            data = p.read_bytes()
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    fd, tmp = tempfile.mkstemp(suffix=".tar", prefix="akropolis-tree-")
    staged = remote_dir.rstrip("/") + ".akropolis.tar"
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf.getvalue())
        push_binary(conn, tmp, staged, mode="0600")
    finally:
        os.unlink(tmp)

    d, t = shlex.quote(remote_dir), shlex.quote(staged)
    r = conn.run(f"mkdir -p {d} && find {d} -mindepth 1 -delete && "
                 f"tar -xf {t} -C {d} --no-same-owner && "
                 f"chmod -R u=rwX,go=rX {d}; rc=$?; rm -f {t}; exit $rc", timeout=120)
    if not r.ok:
        raise RuntimeError(f"[{conn.node.name}] failed to install {remote_dir}: {r.err}")
    r = conn.run(_remote_manifest_cmd(remote_dir))
    if r.out.strip() != want:
        raise RuntimeError(f"[{conn.node.name}] {remote_dir}: contents differ from "
                           f"{local_dir} after upload — transfer corrupted")
    return True


def wait_for(conn: NodeConn, cmd: str, expect: str | None = None,
             timeout: float = 120.0, interval: float = 5.0,
             label: str = "", tick=None) -> bool:
    """Poll `cmd` over SSH until it exits 0 (and, if given, stdout contains `expect`).

    `tick(elapsed_seconds)` is called once per poll so the caller can keep a
    live "still waiting: 40s / 900s" line on screen instead of dead air.
    """
    start = time.monotonic()
    deadline = start + timeout
    while time.monotonic() < deadline:
        if tick:
            tick(time.monotonic() - start)
        r = conn.run(cmd, timeout=min(30, timeout))
        if r.ok and (expect is None or expect in r.out):
            return True
        time.sleep(interval)
    return False


def base_url(cfg) -> str:
    """The external URL an operator/browser reaches this site at.

    Fed into AUTHENTIK_WEB__BASE_URL (a bootstrap value: authentik copies it
    into the Base URL system setting on first start ONLY, never overwrites a
    value already set via the Admin interface/API — see docs.goauthentik.io/
    sys-mgmt/settings) and reused for the handoff landing card, so the two
    can't independently drift.
    """
    if cfg.topology == "single":
        # A bare-metal nginx (nginx_single_phase.py) serves 80/443 here now,
        # not authentik directly. It's plain HTTP for tls.provider 'none'
        # (testing only, same semantics as ha's 'none' below) and HTTPS
        # otherwise (self-signed on first boot, or a real cert via
        # acme/import). hostname can be blank only for single+self_signed
        # (config.py); fall back to the node's own IP in that case.
        host = cfg.tls.hostname or cfg.nodes[0].ip
        scheme = "http" if cfg.tls.provider == "none" else "https"
        return f"{scheme}://{host}"
    if cfg.tls.provider == "none":
        return f"http://{cfg.network.vip}"
    return f"https://{cfg.tls.hostname}"


def gen_password() -> str:
    import secrets as _s
    return _s.token_urlsafe(24)
