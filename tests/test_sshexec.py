"""Tests for akropolis.sshexec: Result, NodeConn.run()'s sudo-wrapping and
transcript recording, and Fleet fan-out.

paramiko itself is never touched -- a fake client stands in for the
paramiko.SSHClient, since exercising sudo-wrapping and result plumbing
doesn't require a real transport.
"""

from __future__ import annotations

from types import SimpleNamespace

from akropolis.config import Node, SSHConfig
from akropolis.sshexec import Fleet, NodeConn, Result


class FakeChannel:
    def __init__(self, exit_status=0):
        self._exit_status = exit_status
        self.shutdown_write_called = False

    def recv_exit_status(self):
        return self._exit_status

    def shutdown_write(self):
        self.shutdown_write_called = True


class FakeStream:
    def __init__(self, data: bytes = b"", channel: FakeChannel | None = None):
        self._data = data
        self.channel = channel
        self.written = []
        self.flushed = False

    def read(self):
        return self._data

    def write(self, data):
        self.written.append(data)

    def flush(self):
        self.flushed = True


class FakeSSHClient:
    def __init__(self, out=b"", err=b"", exit_status=0):
        self.exec_calls = []
        self._out = out
        self._err = err
        self._exit_status = exit_status
        self.closed = False

    def exec_command(self, cmd, timeout=None):
        self.exec_calls.append((cmd, timeout))
        channel = FakeChannel(self._exit_status)
        stdin = FakeStream(channel=channel)
        stdout = FakeStream(self._out, channel=channel)
        stderr = FakeStream(self._err)
        return stdin, stdout, stderr

    def close(self):
        self.closed = True


def make_conn(user="root", auth="key", become=False, password=None, sudo_password=None):
    node = Node(name="node1", ip="10.0.0.1")
    ssh_cfg = SSHConfig(user=user, become=become, auth=auth, key_file=None)
    conn = NodeConn(node, ssh_cfg, password=password, sudo_password=sudo_password)
    return conn


# --- Result -----------------------------------------------------------

def test_result_ok_true_on_zero_rc():
    assert Result(rc=0, out="", err="").ok is True


def test_result_ok_false_on_nonzero_rc():
    assert Result(rc=1, out="", err="").ok is False


# --- NodeConn.run(): sudo wrapping ---------------------------------------

def test_run_as_root_never_wraps_with_sudo():
    conn = make_conn(user="root", become=True)  # become=True but user is already root
    fake = FakeSSHClient(out=b"hi\n")
    conn._client = fake
    conn.run("whoami")
    cmd, _ = fake.exec_calls[0]
    assert cmd == "whoami"


def test_run_non_root_without_become_is_unwrapped():
    conn = make_conn(user="deploy", become=False)
    fake = FakeSSHClient()
    conn._client = fake
    conn.run("whoami")
    cmd, _ = fake.exec_calls[0]
    assert cmd == "whoami"


def test_run_non_root_with_become_no_password_uses_sudo_n():
    conn = make_conn(user="deploy", become=True, sudo_password=None)
    fake = FakeSSHClient()
    conn._client = fake
    conn.run("whoami")
    cmd, _ = fake.exec_calls[0]
    assert cmd.startswith("sudo -n --")
    assert "whoami" in cmd


def test_run_non_root_with_become_and_password_feeds_stdin():
    conn = make_conn(user="deploy", become=True, sudo_password="hunter2")
    fake = FakeSSHClient()
    conn._client = fake
    conn.run("whoami")
    cmd, _ = fake.exec_calls[0]
    assert cmd.startswith("sudo -S -k -p ''")


def test_run_explicit_sudo_true_overrides_become_false():
    conn = make_conn(user="deploy", become=False)
    fake = FakeSSHClient()
    conn._client = fake
    conn.run("whoami", sudo=True)
    cmd, _ = fake.exec_calls[0]
    assert cmd.startswith("sudo")


def test_run_explicit_sudo_false_overrides_become_true():
    conn = make_conn(user="deploy", become=True)
    fake = FakeSSHClient()
    conn._client = fake
    conn.run("whoami", sudo=False)
    cmd, _ = fake.exec_calls[0]
    assert cmd == "whoami"


def test_run_result_reflects_output_and_exit_status():
    conn = make_conn()
    fake = FakeSSHClient(out=b"  stdout text  \n", err=b" stderr text \n", exit_status=7)
    conn._client = fake
    result = conn.run("cmd")
    assert result.rc == 7
    assert result.out == "stdout text"
    assert result.err == "stderr text"
    assert result.ok is False


def test_run_connects_lazily_when_no_client():
    conn = make_conn()
    connected = []
    conn.connect = lambda timeout=10.0: (connected.append(True), setattr(conn, "_client", FakeSSHClient()))
    conn.run("whoami")
    assert connected == [True]


def test_run_records_to_transcript_when_attached():
    conn = make_conn()
    fake = FakeSSHClient(out=b"ok", exit_status=0)
    conn._client = fake

    records = []
    fleet = SimpleNamespace(
        transcript=SimpleNamespace(record=lambda *a: records.append(a)),
        current_phase="etcd",
    )
    conn._fleet = fleet
    conn.run("whoami")
    assert len(records) == 1
    node_name, phase, cmd, rc, out, err = records[0]
    assert (node_name, phase, cmd, rc, out, err) == ("node1", "etcd", "whoami", 0, "ok", "")


def test_run_no_transcript_is_a_noop():
    conn = make_conn()
    conn._client = FakeSSHClient()
    conn._fleet = SimpleNamespace(transcript=None, current_phase="etcd")
    conn.run("whoami")  # must not raise


# --- Fleet ---------------------------------------------------------------

def test_fleet_iterates_nodes_in_order():
    nodes = [Node(name=f"n{i}", ip=f"10.0.0.{i}") for i in range(1, 4)]
    ssh_cfg = SSHConfig(user="root", auth="key", key_file=None)
    fleet = Fleet(nodes, ssh_cfg)
    assert [c.node.name for c in fleet] == ["n1", "n2", "n3"]


def test_fleet_sets_backref_on_each_conn():
    nodes = [Node(name="n1", ip="10.0.0.1")]
    ssh_cfg = SSHConfig(user="root", auth="key", key_file=None)
    fleet = Fleet(nodes, ssh_cfg)
    assert fleet.conns[0]._fleet is fleet


def test_fleet_close_closes_every_conn():
    nodes = [Node(name=f"n{i}", ip=f"10.0.0.{i}") for i in range(1, 3)]
    ssh_cfg = SSHConfig(user="root", auth="key", key_file=None)
    fleet = Fleet(nodes, ssh_cfg)
    for c in fleet.conns:
        c._client = FakeSSHClient()
    fleet.close()
    for c in fleet.conns:
        assert c._client is None
