"""akro logs: an unreadable log source must never be reported as clean.

Regression for a real single-node run: the SSH user was not in the docker
group, `docker logs` failed with "permission denied", the level grep dropped
that message, and every container was reported as "no warnings or errors".
"""
import shutil
import subprocess
import textwrap

import pytest

from akropolis.monitor import logs

FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/sh
    # FAKE_MODE: denied | missing | ok | quiet
    case "$FAKE_MODE" in
      denied)
        echo "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock" >&2
        exit 1 ;;
      missing)
        [ "$1" = inspect ] && { echo "Error: No such object: $4" >&2; exit 1; }
        exit 1 ;;
    esac
    if [ "$1" = inspect ]; then echo deadbeef; exit 0; fi
    if [ "$FAKE_MODE" = ok ]; then
      echo '{"level": "info", "event": "starting"}'
      echo '{"level": "warning", "event": "something odd"}' >&2
    else
      echo '{"level": "info", "event": "all quiet"}'
    fi
""")


@pytest.fixture
def fake_docker(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    return bin_dir


def _run(shell, cmd, bin_dir, mode):
    return subprocess.run(
        [shell, "-c", cmd], capture_output=True, text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_MODE": mode},
    )


SHELLS = [s for s in ("sh", "bash", "dash") if shutil.which(s)]


@pytest.mark.parametrize("shell", SHELLS)
def test_permission_denied_exits_with_source_error(shell, fake_docker):
    r = _run(shell, logs.docker_log_cmd("authentik-server-1", 24), fake_docker, "denied")
    assert r.returncode == logs.SOURCE_ERROR_RC
    assert r.stdout == ""
    assert "permission denied" in r.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_missing_container_exits_with_source_error(shell, fake_docker):
    r = _run(shell, logs.docker_log_cmd("nope-1", 24), fake_docker, "missing")
    assert r.returncode == logs.SOURCE_ERROR_RC
    assert "No such object" in r.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_readable_source_still_filters_and_succeeds(shell, fake_docker):
    r = _run(shell, logs.docker_log_cmd("authentik-server-1", 24), fake_docker, "ok")
    assert r.returncode == 0
    assert "something odd" in r.stdout          # container stderr still collected
    assert "starting" not in r.stdout           # info filtered at warning level


@pytest.mark.parametrize("shell", SHELLS)
def test_genuinely_quiet_source_is_not_an_error(shell, fake_docker):
    r = _run(shell, logs.docker_log_cmd("authentik-server-1", 24), fake_docker, "quiet")
    assert r.returncode != logs.SOURCE_ERROR_RC
    assert r.stdout == ""


class _Channel:
    def __init__(self, rc):
        self.rc = rc

    def recv_exit_status(self):
        return self.rc


class _Stream:
    def __init__(self, data, rc=0):
        self._data = data.encode()
        self.channel = _Channel(rc)

    def read(self):
        return self._data


def _fake_client(stdout, stderr, rc):
    class FakeClient:
        def set_missing_host_key_policy(self, _):
            pass

        def connect(self, *a, **kw):
            pass

        def exec_command(self, cmd, timeout=None):
            return None, _Stream(stdout, rc), _Stream(stderr, rc)

        def close(self):
            pass

    return FakeClient


def test_ssh_run_raises_on_source_error(monkeypatch):
    msg = "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock"
    monkeypatch.setattr(logs.paramiko, "SSHClient", _fake_client("", msg, logs.SOURCE_ERROR_RC))
    with pytest.raises(logs.LogSourceError, match="permission denied"):
        logs.ssh_run("192.0.2.1", "u", None, "cmd")


def test_ssh_run_returns_empty_for_clean_source(monkeypatch):
    # grep found nothing: tail exits 0, output empty. That is a real "clean".
    monkeypatch.setattr(logs.paramiko, "SSHClient", _fake_client("", "", 0))
    assert logs.ssh_run("192.0.2.1", "u", None, "cmd") == ""


def test_permission_error_text_carries_docker_group_hint():
    exc = logs.LogSourceError(
        "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock")
    text = logs.format_fetch_error(exc)
    assert text.startswith("cannot read logs: permission denied")
    assert "docker group" in text and "root-equivalent" in text


def test_ssh_failure_text_unchanged():
    assert logs.format_fetch_error(TimeoutError("timed out")) == "SSH error: timed out"


def test_run_save_reports_error_not_clean(monkeypatch, tmp_path, capsys):
    config = {
        "ssh": {"username": "u", "key_file": "unused"},
        "nodes": {"authentik": [{"name": "authentik-single", "ip": "192.0.2.17"}]},
        "services": [{"label": "Auth Server", "nodes": "authentik",
                      "type": "docker", "container": "authentik-server-1"}],
    }

    def denied(*a, **kw):
        raise logs.LogSourceError(
            "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock")

    monkeypatch.setattr(logs, "load_ssh_key", lambda _: None)
    monkeypatch.setattr(logs, "ssh_run", denied)
    out = tmp_path / "report.log"
    logs.run_save(config, logs.load_services(config), str(out), 24)

    printed = capsys.readouterr().out
    assert "Auth Server: ERROR, cannot read logs: permission denied" in printed
    assert "clean" not in printed
    report = out.read_text()
    assert "cannot read logs: permission denied" in report
    assert "docker group" in report
    assert "no warnings or errors" not in report
