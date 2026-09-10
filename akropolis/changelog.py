"""Read and slice the shipped CHANGELOG.md for `akropolis whats-new`.

CHANGELOG.md lives at the repo root (where GitHub renders it and where the
config-version error message already points operators); `akropolis/CHANGELOG.md`
is a symlink to it so the file travels inside the installed package and the
zipapp (see package-data in pyproject.toml) without keeping two copies in sync.
"""

from __future__ import annotations

import re
from importlib import resources

_SECTION_RE = re.compile(r"^## \[(?P<version>[^\]]+)\].*$", re.MULTILINE)


def load() -> str:
    return resources.files("akropolis").joinpath("CHANGELOG.md").read_text()


def sections(text: str) -> list[tuple[str, str]]:
    """Split into (header_line, body) pairs, one per `## [...]` section, in file order."""
    matches = list(_SECTION_RE.finditer(text))
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(0), text[m.end():end].strip("\n")))
    return out


def entry_for(version: str, text: str | None = None) -> str | None:
    """The changelog section for exactly `version`, or None if it isn't listed."""
    if text is None:
        text = load()
    for header, body in sections(text):
        m = _SECTION_RE.match(header)
        if m and m.group("version") == version:
            return f"{header}\n\n{body}" if body else header
    return None
