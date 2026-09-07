"""Per-site provisioning state.

Records which phases completed (with timestamps and rendered-config checksums)
and pins values that must be generated exactly once per cluster lifetime
(etcd initial-cluster token, Authentik secret key, ...). Re-running a completed
phase must be a no-op or an explicit diff — never a re-bootstrap.

TODO(security): generated secrets are stored plaintext in the state file for
now. Before any production use, wrap `generated` in age/sops encryption or
move it to the OS keyring. Tracked as a v0.2 requirement.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class State:
    def __init__(self, path: Path, site_name: str):
        self.path = Path(path)
        self.site_name = site_name
        self.data: dict = {"site": site_name, "phases": {}, "generated": {}}
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
            if self.data.get("site") != site_name:
                raise RuntimeError(
                    f"State file {self.path} belongs to site {self.data.get('site')!r}, "
                    f"not {site_name!r}. Refusing to mix state between sites."
                )

    # --- phases -----------------------------------------------------------
    def phase_status(self, name: str) -> str:
        return self.data["phases"].get(name, {}).get("status", "pending")

    def mark_phase(self, name: str, status: str, detail: dict | None = None) -> None:
        entry = self.data["phases"].setdefault(name, {})
        entry["status"] = status
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if detail:
            entry.update(detail)
        self.save()

    # --- generated-once values -------------------------------------------
    def get_or_generate(self, key: str, generator) -> str:
        """Return the pinned value for `key`, generating and pinning it on first use."""
        if key not in self.data["generated"]:
            self.data["generated"][key] = generator()
            self.save()
        return self.data["generated"][key]

    # --- io ---------------------------------------------------------------
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        tmp.replace(self.path)
        # state may contain pinned secrets — owner-only
        self.path.chmod(0o600)
