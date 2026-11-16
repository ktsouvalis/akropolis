"""Tests for akropolis.state.State: phase bookkeeping and once-only secrets."""

from __future__ import annotations

import json
import stat

import pytest

from akropolis import state as state_mod
from akropolis.state import State, StateLocked

PW = "correct horse battery"


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(state_mod, "SCRYPT_N", 2 ** 10)  # tests, not security


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "sub" / "site.json"  # parent dir doesn't exist yet -- save() must create it


def test_new_state_has_empty_defaults(state_path):
    st = State(state_path, "site-a")
    assert st.data == {"tool": "akropolis", "site": "site-a", "phases": {}, "generated": {}}
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


def test_mark_phase_clears_previous_error(state_path):
    st = State(state_path, "site-a")
    st.mark_phase("nginx", "failed", {"error": "certbot failed"})
    st.mark_phase("nginx", "pending")
    assert "error" not in st.data["phases"]["nginx"]
    st.mark_phase("nginx", "failed", {"error": "again"})
    st.mark_phase("nginx", "done")
    assert "error" not in st.data["phases"]["nginx"]


def test_mark_phase_creates_parent_dirs(state_path):
    assert not state_path.parent.exists()
    State(state_path, "site-a").mark_phase("etcd", "done")
    assert state_path.parent.exists()


def test_save_sets_owner_only_permissions(state_path):
    State(state_path, "site-a").mark_phase("etcd", "done")
    mode = stat.S_IMODE(state_path.stat().st_mode)
    assert mode == 0o600


def test_get_or_generate_generates_once(state_path):
    st = State(state_path, "site-a", PW)
    st.unlock()
    calls = []

    def gen():
        calls.append(1)
        return "secret-value"

    first = st.get_or_generate("etcd_token", gen)
    second = st.get_or_generate("etcd_token", gen)
    assert first == second == "secret-value"
    assert len(calls) == 1  # generator must not run twice


def test_get_or_generate_persists_across_instances(state_path):
    st = State(state_path, "site-a", PW)
    st.unlock()
    st.get_or_generate("etcd_token", lambda: "pinned")
    reloaded = State(state_path, "site-a", PW)
    reloaded.unlock()
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


# --- encryption at rest -------------------------------------------------------

def _pinned(path, site="site-a", **values):
    st = State(path, site, PW)
    st.unlock()
    for k, v in values.items():
        st.get_or_generate(k, lambda v=v: v)
    return st


def test_secrets_never_written_in_plain_text(state_path):
    _pinned(state_path, authentik_secret_key="s3cr3t-value-xyz", pg_password="pg-pw-123")
    raw = state_path.read_text()
    assert "s3cr3t-value-xyz" not in raw and "pg-pw-123" not in raw
    on_disk = json.loads(raw)
    assert "generated" not in on_disk
    assert on_disk["format"] == 2
    assert on_disk["generated_keys"] == ["authentik_secret_key", "pg_password"]
    assert on_disk["generated_enc"]["kdf"] == "scrypt"


def test_round_trip(state_path):
    _pinned(state_path, a="1", b="2")
    st = State(state_path, "site-a", PW)
    st.unlock()
    assert st.data["generated"] == {"a": "1", "b": "2"}


def test_locked_state_refuses_reads(state_path):
    _pinned(state_path, a="1")
    st = State(state_path, "site-a")
    assert st.locked
    with pytest.raises(StateLocked):
        st.get_or_generate("a", lambda: "regenerated")
    with pytest.raises(StateLocked):
        st.data["generated"].get("a")


def test_locked_state_keeps_blob_on_phase_save(state_path):
    _pinned(state_path, a="1")
    blob = json.loads(state_path.read_text())["generated_enc"]
    st = State(state_path, "site-a")  # e.g. `akro shutdown`: no passphrase
    st.mark_phase("authentik-shutdown", "done")
    assert json.loads(state_path.read_text())["generated_enc"] == blob
    assert st.generated_keys() == ["a"]
    again = State(state_path, "site-a", PW)
    again.unlock()
    assert again.data["generated"] == {"a": "1"}


def test_wrong_passphrase(state_path):
    _pinned(state_path, a="1")
    before = state_path.read_text()
    st = State(state_path, "site-a", "not the passphrase")
    with pytest.raises(StateLocked, match="wrong passphrase"):
        st.unlock()
    assert st.locked
    assert state_path.read_text() == before


def test_blob_bound_to_site(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _pinned(a, "site-a", x="from-a")
    _pinned(b, "site-b", x="from-b")
    doc_b = json.loads(b.read_text())
    doc_b["generated_enc"] = json.loads(a.read_text())["generated_enc"]
    b.write_text(json.dumps(doc_b))
    with pytest.raises(StateLocked):
        State(b, "site-b", PW).unlock()


def test_foreign_tool_refused(state_path):
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"tool": "charmer", "site": "site-a",
                                      "phases": {}, "generated": {}}))
    with pytest.raises(RuntimeError, match="charmer"):
        State(state_path, "site-a")


def test_plaintext_file_migrates_on_save(state_path):
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"site": "site-a", "phases": {"etcd": {"status": "done"}},
                                      "generated": {"etcd_token": "old-plain-token"}}))
    st = State(state_path, "site-a")
    assert st.plaintext_on_disk and not st.locked
    assert st.data["generated"]["etcd_token"] == "old-plain-token"  # readable before unlock
    with pytest.raises(StateLocked, match="plain text"):
        st.save()
    st = State(state_path, "site-a", PW)
    st.unlock()
    st.save()
    raw = state_path.read_text()
    assert "old-plain-token" not in raw
    st = State(state_path, "site-a", PW)
    assert st.encrypted and not st.plaintext_on_disk
    st.unlock()
    assert st.data["generated"] == {"etcd_token": "old-plain-token"}
    assert st.phase_status("etcd") == "done"


def test_nothing_pinned_needs_no_passphrase(state_path):
    State(state_path, "site-a").mark_phase("preflight", "done")
    on_disk = json.loads(state_path.read_text())
    assert "generated_enc" not in on_disk and on_disk["generated_keys"] == []


def test_change_passphrase(state_path):
    _pinned(state_path, a="1")
    calls = iter([PW, "a brand new passphrase"])
    st = State(state_path, "site-a", lambda _new: next(calls))
    st.change_passphrase()
    with pytest.raises(StateLocked):
        State(state_path, "site-a", PW).unlock()
    st = State(state_path, "site-a", "a brand new passphrase")
    st.unlock()
    assert st.data["generated"] == {"a": "1"}


def test_env_var_passphrase(state_path, monkeypatch):
    _pinned(state_path, a="1")
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    st = State(state_path, "site-a")  # default source: env var first
    st.unlock()
    assert st.data["generated"] == {"a": "1"}


def test_passphrase_file(state_path, tmp_path, monkeypatch):
    monkeypatch.delenv(state_mod.PASSPHRASE_ENV, raising=False)
    _pinned(state_path, a="1")
    pw_file = tmp_path / "pw"
    pw_file.write_text(PW + "\n")
    st = State(state_path, "site-a", state_mod.passphrase_source(pw_file))
    st.unlock()
    assert st.data["generated"] == {"a": "1"}


def test_tmp_file_is_owner_only_from_creation(state_path, monkeypatch):
    seen = {}
    real_replace = type(state_path).replace

    def spy(self, target):
        seen["mode"] = stat.S_IMODE(self.stat().st_mode)
        return real_replace(self, target)

    monkeypatch.setattr(type(state_path), "replace", spy)
    State(state_path, "site-a").mark_phase("etcd", "done")
    assert seen["mode"] == 0o600
