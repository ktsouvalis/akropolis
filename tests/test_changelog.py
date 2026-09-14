"""Tests for akropolis.changelog: CHANGELOG.md parsing for `whats-new`."""

from __future__ import annotations

from akropolis import changelog

SAMPLE = """\
# Changelog

Some preamble text that isn't part of any section.

## [Unreleased]

## [2.2.3] - 2026-09-14

### Fixed

- fixed thing one
- fixed thing two

## [2.2.2] - 2026-09-14

### Fixed

- fixed thing three
"""


def test_sections_splits_in_file_order():
    secs = changelog.sections(SAMPLE)
    headers = [h for h, _ in secs]
    assert headers == ["## [Unreleased]", "## [2.2.3] - 2026-09-14", "## [2.2.2] - 2026-09-14"]


def test_sections_body_excludes_header_and_next_header():
    secs = changelog.sections(SAMPLE)
    body = dict(secs)["## [2.2.3] - 2026-09-14"]
    assert "fixed thing one" in body
    assert "fixed thing two" in body
    assert "fixed thing three" not in body
    assert "## [2.2.2]" not in body


def test_sections_empty_body_for_unreleased():
    secs = changelog.sections(SAMPLE)
    assert dict(secs)["## [Unreleased]"] == ""


def test_entry_for_returns_header_and_body():
    entry = changelog.entry_for("2.2.3", SAMPLE)
    assert entry.startswith("## [2.2.3] - 2026-09-14")
    assert "fixed thing one" in entry


def test_entry_for_unknown_version_returns_none():
    assert changelog.entry_for("9.9.9", SAMPLE) is None


def test_entry_for_empty_section_returns_just_header():
    assert changelog.entry_for("Unreleased", SAMPLE) == "## [Unreleased]"


def test_entry_for_defaults_to_load(monkeypatch):
    monkeypatch.setattr(changelog, "load", lambda: SAMPLE)
    entry = changelog.entry_for("2.2.2")
    assert "fixed thing three" in entry


# --- the real shipped CHANGELOG.md, not the synthetic sample above --------

def test_real_changelog_loads_and_parses():
    text = changelog.load()
    secs = changelog.sections(text)
    assert len(secs) > 1
    # every header must match the `## [version] ...` shape sections() splits on
    assert all(h.startswith("## [") for h, _ in secs)


def test_real_changelog_current_version_has_entry():
    import importlib.metadata as md
    try:
        version = md.version("akropolis")
    except md.PackageNotFoundError:
        return  # package not installed in this environment -- nothing to check
    assert changelog.entry_for(version) is not None
