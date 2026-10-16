"""Tests for authentik.custom_templates: the directory mirror (push_tree) and
the /templates mount in both compose files.

push_tree is exercised end to end against a LocalConn that runs the shell
commands on this machine, so the find/sort/sha256sum/tar pipeline is the
real one, not a mock of it.
"""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

from akropolis import remote
from akropolis.phases.authentik_phase import custom_templates_plan, push_custom_templates
from akropolis.sshexec import Result


class LocalConn:
    def __init__(self, name="node1"):
        self.node = SimpleNamespace(name=name)

    def run(self, cmd, timeout=30.0):
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout)
        return Result(rc=p.returncode, out=p.stdout.strip(), err=p.stderr.strip())

    def put(self, local_path, remote_path, callback=None):
        shutil.copyfile(local_path, remote_path)


class RecordingCtx:
    def __init__(self, raw):
        self.cfg = SimpleNamespace(raw=raw)
        self.records = []

    def record(self, node, label, ok, detail="", warn=False):
        self.records.append((node, label, ok, detail))


def _tree(root):
    return {p.relative_to(root).as_posix(): p.read_text()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_push_tree_mirrors_then_noops_then_prunes(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "email").mkdir(parents=True)
    (src / "email" / "password_reset.html").write_text("<p>Επαναφορά κωδικού</p>")
    (src / "email" / "password_reset.txt").write_text("Επαναφορά κωδικού")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    (src / ".password_reset.html.swp").write_text("x")
    dst.mkdir()
    (dst / "stale.html").write_text("old")
    conn = LocalConn()

    assert remote.push_tree(conn, src, str(dst)) is True
    assert _tree(dst) == {"email/password_reset.html": "<p>Επαναφορά κωδικού</p>",
                          "email/password_reset.txt": "Επαναφορά κωδικού"}
    assert (dst / "email" / "password_reset.html").stat().st_mode & 0o777 == 0o644
    assert not (tmp_path / "dst.akropolis.tar").exists()

    assert remote.push_tree(conn, src, str(dst)) is False

    (src / "email" / "password_reset.txt").unlink()
    (src / "email" / "password_reset.html").write_text("<p>νέο</p>")
    assert remote.push_tree(conn, src, str(dst)) is True
    assert _tree(dst) == {"email/password_reset.html": "<p>νέο</p>"}


def test_push_tree_keeps_the_mounted_directory(tmp_path):
    # a container bind-mounts the directory: replacing it (new inode) would
    # leave the container looking at the old, deleted one
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    (src / "a.html").write_text("a")
    dst.mkdir()
    inode = dst.stat().st_ino
    remote.push_tree(LocalConn(), src, str(dst))
    assert dst.stat().st_ino == inode


def test_push_custom_templates_reports_changed_nodes(tmp_path, monkeypatch):
    src = tmp_path / "custom-templates"
    (src / "email").mkdir(parents=True)
    (src / "email" / "password_reset.html").write_text("x")
    calls = []
    monkeypatch.setattr("akropolis.phases.authentik_phase.push_tree",
                        lambda conn, local, d: calls.append(conn.node.name) or
                        conn.node.name == "n2")
    ctx = RecordingCtx({"authentik": {"custom_templates": str(src)}})
    touched = push_custom_templates(ctx, [LocalConn("n1"), LocalConn("n2")])
    assert calls == ["n1", "n2"] and touched == {"n2"}
    assert "(unchanged)" in ctx.records[0][3] and "(unchanged)" not in ctx.records[1][3]


def test_custom_templates_unset_or_missing(tmp_path):
    assert push_custom_templates(RecordingCtx({}), [LocalConn()]) == set()
    assert custom_templates_plan({}) == []
    ctx = RecordingCtx({"authentik": {"custom_templates": str(tmp_path / "nope")}})
    try:
        push_custom_templates(ctx, [LocalConn()])
    except RuntimeError as exc:
        assert "not a directory" in str(exc)
    else:
        raise AssertionError("missing directory accepted")


def test_templates_mounted_in_server_and_worker():
    ha = remote.render("authentik-compose.yml.j2",
                       extra_server_volumes=[], extra_worker_volumes=[])
    single = remote.render("authentik-single-compose.yml.j2",
                           extra_server_volumes=[], extra_worker_volumes=[],
                           publish_pg_port=False, pg_loopback=False, pg_port=5432)
    mount = "      - /opt/authentik/custom-templates:/templates\n"
    for out in (ha, single):
        server, worker = out.split("\n  worker:\n", 1)
        assert mount in server and mount in worker
