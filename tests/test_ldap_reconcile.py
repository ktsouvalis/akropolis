"""Tests for akropolis.phases.ldap_reconcile.

Covers the parts that don't require a live Authentik/LDAP stack: the
generated `ak shell` script bodies (in particular that untrusted values are
embedded safely via repr()), the marker-line/error-handling contract in
_run_script, and the run() orchestration logic (leader lookup, source
filtering, confirmation prompting, apply + verify).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akropolis.phases import ldap_reconcile as lr


# --- script generation: must stay syntactically valid Python no matter what
#     goes into source_slug/usernames/changes (they come from Authentik data
#     that this command doesn't control) -----------------------------------

@pytest.mark.parametrize("source_slug", [None, "ldap-prod", "it's \"tricky\"", "line\nbreak"])
def test_dry_run_script_compiles(source_slug):
    script = lr._dry_run_script(source_slug, usernames=None)
    compile(script, "<test>", "exec")  # raises SyntaxError if injection broke it
    assert f"SOURCE_SLUG = {source_slug!r}" in script


def test_dry_run_script_embeds_usernames_safely():
    usernames = ["alice", 'bob"; import os', "carol\nDROP TABLE"]
    script = lr._dry_run_script(None, usernames)
    compile(script, "<test>", "exec")
    assert f"USERNAMES = {usernames!r}" in script


def test_apply_script_compiles_with_hostile_values():
    changes = [
        {"source": "ldap-prod", "username": 'weird"name', "stored": "old'val", "live": "new\nval"},
    ]
    script = lr._apply_script(changes)
    compile(script, "<test>", "exec")


def test_apply_script_embeds_change_tuples():
    changes = [{"source": "s1", "username": "alice", "stored": "old", "live": "new"}]
    script = lr._apply_script(changes)
    assert "('s1', 'alice', 'old', 'new')" in script


# --- _run_script --------------------------------------------------------

class FakeConn:
    def __init__(self, ok=True, out="", err="", node_name="node1"):
        self._ok = ok
        self._out = out
        self._err = err
        self.node = SimpleNamespace(name=node_name)
        self.calls = []

    def run(self, cmd, timeout=None):
        self.calls.append((cmd, timeout))
        return SimpleNamespace(ok=self._ok, out=self._out, err=self._err)


class FakeCtx:
    """Minimal stand-in for PhaseContext: only begin/end_status/record are used
    by ldap_reconcile, and none of them need real Console/State behavior."""

    def __init__(self, cfg=None, fleet=None):
        self.cfg = cfg
        self.fleet = fleet or []
        self.records = []

    def begin(self, node, name, detail=""):
        pass

    def end_status(self):
        pass

    def record(self, node, name, ok, detail="", warn=False):
        self.records.append((node, name, ok, detail, warn))


def test_run_script_raises_on_command_failure():
    conn = FakeConn(ok=False, err="boom")
    with pytest.raises(RuntimeError, match="boom"):
        lr._run_script(FakeCtx(), conn, "print('hi')", "doing a thing")


def test_run_script_raises_when_marker_missing():
    conn = FakeConn(ok=True, out="no marker here")
    with pytest.raises(RuntimeError, match="doing a thing"):
        lr._run_script(FakeCtx(), conn, "print('hi')", "doing a thing")


def test_run_script_parses_marker_line():
    payload = '{"rows": [], "errors": [], "sources_checked": ["s1"]}'
    conn = FakeConn(ok=True, out=f"some noise\n{lr._MARKER}{payload}\nmore noise")
    data = lr._run_script(FakeCtx(), conn, "print('hi')", "reading")
    assert data == {"rows": [], "errors": [], "sources_checked": ["s1"]}


# --- collect_drift / apply_changes --------------------------------------

def test_collect_drift_returns_rows_and_sources(monkeypatch):
    monkeypatch.setattr(lr, "_run_script", lambda ctx, conn, py, label: {
        "rows": [{"username": "alice"}], "errors": [], "sources_checked": ["s1"],
    })
    rows, sources = lr.collect_drift(FakeCtx(), FakeConn())
    assert rows == [{"username": "alice"}]
    assert sources == ["s1"]


def test_apply_changes_short_circuits_on_empty():
    conn = FakeConn()
    result = lr.apply_changes(FakeCtx(), conn, [])
    assert result == []
    assert conn.calls == []  # must not touch the wire for a no-op


def test_apply_changes_runs_script(monkeypatch):
    seen = {}

    def fake_run_script(ctx, conn, py, label, timeout=120):
        seen["py"] = py
        return [{"status": "UPDATED"}]

    monkeypatch.setattr(lr, "_run_script", fake_run_script)
    result = lr.apply_changes(FakeCtx(), FakeConn(), [
        {"source": "s1", "username": "alice", "stored": "old", "live": "new"},
    ])
    assert result == [{"status": "UPDATED"}]
    assert "alice" in seen["py"]


# --- run() orchestration -------------------------------------------------

def make_ctx(leader_name="node1", conn_names=("node1",)):
    cfg = SimpleNamespace(bootstrap_leader=SimpleNamespace(name=leader_name))
    fleet = [FakeConn(node_name=n) for n in conn_names]
    return FakeCtx(cfg=cfg, fleet=fleet)


def test_run_returns_2_when_leader_not_in_fleet():
    ctx = make_ctx(leader_name="ghost", conn_names=("node1",))
    assert lr.run(ctx) == 2


def test_run_returns_2_for_unknown_source(monkeypatch):
    monkeypatch.setattr(lr, "collect_drift", lambda ctx, conn, source=None, usernames=None: ([], ["s1"]))
    ctx = make_ctx()
    assert lr.run(ctx, source="typo'd") == 2


def test_run_returns_2_when_no_sources_configured(monkeypatch):
    monkeypatch.setattr(lr, "collect_drift", lambda ctx, conn, source=None, usernames=None: ([], []))
    ctx = make_ctx()
    assert lr.run(ctx) == 2


def test_run_returns_0_when_no_rows(monkeypatch):
    monkeypatch.setattr(lr, "collect_drift", lambda ctx, conn, source=None, usernames=None: ([], ["s1"]))
    ctx = make_ctx()
    assert lr.run(ctx) == 0


def test_run_returns_0_when_all_same(monkeypatch):
    rows = [{"source": "s1", "username": "alice", "status": "SAME", "stored": "x", "live": "x", "path": "", "active": True}]
    monkeypatch.setattr(lr, "collect_drift", lambda ctx, conn, source=None, usernames=None: (rows, ["s1"]))
    ctx = make_ctx()
    assert lr.run(ctx) == 0


def test_run_returns_0_when_only_not_found(monkeypatch):
    rows = [{"source": "s1", "username": "alice", "status": "NOT_FOUND_IN_LDAP",
             "stored": "x", "live": None, "path": "", "active": True}]
    monkeypatch.setattr(lr, "collect_drift", lambda ctx, conn, source=None, usernames=None: (rows, ["s1"]))
    ctx = make_ctx()
    assert lr.run(ctx) == 0


def test_run_declines_when_user_says_no(monkeypatch):
    rows = [{"source": "s1", "username": "alice", "status": "DIFFERENT",
             "stored": "old", "live": "new", "path": "", "active": True}]
    monkeypatch.setattr(lr, "collect_drift", lambda ctx, conn, source=None, usernames=None: (rows, ["s1"]))
    apply_called = []
    monkeypatch.setattr(lr, "apply_changes", lambda ctx, conn, changes: apply_called.append(changes))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    ctx = make_ctx()
    assert lr.run(ctx) == 0
    assert apply_called == []


def test_run_applies_confirmed_change_and_verifies(monkeypatch):
    drifted = [{"source": "s1", "username": "alice", "status": "DIFFERENT",
                "stored": "old", "live": "new", "path": "", "active": True}]
    same_after = [{"source": "s1", "username": "alice", "status": "SAME",
                   "stored": "new", "live": "new", "path": "", "active": True}]
    calls = {"n": 0}

    def fake_collect_drift(ctx, conn, source=None, usernames=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return drifted, ["s1"]
        return same_after, ["s1"]

    monkeypatch.setattr(lr, "collect_drift", fake_collect_drift)
    monkeypatch.setattr(lr, "apply_changes", lambda ctx, conn, changes: [
        {"username": "alice", "status": "UPDATED"}
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    ctx = make_ctx()
    assert lr.run(ctx) == 0
    statuses = [r[2] for r in ctx.records]
    assert all(statuses)  # both the UPDATED record and the verify SAME record are ok=True


def test_run_returns_1_when_apply_fails(monkeypatch):
    # Verify is mocked to report SAME (i.e. it would independently pass) so
    # this isolates the apply-result loop's own ok/fail bookkeeping rather
    # than relying on the verify step to also catch the failure.
    drifted = [{"source": "s1", "username": "alice", "status": "DIFFERENT",
                "stored": "old", "live": "new", "path": "", "active": True}]
    same_after = [{"source": "s1", "username": "alice", "status": "SAME",
                   "stored": "old", "live": "old", "path": "", "active": True}]
    calls = {"n": 0}

    def fake_collect_drift(ctx, conn, source=None, usernames=None):
        calls["n"] += 1
        return (drifted, ["s1"]) if calls["n"] == 1 else (same_after, ["s1"])

    monkeypatch.setattr(lr, "collect_drift", fake_collect_drift)
    monkeypatch.setattr(lr, "apply_changes", lambda ctx, conn, changes: [
        {"username": "alice", "status": "SKIPPED_CHANGED", "expected": "old", "found": "different"}
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    ctx = make_ctx()
    assert lr.run(ctx) == 1


def test_run_returns_1_when_verify_disagrees(monkeypatch):
    drifted = [{"source": "s1", "username": "alice", "status": "DIFFERENT",
                "stored": "old", "live": "new", "path": "", "active": True}]
    still_different = [{"source": "s1", "username": "alice", "status": "DIFFERENT",
                        "stored": "old", "live": "newer", "path": "", "active": True}]
    calls = {"n": 0}

    def fake_collect_drift(ctx, conn, source=None, usernames=None):
        calls["n"] += 1
        return (drifted, ["s1"]) if calls["n"] == 1 else (still_different, ["s1"])

    monkeypatch.setattr(lr, "collect_drift", fake_collect_drift)
    monkeypatch.setattr(lr, "apply_changes", lambda ctx, conn, changes: [
        {"username": "alice", "status": "UPDATED"}
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    ctx = make_ctx()
    assert lr.run(ctx) == 1
