"""Tests for akropolis.cli: `status` command (phase_rows + cmd_status)."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
import yaml

import json

from akropolis import state as state_mod
from akropolis.cli import cmd_state, cmd_status, phase_rows, pipeline_for
from akropolis.config import CONFIG_SCHEMA_VERSION
from akropolis.state import State

SINGLE_BASE = {
    "site": {
        "name": "test-single",
        "environment": "lab",
        "topology": "single",
        "config_version": CONFIG_SCHEMA_VERSION,
    },
    "nodes": [{"name": "node1", "ip": "10.0.0.1"}],
    "ssh": {"user": "root", "auth": "key", "key_file": None},
    "tls": {"provider": "self_signed"},
}

SINGLE_PHASE_NAMES = [p.name for p in pipeline_for("single")]


@pytest.fixture
def ssh_key(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("fake key material")
    return str(key)


@pytest.fixture
def write_config(tmp_path, ssh_key):
    def _write(overrides: dict | None = None):
        doc = copy.deepcopy(SINGLE_BASE)
        doc["ssh"]["key_file"] = ssh_key
        doc.setdefault("provision", {})["state_file"] = str(tmp_path / "site.json")
        if overrides:
            doc.update(overrides)
        path = tmp_path / "config.yml"
        path.write_text(yaml.safe_dump(doc))
        return path

    return _write


# --- phase_rows (pure) -------------------------------------------------------

def test_phase_rows_all_pending_on_fresh_state(tmp_path):
    state = State(tmp_path / "site.json", "test-single")
    rows = phase_rows(pipeline_for("single"), state)
    assert [r["name"] for r in rows] == SINGLE_PHASE_NAMES
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["updated_at"] == "" for r in rows)


def test_phase_rows_merges_done_and_failed(tmp_path):
    state = State(tmp_path / "site.json", "test-single")
    state.mark_phase("preflight", "done")
    state.mark_phase("base", "failed", {"error": "boom"})
    rows = {r["name"]: r for r in phase_rows(pipeline_for("single"), state)}
    assert rows["preflight"]["status"] == "done"
    assert rows["preflight"]["updated_at"] != ""
    assert rows["base"]["status"] == "failed"
    assert rows["base"]["error"] == "boom"
    assert rows["authentik"]["status"] == "pending"  # untouched phase stays pending


def test_phase_rows_hides_stale_error_on_done_phase(tmp_path):
    # a pre-2.9.2 state file: failed attempt, then done without clearing
    state = State(tmp_path / "site.json", "test-single")
    state.data["phases"]["nginx"] = {"status": "done", "updated_at": "x",
                                     "error": "certbot failed"}
    rows = {r["name"]: r for r in phase_rows(pipeline_for("single"), state)}
    assert rows["nginx"]["error"] == ""


def test_phase_rows_marks_optional_phases(tmp_path):
    state = State(tmp_path / "site.json", "test-single")
    rows = {r["name"]: r for r in phase_rows(pipeline_for("single"), state)}
    assert rows["restore"]["optional"] is True
    assert rows["preflight"]["optional"] is False


# --- cmd_status ---------------------------------------------------------------

def test_status_no_state_file_yet(write_config, capsys):
    config_path = write_config()
    rc = cmd_status(SimpleNamespace(config=str(config_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "doesn't exist yet" in out
    for name in SINGLE_PHASE_NAMES:
        assert name in out
    assert "pending" in out


def test_status_reflects_progress_and_names_next_phase(write_config, capsys, tmp_path):
    config_path = write_config()
    state = State(tmp_path / "site.json", "test-single")
    state.mark_phase("preflight", "done")
    state.mark_phase("base", "done")
    state.mark_phase("authentik", "failed", {"error": "connection refused"})

    rc = cmd_status(SimpleNamespace(config=str(config_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "failed" in out
    assert "connection refused" in out
    assert "next up" in out
    assert "authentik" in out.split("next up")[1]  # the failed phase, not a later one


def test_status_all_done(write_config, capsys, tmp_path):
    config_path = write_config()
    state = State(tmp_path / "site.json", "test-single")
    for name in SINGLE_PHASE_NAMES:
        state.mark_phase(name, "done")

    rc = cmd_status(SimpleNamespace(config=str(config_path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "all phases done" in out


def test_status_bad_config_reports_problems(tmp_path, capsys):
    bad_path = tmp_path / "bad.yml"
    bad_path.write_text(yaml.safe_dump({"site": {"name": "x"}}))
    rc = cmd_status(SimpleNamespace(config=str(bad_path)))
    assert rc == 2
    assert "config problems" in capsys.readouterr().out


# --- encrypted state: status without a passphrase, `state` subcommand ----------

PW = "correct horse battery"


@pytest.fixture
def pinned_site(write_config, tmp_path, monkeypatch):
    """A site whose state holds one pinned secret, encrypted with PW."""
    monkeypatch.setattr(state_mod, "SCRYPT_N", 2 ** 10)
    monkeypatch.delenv(state_mod.PASSPHRASE_ENV, raising=False)
    config_path = write_config()
    st = State(tmp_path / "site.json", "test-single", PW)
    st.unlock()
    st.get_or_generate("smtp_password", lambda: "old-smtp-pw")
    st.mark_phase("preflight", "done")
    return config_path, tmp_path / "site.json"


def _state_args(config_path, action, key=None):
    return SimpleNamespace(config=str(config_path), action=action, key=key,
                           passphrase_file=None)


def test_status_works_without_passphrase(pinned_site, capsys):
    config_path, _ = pinned_site
    assert cmd_status(SimpleNamespace(config=str(config_path))) == 0
    out = capsys.readouterr().out
    assert "pinned values (encrypted): smtp_password" in out
    assert "old-smtp-pw" not in out


def test_status_warns_on_plaintext_file(write_config, tmp_path, capsys):
    config_path = write_config()
    (tmp_path / "site.json").write_text(json.dumps(
        {"site": "test-single", "phases": {}, "generated": {"k": "plain"}}))
    assert cmd_status(SimpleNamespace(config=str(config_path))) == 0
    assert "state encrypt" in capsys.readouterr().out


def test_state_encrypt_migrates_plaintext(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "SCRYPT_N", 2 ** 10)
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    config_path = write_config()
    sf = tmp_path / "site.json"
    sf.write_text(json.dumps({"site": "test-single", "phases": {},
                              "generated": {"k": "plain-secret"}}))
    assert cmd_state(_state_args(config_path, "encrypt")) == 0
    assert "plain-secret" not in sf.read_text()
    st = State(sf, "test-single", PW)
    st.unlock()
    assert st.data["generated"] == {"k": "plain-secret"}


def test_state_get_and_set(pinned_site, monkeypatch, capsys):
    config_path, sf = pinned_site
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": "new-smtp-pw")
    assert cmd_state(_state_args(config_path, "set", "smtp_password")) == 0
    capsys.readouterr()
    assert cmd_state(_state_args(config_path, "get", "smtp_password")) == 0
    assert capsys.readouterr().out.strip() == "new-smtp-pw"
    assert "new-smtp-pw" not in sf.read_text()


def test_state_unknown_key(pinned_site, monkeypatch):
    config_path, _ = pinned_site
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, PW)
    assert cmd_state(_state_args(config_path, "get", "nope")) == 2


def test_state_wrong_passphrase(pinned_site, monkeypatch, capsys):
    config_path, _ = pinned_site
    monkeypatch.setenv(state_mod.PASSPHRASE_ENV, "wrong passphrase!!")
    assert cmd_state(_state_args(config_path, "get", "smtp_password")) == 2
    assert "wrong passphrase" in capsys.readouterr().out
