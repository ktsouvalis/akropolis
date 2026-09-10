"""Self-update: check GitHub releases for a newer akropolis and replace the
running binary in place.

Only the single-file zipapp published on GitHub releases can update itself
this way (see tools/build_pyz.sh) -- a pip/source install has its own update
path (git pull / pip install -U) and is left alone.

The version check (`check_for_update`) is cheap and safe to call on every
invocation: it is cached for CHECK_INTERVAL_SECONDS and swallows every
network/parsing error, since a flaky connection or GitHub outage must never
break an unrelated `akropolis provision` run.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from rich.console import Console

REPO = "ktsouvalis/akropolis"
API_LATEST_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "akropolis-cli"

CACHE_PATH = Path.home() / ".cache" / "akropolis" / "update_check.json"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
CHECK_TIMEOUT_SECONDS = 3
DOWNLOAD_TIMEOUT_SECONDS = 30

console = Console()


def _is_newer(candidate: str, current: str) -> bool:
    try:
        candidate_t = tuple(int(p) for p in candidate.split("."))
        current_t = tuple(int(p) for p in current.split("."))
    except ValueError:
        return False
    return candidate_t > current_t


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001 -- missing/corrupt cache is not an error
        return {}


def _write_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data))
    except Exception:  # noqa: BLE001 -- caching is best-effort
        pass


def _fetch_latest_release(timeout: float) -> dict | None:
    req = urllib.request.Request(
        API_LATEST_RELEASE,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001 -- offline/rate-limited/DNS/etc. must not break the CLI
        return None


def check_for_update(current_version: str) -> str | None:
    """Return the latest release version if it's newer than `current_version`,
    else None. Hits the network at most once per CHECK_INTERVAL_SECONDS."""
    cache = _read_cache()
    now = time.time()
    if now - cache.get("last_checked", 0) < CHECK_INTERVAL_SECONDS:
        latest = cache.get("latest")
    else:
        release = _fetch_latest_release(CHECK_TIMEOUT_SECONDS)
        latest = release.get("tag_name", "").lstrip("v") if release else None
        # A failed probe keeps whatever was last known rather than losing the
        # notice, but still stamps last_checked so we don't retry every run.
        _write_cache({"last_checked": now, "latest": latest or cache.get("latest")})
        latest = latest or cache.get("latest")

    if latest and _is_newer(latest, current_version):
        return latest
    return None


def _running_executable_path() -> Path | None:
    path = Path(sys.argv[0]).resolve()
    return path if path.is_file() else None


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
        return resp.read()


def check_update_now(current_version: str) -> int:
    """Force a fresh check against GitHub and report the result unconditionally.

    Unlike check_for_update() -- which is cached, throttled, and silent when
    already current -- this always hits the network (bypassing the cache) and
    always prints a definite answer. Used by the `check-update` subcommand,
    including from scripts/cron via the exit code: 0 up to date, 1 an update
    is available, 2 the check itself failed (network/parse error).
    """
    console.print("checking latest release…")
    release = _fetch_latest_release(CHECK_TIMEOUT_SECONDS)
    if release is None:
        console.print("[red]could not reach GitHub to check the latest release.[/red]")
        return 2

    latest = release.get("tag_name", "").lstrip("v")
    if not latest:
        console.print("[red]unexpected response from GitHub — no tag_name on the latest release.[/red]")
        return 2

    _write_cache({"last_checked": time.time(), "latest": latest})

    if _is_newer(latest, current_version):
        console.print(
            f"[yellow]a new akropolis release is available: "
            f"{current_version} → {latest}[/yellow] [dim](run `akropolis update`)[/dim]"
        )
        return 1

    console.print(f"already up to date (akropolis {current_version}).")
    return 0


def self_update(current_version: str) -> int:
    exe_path = _running_executable_path()
    if exe_path is None or not zipfile.is_zipfile(exe_path):
        console.print(
            "[yellow]akropolis update only supports the release zipapp binary "
            "(the single `akropolis` file from GitHub releases).[/yellow]"
        )
        console.print(
            "This looks like a source/pip install -- use `git pull` or "
            "`pip install -U akropolis` instead."
        )
        return 1

    console.print("checking latest release…")
    release = _fetch_latest_release(CHECK_TIMEOUT_SECONDS)
    if release is None:
        console.print("[red]could not reach GitHub to check the latest release.[/red]")
        return 1

    latest = release.get("tag_name", "").lstrip("v")
    if not latest:
        console.print("[red]unexpected response from GitHub — no tag_name on the latest release.[/red]")
        return 1

    if not _is_newer(latest, current_version):
        console.print(f"already up to date (akropolis {current_version}).")
        return 0

    assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets", [])}
    binary_url = assets.get("akropolis")
    sums_url = assets.get("SHA256SUMS")
    if not binary_url or not sums_url:
        console.print(f"[red]release {latest} is missing the `akropolis` binary or SHA256SUMS asset.[/red]")
        return 1

    console.print(f"downloading akropolis {latest}…")
    try:
        binary_data = _download(binary_url)
        sums_text = _download(sums_url).decode()
    except urllib.error.URLError as exc:
        console.print(f"[red]download failed:[/red] {exc}")
        return 1

    expected = None
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "akropolis":
            expected = parts[0]
            break
    if expected is None:
        console.print("[red]SHA256SUMS does not list the `akropolis` binary — refusing to install.[/red]")
        return 1

    actual = hashlib.sha256(binary_data).hexdigest()
    if actual != expected:
        console.print(
            f"[red]checksum mismatch[/red] (expected {expected}, got {actual}) — refusing to install."
        )
        return 1

    mode = exe_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    fd, tmp_path_str = tempfile.mkstemp(dir=exe_path.parent, prefix=".akropolis-update-")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(binary_data)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, exe_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    console.print(f"[green]updated akropolis {current_version} → {latest}[/green] ({exe_path})")
    return 0
