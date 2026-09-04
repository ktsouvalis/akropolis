"""Thin SSH execution layer.

One `NodeConn` per node; `Fleet` fans a command out to all nodes. Read-only by
convention in preflight; later phases use the same primitives plus `put_file`.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

import paramiko

from .config import Node, SSHConfig


@dataclass
class Result:
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class NodeConn:
    def __init__(self, node: Node, ssh_cfg: SSHConfig, password: str | None = None,
                 sudo_password: str | None = None):
        self.node = node
        self.cfg = ssh_cfg
        self._password = password
        self._sudo_password = sudo_password
        self._client: paramiko.SSHClient | None = None
        self._fleet: "Fleet | None" = None  # set by Fleet.__init__; reaches its transcript

    def connect(self, timeout: float = 10.0) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # TOFU; known_hosts respected first
        client.load_system_host_keys()
        kwargs: dict = {
            "hostname": self.node.ip,
            "port": self.cfg.port,
            "username": self.cfg.user,
            "timeout": timeout,
            "allow_agent": self.cfg.auth in ("agent", "key"),
            "look_for_keys": self.cfg.auth in ("agent", "key"),
        }
        if self.cfg.auth == "key" and self.cfg.key_file:
            kwargs["key_filename"] = os.path.expanduser(self.cfg.key_file)
        if self.cfg.auth == "password":
            kwargs["password"] = self._password
        client.connect(**kwargs)
        self._client = client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def run(self, cmd: str, sudo: bool | None = None, timeout: float = 30.0) -> Result:
        if self._client is None:
            self.connect()
        use_sudo = self.cfg.become if sudo is None else sudo
        feed_sudo_pw = False
        if use_sudo and self.cfg.user != "root":
            if self._sudo_password:
                # -S reads the password from stdin; -p '' keeps the prompt out
                # of stderr; -k forces a fresh authentication so a stale
                # timestamp can't make the stdin line leak into the command.
                cmd = f"sudo -S -k -p '' -- sh -c {shlex.quote(cmd)}"
                feed_sudo_pw = True
            else:
                cmd = f"sudo -n -- sh -c {shlex.quote(cmd)}"
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)  # type: ignore[union-attr]
        if feed_sudo_pw:
            stdin.write(self._sudo_password + "\n")
            stdin.flush()
            # EOF stdin: if the password is wrong, sudo's re-prompt reads EOF
            # and exits immediately instead of hanging until the SSH timeout.
            stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        result = Result(rc=rc, out=out.strip(), err=err.strip())
        if self._fleet is not None and self._fleet.transcript is not None:
            self._fleet.transcript.record(self.node.name, self._fleet.current_phase,
                                          cmd, result.rc, result.out, result.err)
        return result

    def put(self, local_path: str, remote_path: str, callback=None) -> None:
        """SFTP upload — for payloads too large for the base64 push_file pipe
        (SQL dumps). Writes as the SSH user; chmod/chown afterwards via run().
        `callback(transferred_bytes, total_bytes)` streams progress."""
        if self._client is None:
            self.connect()
        sftp = self._client.open_sftp()  # type: ignore[union-attr]
        try:
            sftp.put(local_path, remote_path, callback=callback)
        finally:
            sftp.close()
        if self._fleet is not None and self._fleet.transcript is not None:
            self._fleet.transcript.note(f"[{self.node.name}] ({self._fleet.current_phase}) "
                                        f"SFTP put {local_path} -> {remote_path}")


class Fleet:
    """All three nodes, connected lazily, iterated in config order."""

    def __init__(self, nodes: list[Node], ssh_cfg: SSHConfig, password: str | None = None,
                 sudo_password: str | None = None, transcript=None):
        self.transcript = transcript
        self.current_phase = ""  # set by run_phases()/cmd_clean() as phases start
        self.conns = [NodeConn(n, ssh_cfg, password, sudo_password) for n in nodes]
        for c in self.conns:
            c._fleet = self

    def __iter__(self):
        return iter(self.conns)

    def close(self) -> None:
        for c in self.conns:
            c.close()
