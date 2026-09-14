"""Tests for akropolis.state.State: phase bookkeeping and once-only secrets."""

from __future__ import annotations

import json
import stat

import pytest

from akropolis.state import State


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "sub" / "site.json"  # parent dir doesn't exist yet -- save() must create it


def test_new_state_has_empty_defaults(state_path):
    st = State(state_path, "site-a")
    assert st.data == {"site": "site-a", "phases": {}, "generated": {}}
    assert not state_path.exists()  # nothing written until save() is called


def test_phase_status_defaults_to_pending(state_path):
    st = State(state_path, "site-a")
    assert st.phase_status("etcd") == "pending"


def test_mark_phase_persists_status_and_timestamp(state_path):
    st = State(state_path, "site-a")
    st.mark_phase("etcd", "done")
    assert st.phase_status("etcd") == "done"
    assert state_path.exists()
    on_disk = json.loads(state_path.read_text())
    assert on_disk["phases"]["etcd"]["status"] == "done"
    assert "updated_at" in on_disk["phases"]["etcd"]


def test_mark_phase_merges_detail(state_path):
    st = State(state_path, "site-a")
    st.mark_phase("etcd", "failed", {"error": "timeout"})
    entry = st.data["phases"]["etcd"]
    assert entry["status"] == "failed"
    assert entry["error"] == "timeout"


def test_mark_phase_creates_parent_dirs(state_path):
    assert not state_path.parent.exists()
    State(state_path, "site-a").mark_phase("etcd", "done")
    assert state_path.parent.exists()


def test_save_sets_owner_only_permissions(state_path):
    State(state_path, "site-a").mark_phase("etcd", "done")
    mode = stat.S_IMODE(state_path.stat().st_mode)
    assert mode == 0o600


def test_get_or_generate_generates_once(state_path):
    st = State(state_path, "site-a")
    calls = []

    def gen():
        calls.append(1)
        return "secret-value"

    first = st.get_or_generate("etcd_token", gen)
    second = st.get_or_generate("etcd_token", gen)
    assert first == second == "secret-value"
    assert len(calls) == 1  # generator must not run twice


def test_get_or_generate_persists_across_instances(state_path):
    State(state_path, "site-a").get_or_generate("etcd_token", lambda: "pinned")
    reloaded = State(state_path, "site-a")
    assert reloaded.get_or_generate("etcd_token", lambda: "should-not-be-used") == "pinned"


def test_phase_status_persists_across_instances(state_path):
    State(state_path, "site-a").mark_phase("etcd", "done")
    reloaded = State(state_path, "site-a")
    assert reloaded.phase_status("etcd") == "done"


def test_loading_state_for_wrong_site_refuses(state_path):
    State(state_path, "site-a").mark_phase("etcd", "done")
    with pytest.raises(RuntimeError, match="site-a"):
        State(state_path, "site-b")


def test_save_is_atomic_no_leftover_tmp_file(state_path):
    State(state_path, "site-a").mark_phase("etcd", "done")
    assert not state_path.with_suffix(".tmp").exists()
