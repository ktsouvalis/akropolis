"""Tests for akropolis.config: load() validation and defaulting."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from akropolis import config as config_mod
from akropolis.config import CONFIG_SCHEMA_VERSION, ConfigError, load

HA_BASE = {
    "site": {
        "name": "test-site",
        "environment": "lab",
        "topology": "ha",
        "config_version": CONFIG_SCHEMA_VERSION,
    },
    "nodes": [
        {"name": "node1", "ip": "10.0.0.1", "bootstrap_leader": True},
        {"name": "node2", "ip": "10.0.0.2"},
        {"name": "node3", "ip": "10.0.0.3"},
    ],
    "ssh": {"user": "root", "auth": "key", "key_file": None},  # key_file filled in by fixture
    "network": {"vip": "10.0.0.10", "interface": "ens18"},
    "tls": {"provider": "self_signed", "hostname": "example.test"},
    "authentik": {"tag": "2026.5.6"},
}

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


def _deep_merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@pytest.fixture
def ssh_key(tmp_path):
    key = tmp_path / "id_rsa"
    key.write_text("fake key material")
    return str(key)


@pytest.fixture
def write_raw(tmp_path):
    """Write an arbitrary dict as-is (no key_file defaulting/merging)."""
    def _write(doc: dict):
        path = tmp_path / "config.yml"
        path.write_text(yaml.safe_dump(doc))
        return path

    return _write


@pytest.fixture
def write_config(tmp_path, ssh_key, write_raw):
    def _write(base: dict, overrides: dict | None = None):
        doc = _deep_merge(base, overrides or {})
        if doc.get("ssh", {}).get("key_file") is None:
            doc.setdefault("ssh", {})["key_file"] = ssh_key
        return write_raw(doc)

    return _write


# --- file / version handling ------------------------------------------------

def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.yml")


def test_missing_config_version_raises(write_raw, ssh_key):
    doc = copy.deepcopy(HA_BASE)
    doc["ssh"]["key_file"] = ssh_key
    del doc["site"]["config_version"]
    path = write_raw(doc)
    with pytest.raises(ConfigError, match="config_version is missing"):
        load(path)


def test_config_version_wrong_type_raises(write_config):
    path = write_config(HA_BASE, {"site": {"config_version": "two"}})
    with pytest.raises(ConfigError, match="non-negative integer"):
        load(path)


def test_config_version_bool_raises(write_config):
    # bool is a subclass of int in Python -- must be explicitly rejected
    path = write_config(HA_BASE, {"site": {"config_version": True}})
    with pytest.raises(ConfigError, match="non-negative integer"):
        load(path)


def test_config_version_too_old_raises(write_config):
    path = write_config(HA_BASE, {"site": {"config_version": CONFIG_SCHEMA_VERSION - 1}})
    with pytest.raises(ConfigError, match="CHANGELOG"):
        load(path)


def test_config_version_too_new_raises(write_config):
    path = write_config(HA_BASE, {"site": {"config_version": CONFIG_SCHEMA_VERSION + 1}})
    with pytest.raises(ConfigError, match="akropolis update"):
        load(path)


# --- happy path --------------------------------------------------------------

def test_valid_ha_config_loads(write_config):
    path = write_config(HA_BASE)
    cfg = load(path)
    assert cfg.name == "test-site"
    assert cfg.topology == "ha"
    assert len(cfg.nodes) == 3
    assert cfg.bootstrap_leader.name == "node1"
    assert cfg.authentik_tag == "2026.5.6"
    assert cfg.state_file == Path(f".state/{cfg.name}.json")


def test_valid_single_config_loads(write_config):
    path = write_config(SINGLE_BASE)
    cfg = load(path)
    assert cfg.topology == "single"
    assert len(cfg.nodes) == 1
    assert cfg.bootstrap_leader.name == "node1"  # only node defaults to leader
    # single topology's default authentik tag differs from HA's
    assert cfg.authentik_tag == config_mod.DEFAULT_AUTHENTIK_TAG["single"]


def test_raw_dict_preserved(write_config):
    path = write_config(HA_BASE)
    cfg = load(path)
    assert cfg.raw["site"]["name"] == "test-site"


def test_custom_state_file(write_config):
    path = write_config(HA_BASE, {"provision": {"state_file": "/tmp/custom-state.json"}})
    cfg = load(path)
    assert str(cfg.state_file) == "/tmp/custom-state.json"


# --- site block ---------------------------------------------------------

def test_missing_name_reported(write_raw, ssh_key):
    doc = copy.deepcopy(HA_BASE)
    doc["ssh"]["key_file"] = ssh_key
    del doc["site"]["name"]
    path = write_raw(doc)
    with pytest.raises(ConfigError, match="site.name is required"):
        load(path)


def test_invalid_environment_reported(write_config):
    path = write_config(HA_BASE, {"site": {"environment": "staging"}})
    with pytest.raises(ConfigError, match="site.environment"):
        load(path)


def test_invalid_topology_reported(write_config):
    path = write_config(HA_BASE, {"site": {"topology": "mesh"}})
    with pytest.raises(ConfigError, match="site.topology"):
        load(path)


# --- nodes ----------------------------------------------------------------

def test_wrong_node_count_for_topology(write_config):
    doc = copy.deepcopy(HA_BASE)
    doc["nodes"] = doc["nodes"][:2]
    path = write_config(doc)
    with pytest.raises(ConfigError, match="requires exactly 3"):
        load(path)


def test_invalid_node_ip_reported(write_config):
    doc = copy.deepcopy(HA_BASE)
    doc["nodes"][0]["ip"] = "not-an-ip"
    path = write_config(doc)
    with pytest.raises(ConfigError, match="invalid or placeholder IP"):
        load(path)


def test_duplicate_node_ip_reported(write_config):
    doc = copy.deepcopy(HA_BASE)
    doc["nodes"][1]["ip"] = doc["nodes"][0]["ip"]
    path = write_config(doc)
    with pytest.raises(ConfigError, match="duplicate IP"):
        load(path)


def test_no_bootstrap_leader_defaults_to_first(write_config):
    doc = copy.deepcopy(HA_BASE)
    for n in doc["nodes"]:
        n.pop("bootstrap_leader", None)
    path = write_config(doc)
    cfg = load(path)
    assert cfg.bootstrap_leader.name == "node1"


def test_multiple_bootstrap_leaders_reported(write_config):
    doc = copy.deepcopy(HA_BASE)
    doc["nodes"][1]["bootstrap_leader"] = True
    path = write_config(doc)
    with pytest.raises(ConfigError, match="exactly one node"):
        load(path)


# --- ssh --------------------------------------------------------------------

def test_invalid_ssh_auth_reported(write_config):
    path = write_config(HA_BASE, {"ssh": {"auth": "carrier-pigeon"}})
    with pytest.raises(ConfigError, match="ssh.auth"):
        load(path)


def test_key_auth_without_key_file_reported(write_raw):
    doc = copy.deepcopy(HA_BASE)
    del doc["ssh"]["key_file"]
    path = write_raw(doc)
    with pytest.raises(ConfigError, match="ssh.key_file is not set"):
        load(path)


def test_key_auth_nonexistent_key_file_reported(write_config):
    path = write_config(HA_BASE, {"ssh": {"key_file": "/no/such/key"}})
    with pytest.raises(ConfigError, match="does not exist"):
        load(path)


def test_password_auth_does_not_require_key_file(write_raw):
    doc = copy.deepcopy(HA_BASE)
    doc["ssh"]["auth"] = "password"
    del doc["ssh"]["key_file"]
    path = write_raw(doc)
    cfg = load(path)
    assert cfg.ssh.auth == "password"


# --- network / VIP -----------------------------------------------------------

def test_vip_outside_node_subnet_reported(write_config):
    path = write_config(HA_BASE, {"network": {"vip": "10.1.0.10"}})
    with pytest.raises(ConfigError, match="not in the same /24"):
        load(path)


def test_vip_collides_with_node_reported(write_config):
    path = write_config(HA_BASE, {"network": {"vip": "10.0.0.1"}})
    with pytest.raises(ConfigError, match="collides with a node IP"):
        load(path)


def test_invalid_vip_reported(write_config):
    path = write_config(HA_BASE, {"network": {"vip": "not-an-ip"}})
    with pytest.raises(ConfigError, match="invalid or placeholder IP"):
        load(path)


def test_single_topology_skips_vip_validation(write_config):
    doc = copy.deepcopy(SINGLE_BASE)
    doc["network"] = {"vip": "garbage"}
    path = write_config(doc)
    cfg = load(path)  # must not raise -- VIP is irrelevant to single topology
    assert cfg.topology == "single"


# --- tls ----------------------------------------------------------------

def test_invalid_tls_provider_reported(write_config):
    path = write_config(HA_BASE, {"tls": {"provider": "carrier-pigeon"}})
    with pytest.raises(ConfigError, match="tls.provider"):
        load(path)


def test_tls_none_refused_in_production(write_config):
    path = write_config(HA_BASE, {
        "site": {"environment": "production"},
        "tls": {"provider": "none"},
    })
    with pytest.raises(ConfigError, match="refused when site.environment is 'production'"):
        load(path)


def test_tls_none_allowed_in_lab(write_config):
    path = write_config(HA_BASE, {"tls": {"provider": "none"}})
    cfg = load(path)
    assert cfg.tls.provider == "none"


def test_tls_hostname_required_for_self_signed_ha(write_config):
    doc = copy.deepcopy(HA_BASE)
    del doc["tls"]["hostname"]
    path = write_config(doc)
    with pytest.raises(ConfigError, match="tls.hostname is required"):
        load(path)


def test_tls_hostname_optional_for_single_self_signed(write_config):
    doc = copy.deepcopy(SINGLE_BASE)
    path = write_config(doc)
    cfg = load(path)  # single + self_signed: hostname optional, must not raise
    assert cfg.tls.hostname == ""


def test_tls_acme_requires_directory_url_and_email(write_config):
    path = write_config(HA_BASE, {"tls": {"provider": "acme", "hostname": "x.test", "acme": {}}})
    with pytest.raises(ConfigError) as exc:
        load(path)
    msg = "\n".join(exc.value.problems)
    assert "tls.acme.directory_url" in msg
    assert "tls.acme.email" in msg


def test_tls_acme_valid(write_config):
    path = write_config(HA_BASE, {
        "tls": {
            "provider": "acme",
            "hostname": "x.test",
            "acme": {"directory_url": "https://acme.example/directory", "email": "ops@example.test"},
        }
    })
    cfg = load(path)
    assert cfg.tls.provider == "acme"


def test_tls_import_requires_existing_files(write_config, tmp_path):
    fullchain = tmp_path / "fullchain.pem"
    fullchain.write_text("cert")
    path = write_config(HA_BASE, {
        "tls": {
            "provider": "import",
            "hostname": "x.test",
            "import": {"fullchain": str(fullchain), "privkey": str(tmp_path / "missing.pem")},
        }
    })
    with pytest.raises(ConfigError, match="privkey does not exist"):
        load(path)


def test_tls_import_valid(write_config, tmp_path):
    fullchain = tmp_path / "fullchain.pem"
    privkey = tmp_path / "privkey.pem"
    fullchain.write_text("cert")
    privkey.write_text("key")
    path = write_config(HA_BASE, {
        "tls": {
            "provider": "import",
            "hostname": "x.test",
            "import": {"fullchain": str(fullchain), "privkey": str(privkey)},
        }
    })
    cfg = load(path)
    assert cfg.tls.provider == "import"


# --- authentik.tag ------------------------------------------------------

def test_unquoted_authentik_tag_reported(tmp_path, ssh_key):
    # Simulate `tag: 2026.10` parsed by YAML as a float -- write raw YAML
    # text directly since a Python dict would need an actual float literal
    # to trigger it, which round-trips differently through yaml.safe_dump.
    doc = copy.deepcopy(HA_BASE)
    doc["ssh"]["key_file"] = ssh_key
    text = yaml.safe_dump(doc).replace("tag: 2026.5.6\n", "tag: 2026.10\n")
    path = tmp_path / "config.yml"
    path.write_text(text)
    with pytest.raises(ConfigError, match="authentik.tag must be quoted"):
        load(path)


def test_default_authentik_tag_by_topology(write_config):
    doc = copy.deepcopy(HA_BASE)
    del doc["authentik"]
    path = write_config(doc)
    cfg = load(path)
    assert cfg.authentik_tag == config_mod.DEFAULT_AUTHENTIK_TAG["ha"]


# --- monitor (optional) --------------------------------------------------

def test_invalid_monitor_ip_reported(write_config):
    path = write_config(HA_BASE, {"monitor": {"ip": "garbage"}})
    with pytest.raises(ConfigError, match="monitor.ip: invalid IP"):
        load(path)


def test_monitor_ip_colliding_with_node_reported(write_config):
    path = write_config(HA_BASE, {"monitor": {"ip": "10.0.0.1"}})
    with pytest.raises(ConfigError, match="monitor.ip 10.0.0.1 collides"):
        load(path)


def test_monitor_ip_optional(write_config):
    path = write_config(HA_BASE)
    cfg = load(path)  # no monitor block at all -- must not raise
    assert cfg.name == "test-site"


# --- multiple problems reported together --------------------------------

def test_multiple_problems_all_reported_together(write_config):
    doc = copy.deepcopy(HA_BASE)
    doc["site"]["environment"] = "bogus"
    doc["nodes"][0]["ip"] = "not-an-ip"
    path = write_config(doc)
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert len(exc.value.problems) >= 2
