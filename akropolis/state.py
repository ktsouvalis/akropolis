"""Per-site provisioning state.

Records which phases completed (with timestamps and rendered-config checksums)
and pins values that must be generated exactly once per cluster lifetime
(etcd initial-cluster token, Authentik secret key, ...). Re-running a completed
phase must be a no-op or an explicit diff — never a re-bootstrap.

Secrets at rest: everything under `generated` is encrypted with a key derived
from a passphrase (scrypt → AES-256-GCM). Only `site`, `tool`, `phases` and
the *names* of the pinned values stay readable, so `akro status` works without
the passphrase. Nothing on disk can decrypt the file: a copy of the working
directory (a VM backup, a `clean` archive, a stray `.bak`) gives away no secret.

    {"tool": "akropolis", "site": ..., "format": 2, "phases": {...},
     "generated_keys": [...],
     "generated_enc": {"kdf": "scrypt", "n": ..., "r": ..., "p": ...,
                       "salt": b64, "nonce": b64, "ct": b64}}

The AES-GCM associated data is `tool|site|format`, so a blob pasted into
another site's file fails to decrypt instead of silently loading. A file with
no `generated_enc` has nothing pinned yet. A file without `format` is the
old plaintext layout (format 1): it is read as-is and written back encrypted
on the first save after `unlock()`.

A command that needs the pinned values calls `unlock()` once, up front;
`save()` never writes a plaintext secret and never prompts on its own.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import time
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

TOOL = "akropolis"
FORMAT = 2
PASSPHRASE_ENV = "AKRO_STATE_PASSPHRASE"
MIN_PASSPHRASE = 12

# scrypt cost: ~0.4 s and 128 MiB once per run on a small VM
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1

# PassphraseSource(new) -> passphrase. `new` is True when a passphrase is
# being chosen (no encrypted secrets yet, or `state passwd`), so an interactive
# source should ask twice.
PassphraseSource = Callable[[bool], str]


class StateLocked(RuntimeError):
    """The pinned values are encrypted and `unlock()` was not called."""


class _Locked(dict):
    """Stands in for `generated` until `unlock()`: any read or write fails
    loudly instead of looking like "nothing pinned yet" and regenerating."""

    def _refuse(self, *_a, **_kw):
        raise StateLocked("state is locked: the pinned values are encrypted; "
                          "the command must call State.unlock() first")

    __getitem__ = __setitem__ = __delitem__ = __contains__ = __iter__ = _refuse
    get = setdefault = pop = keys = values = items = update = _refuse


def passphrase_source(passphrase_file: str | Path | None = None,
                      site_name: str = "") -> PassphraseSource:
    """Env var, then `--passphrase-file`, then an interactive prompt."""

    def get(new: bool) -> str:
        env = os.environ.get(PASSPHRASE_ENV)
        if env:
            return env
        if passphrase_file:
            value = Path(passphrase_file).read_text().rstrip("\r\n")
            if not value:
                raise StateLocked(f"passphrase file {passphrase_file} is empty")
            return value
        label = f" for site {site_name}" if site_name else ""
        if not new:
            return getpass.getpass(f"state passphrase{label}: ")
        print(f"Choose a passphrase{label}. It encrypts every pinned secret in the "
              "state file; nothing on disk can recover it, so store it in your "
              "password manager now.")
        while True:
            first = getpass.getpass(f"new state passphrase{label}: ")
            if len(first) < MIN_PASSPHRASE:
                print(f"at least {MIN_PASSPHRASE} characters, please")
                continue
            if getpass.getpass("repeat it: ") == first:
                return first
            print("they don't match, try again")

    return get


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class State:
    def __init__(self, path: Path, site_name: str,
                 passphrase: PassphraseSource | str | None = None):
        self.path = Path(path)
        self.site_name = site_name
        if isinstance(passphrase, str):
            fixed = passphrase
            passphrase = lambda _new: fixed  # noqa: E731
        self._passphrase = passphrase
        self._key: bytes | None = None
        self._kdf: dict | None = None  # salt + scrypt params of the current key
        self.data: dict = {"tool": TOOL, "site": site_name, "phases": {}, "generated": {}}
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
            tool = self.data.get("tool")
            if tool is not None and tool != TOOL:
                raise RuntimeError(
                    f"State file {self.path} was written by {tool!r}, not akropolis. "
                    "Refusing to read or overwrite another tool's state.")
            if self.data.get("site") != site_name:
                raise RuntimeError(
                    f"State file {self.path} belongs to site {self.data.get('site')!r}, "
                    f"not {site_name!r}. Refusing to mix state between sites."
                )
            self.data["tool"] = TOOL
            if self.data.get("generated_enc"):
                self.data["generated"] = _Locked()
            else:
                self.data.setdefault("generated", {})

    # --- encryption ---------------------------------------------------------
    @property
    def encrypted(self) -> bool:
        """True when the file on disk holds its pinned values encrypted."""
        return bool(self.data.get("generated_enc"))

    @property
    def plaintext_on_disk(self) -> bool:
        """True for an old (format 1) file that still holds secrets in clear."""
        return (self.path.exists() and not self.encrypted
                and bool(self.data.get("generated")))

    @property
    def locked(self) -> bool:
        return isinstance(self.data.get("generated"), _Locked)

    def generated_keys(self) -> list[str]:
        """Names of the pinned values; readable without the passphrase."""
        if self.locked:
            return list(self.data.get("generated_keys", []))
        return sorted(self.data["generated"])

    def _aad(self) -> bytes:
        return f"{TOOL}|{self.site_name}|{FORMAT}".encode()

    @staticmethod
    def _derive(passphrase: str, kdf: dict) -> bytes:
        return Scrypt(salt=_unb64(kdf["salt"]), length=32, n=kdf["n"],
                      r=kdf["r"], p=kdf["p"]).derive(passphrase.encode())

    def _source(self) -> PassphraseSource:
        if self._passphrase is None:
            self._passphrase = passphrase_source(site_name=self.site_name)
        return self._passphrase

    def _new_key(self, passphrase: str) -> None:
        self._kdf = {"kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                     "salt": _b64(os.urandom(16))}
        self._key = self._derive(passphrase, self._kdf)

    def unlock(self) -> None:
        """Make `generated` readable and writable for this run.

        Encrypted file: asks for its passphrase and decrypts. New file, empty
        file or old plaintext file: asks for a new passphrase, which the next
        save encrypts with. Idempotent."""
        if self._key is not None:
            return
        if not self.encrypted:
            self._new_key(self._source()(True))
            return
        blob = self.data["generated_enc"]
        if blob.get("kdf") != "scrypt":
            raise StateLocked(f"State file {self.path}: unknown kdf {blob.get('kdf')!r}")
        kdf = {k: blob[k] for k in ("kdf", "n", "r", "p", "salt")}
        key = self._derive(self._source()(False), kdf)
        try:
            plain = AESGCM(key).decrypt(_unb64(blob["nonce"]), _unb64(blob["ct"]), self._aad())
        except InvalidTag:
            raise StateLocked(
                f"wrong passphrase for {self.path} (or the file was altered, or "
                "copied from another site)") from None
        self._key, self._kdf = key, kdf
        self.data["generated"] = json.loads(plain)

    def change_passphrase(self) -> None:
        """Re-encrypt with a new passphrase (and a new salt)."""
        self.unlock()
        self._new_key(self._source()(True))
        self.save()

    # --- phases -----------------------------------------------------------
    def phase_status(self, name: str) -> str:
        return self.data["phases"].get(name, {}).get("status", "pending")

    def mark_phase(self, name: str, status: str, detail: dict | None = None) -> None:
        entry = self.data["phases"].setdefault(name, {})
        # an error describes the attempt that recorded it; a later mark
        # (done, replay → pending, a verify failure) must not inherit it
        entry.pop("error", None)
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
    def _serialise(self) -> dict:
        out = {k: v for k, v in self.data.items()
               if k not in ("generated", "generated_keys", "generated_enc")}
        out["format"] = FORMAT
        if self.locked:
            # never unlocked this run: carry the blob over untouched
            out["generated_keys"] = self.data.get("generated_keys", [])
            out["generated_enc"] = self.data["generated_enc"]
            return out
        generated = self.data["generated"]
        out["generated_keys"] = sorted(generated)
        if not generated and self._key is None:
            return out  # nothing pinned yet: no blob, no passphrase needed
        if self._key is None:
            raise StateLocked(
                f"refusing to write pinned secrets to {self.path} in plain text: "
                "the command must call State.unlock() first")
        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, json.dumps(generated).encode(), self._aad())
        out["generated_enc"] = {**self._kdf, "nonce": _b64(nonce), "ct": _b64(ct)}
        return out

    def save(self) -> None:
        out = self._serialise()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # created owner-only: the file is never readable by others, not even
        # between write and rename
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
        os.chmod(tmp, 0o600)  # O_CREAT's mode doesn't apply to an existing .tmp
        tmp.replace(self.path)
        if "generated_enc" in out:
            self.data["generated_enc"] = out["generated_enc"]
        self.data["generated_keys"] = out["generated_keys"]
