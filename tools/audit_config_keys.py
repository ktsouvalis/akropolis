#!/usr/bin/env python3
"""Fail if the code reads a site-config key that config.example.yml never mentions.

The example file is the only place most operators will ever look for the set of
available settings: `base.apt_upgrade` and `network.trusted_proxies` were both
documented in the README and implemented in code, yet absent from the example,
so in practice they did not exist. A README paragraph is not discovery.

Run from the repo root:

    python3 tools/audit_config_keys.py

Exits non-zero and lists the offenders when a key is undocumented, so it can be
wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Keys read through a helper that hides the section name, mapped to their section.
HELPER_SECTIONS = {
    "_acfg": "authentik",
    "_rcfg": "restore",
}


def collect_keys() -> set[str]:
    keys: set[str] = set()
    for path in (ROOT / "akropolis").rglob("*.py"):
        src = path.read_text()
        # _get(raw, "a.b.c")
        keys.update(m.group(1) for m in re.finditer(r'_get\(raw,\s*"([a-z_.]+)"', src))
        # raw.get("section") ... .get("key")
        for pattern in (r'raw\.get\("([a-z_]+)"\)[^\n]*?\)\.get\("([a-z_]+)"',
                        r'raw\.get\("([a-z_]+)"\)\s*or\s*\{\}\)\.get\("([a-z_]+)"'):
            keys.update(f"{m.group(1)}.{m.group(2)}" for m in re.finditer(pattern, src))
        # self._acfg(ctx).get("key") / self._rcfg(ctx).get("key")
        for helper, section in HELPER_SECTIONS.items():
            keys.update(f"{section}.{m.group(1)}"
                        for m in re.finditer(rf'{helper}\(ctx\)\.get\("([a-z_]+)"', src))
    return keys


def main() -> int:
    example = (ROOT / "config.example.yml").read_text()
    # Presence of the leaf name is enough: the example carries most keys
    # commented out, so a YAML parse would not see them.
    missing = sorted(k for k in collect_keys() if k.split(".")[-1] not in example)
    if missing:
        print("Config keys read by the code but absent from config.example.yml:")
        for k in missing:
            print(f"  ✘ {k}")
        print("\nDocument them (commented out is fine) so operators can find them.")
        return 1
    print(f"OK — every config key the code reads appears in config.example.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
