"""Third-party license manifest — shared by `akropolis licenses` (reads
whatever is actually importable right now, pip install or running zipapp
alike) and tools/build_pyz.sh (reads a pre-zip build directory), so the
row/render logic lives in one place instead of two copies drifting apart.
"""

from __future__ import annotations

import pathlib
import re
from importlib import metadata

_LICENSE_FILE_RE = re.compile(r"(?i)^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)")

Row = tuple[str, str, str, list[str]]  # name, version, license, license file paths


def declared_license(meta_text: str) -> str:
    """`meta_text` is a dist-info METADATA file's raw contents (only the
    header block matters; a package that stuffs its whole README into a
    single-line `Description:` header, as paramiko does, can otherwise make
    this look like it needs full RFC 5322 parsing — it doesn't)."""
    m = re.search(r"^License-Expression:\s*(.+)$", meta_text, re.M)
    if m:
        return m.group(1).strip()
    m = re.search(r"^License:\s*(.+)$", meta_text, re.M)
    if m and m.group(1).strip() and m.group(1).strip().upper() != "UNKNOWN":
        return m.group(1).strip()
    m = re.search(r"^Classifier:\s*License :: OSI Approved :: (.+)$", meta_text, re.M)
    if m:
        return m.group(1).strip()
    return "unknown -- see embedded license file"


def rows_from_dist_info_dir(root: pathlib.Path) -> list[Row]:
    """Scan a plain directory of `*.dist-info` folders (a pip --target build,
    before it's zipped up)."""
    rows = []
    for d in sorted(root.glob("*.dist-info")):
        name, _, version = d.name[: -len(".dist-info")].rpartition("-")
        if name.lower() == "akropolis":
            continue
        meta_path = d / "METADATA"
        meta = meta_path.read_text(errors="replace") if meta_path.exists() else ""
        lic_files = sorted(
            str(p.relative_to(root))
            for p in d.rglob("*")
            if p.is_file() and _LICENSE_FILE_RE.match(p.name)
        )
        rows.append((name, version, declared_license(meta), lic_files))
    return rows


def rows_from_installed() -> list[Row]:
    """What's actually importable in the running interpreter right now —
    correct for both a pip install and a running zipapp, since
    importlib.metadata resolves distributions from sys.path either way."""
    by_name: dict[str, Row] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"] or ""
        if not name or name.lower() == "akropolis":
            continue
        lic_files = sorted(
            str(p) for p in (dist.files or []) if _LICENSE_FILE_RE.match(pathlib.Path(str(p)).name)
        )
        # dist.read_text() returns the raw METADATA file, not a parsed
        # email.message.Message -- str(dist.metadata) round-trips through
        # the latter and blows up on any package whose Description header
        # isn't properly folded (paramiko's, among others).
        meta_text = dist.read_text("METADATA") or ""
        by_name[name.lower()] = (name, dist.version or "", declared_license(meta_text), lic_files)
    return [by_name[k] for k in sorted(by_name)]


def render(rows: list[Row], *, note: str | None = None) -> str:
    lines = ["# Third-party licenses", ""]
    if note:
        lines += [note, ""]
    lines += [
        "| Package | Version | License | License file(s) |",
        "| :--- | :--- | :--- | :--- |",
    ]
    for name, version, lic, lic_files in rows:
        paths = "<br>".join(f"`{f}`" for f in lic_files) if lic_files else "*(none shipped by upstream)*"
        lines.append(f"| {name} | {version} | {lic} | {paths} |")
    return "\n".join(lines) + "\n"


def report() -> str:
    note = (
        "Listing what's actually importable in this akropolis right now — "
        "installed dependencies for a pip install, or the packages bundled "
        "inside the archive for a zipapp binary. `psycopg2`, `cryptography`, "
        "`bcrypt` and `nacl` are supplied by the system rather than bundled "
        "in the zipapp (see README) and so may be absent from this list "
        "there even though akropolis depends on them."
    )
    return render(rows_from_installed(), note=note)
