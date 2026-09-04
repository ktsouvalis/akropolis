"""transcript — a full, timestamped record of every command run on every
node during a `provision` or `clean` invocation, plus its output.

Exists so an operator (or an auditor) can reconstruct exactly what akropolis
did to a machine without re-reading the source or trusting memory of what
happened months ago. One file per invocation, named after the site, the
command, and the run's start time, so successive runs never overwrite each
other's record. Lives on the WORKSTATION only — nothing here is written to
a node.

Wiring: NodeConn.run() (sshexec.py) is the single choke point every remote
command already passes through, so that is where every command/output pair
is captured — no phase has to opt in or remember to log anything itself.

Secrets — what IS covered and what is NOT:
Commands routinely embed the very secrets akropolis generates: bootstrap
tokens in `curl -H 'Authorization: Bearer ...'`, DB/SMTP passwords in
KEY=value assignments, and the base64 blob push_file()/push_binary() pipe
through `base64 -d` to write rendered files (.env, certs, compose) whose
CONTENT is secret-bearing even though the shell command itself is generic.
redact() catches all three patterns. It does NOT catch a secret embedded in
free-form prose inside a script body that doesn't match one of those shapes
(e.g. a secret hardcoded oddly inside a heredoc) — this is best-effort, not
a guarantee, which is why the file is written 0600 like the state file, and
the handoff landing card says so explicitly rather than implying it's safe
to hand around casually.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

# Patterns matched against command/output TEXT before it is written to disk.
# Kept narrow and named on purpose: a pattern too eager to match starts
# hiding useful debugging context (port numbers, ordinary flag values), and
# a false sense of "everything is redacted" is worse than an operator who
# knows to treat the file as sensitive regardless.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # curl -H 'Authorization: Bearer <token>' ...
    (re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.IGNORECASE), r"\1<redacted>"),
    # KEY=value assignments where KEY looks like a secret — env exports,
    # rendered .env content visible in a script body, psql connection
    # strings, etc. (?: for common suffixes so e.g. AUTHENTIK_SECRET_KEY,
    # PG_PASS, AUTHENTIK_BOOTSTRAP_TOKEN all match).
    (re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:PASSWORD|SECRET|TOKEN|PASS)[A-Za-z0-9_]*)=\S+"),
     r"\1=<redacted>"),
    # push_file()/push_binary() write rendered files (.env, certs, compose)
    # via `echo '<base64>' | base64 -d > path` — the encoded blob typically
    # *is* the secret (a whole .env), and reading it back is never useful,
    # so it is collapsed on sight rather than searched for individual
    # fields inside it.
    (re.compile(r"(echo )'[A-Za-z0-9+/=]{40,}'( \| base64 -d)"),
     r"\1'<base64 payload redacted>'\2"),
]


def redact(text: str) -> str:
    """Best-effort secret redaction — see module docstring for exact scope."""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class Transcript:
    """One append-only file per `provision`/`clean` run.

    Flat and chronological by design (not one file per node) — grep for
    `[ak-node-2]` to filter by node, or `(patroni)` to filter by phase;
    that's simpler than reconstructing an interleaved timeline from several
    files after the fact.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")
        self.path.chmod(0o600)
        self._f.write(f"\n=== akropolis run started "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        self._f.flush()

    def record(self, node: str, phase: str, cmd: str, rc: int, out: str, err: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._f.write(f"\n[{ts}] [{node}] ({phase or '-'}) $ {redact(cmd)}\n")
        for line in redact(out).splitlines():
            self._f.write(f"  out| {line}\n")
        for line in redact(err).splitlines():
            self._f.write(f"  err| {line}\n")
        self._f.write(f"  rc={rc}\n")
        # Flushed on every write, not buffered until close(): a run that
        # crashes or is killed mid-phase should still leave a usable trail
        # up to the point it stopped, not an empty or truncated file.
        self._f.flush()

    def note(self, text: str) -> None:
        """Free-form marker line — phase boundaries, SFTP transfers, etc."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._f.write(f"\n[{ts}] --- {text} ---\n")
        self._f.flush()

    def close(self) -> None:
        if self._f and not self._f.closed:
            self._f.write(f"\n=== akropolis run ended "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            self._f.close()
