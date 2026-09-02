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
    def __init__(self, node: Node, ssh_cfg: SSHConfig, password: str | None = None):
        self.node = node
        self.cfg = ssh_cfg
        self._password = password
        self._client: paramiko.SSHClient | None = None

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
        if use_sudo and self.cfg.user != "root":
            cmd = f"sudo -n -- sh -c {shlex.quote(cmd)}"
        _stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)  # type: ignore[union-attr]
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        return Result(rc=rc, out=out.strip(), err=err.strip())


class Fleet:
    """All three nodes, connected lazily, iterated in config order."""

    def __init__(self, nodes: list[Node], ssh_cfg: SSHConfig, password: str | None = None):
        self.conns = [NodeConn(n, ssh_cfg, password) for n in nodes]

    def __iter__(self):
        return iter(self.conns)

    def close(self) -> None:
        for c in self.conns:
            c.close()
