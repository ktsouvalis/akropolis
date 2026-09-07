#!/usr/bin/env bash
#
# Build the single-file akropolis executable (a PEP 441 zipapp).
#
#   ./tools/build_pyz.sh          -> dist/akropolis
#
# The result is one executable file carrying akropolis plus its pure-Python
# dependencies (paramiko, Jinja2, PyYAML, rich and their transitive pure-Python
# deps). Copy it anywhere and run it; there is no install step.
#
# WHAT IS DELIBERATELY *NOT* BUNDLED
# ----------------------------------
# zipimport cannot load compiled extension modules (.so) out of a zip, so
# paramiko's compiled dependencies must come from the system:
#
#     sudo apt install python3-cryptography python3-bcrypt python3-nacl
#
# This is a feature, not a workaround. cryptography stays on the distribution's
# security-update track instead of being frozen inside a release artifact that
# nobody re-cuts for six months. Bundling it would also make this file
# architecture-specific; as built, it runs on any CPython >= 3.10.
#
# PyYAML's _yaml and MarkupSafe's _speedups are stripped for the same reason.
# Both fall back to their pure-Python implementations automatically -- slower,
# irrelevant at the volume of YAML and templating akropolis does.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build/pyz"
DIST="$ROOT/dist"
OUT="$DIST/akropolis"

# Deterministic timestamps so two builds of the same commit produce the same
# bytes. Falls back to the commit date, then to a fixed epoch.
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || echo 1700000000)}"

# Releases are built on this interpreter. The artifact's *contents* depend on
# it -- rich pulls typing_extensions only below 3.11, and the wheels pip picks
# for packages with compiled variants carry the interpreter tag in their
# metadata. A build on a newer interpreter is fine to run locally, but will not
# be byte-identical to the release, and must not be published: it would omit
# typing_extensions and fail on a 3.10 host.
REFERENCE_PYTHON="3.10"

PY="${PYTHON:-python3}"
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"need Python >= 3.10 to build, have {sys.version.split()[0]}")
EOF

PY_MM="$("$PY" -c 'import sys;print("%d.%d" % sys.version_info[:2])')"
if [ "$PY_MM" != "$REFERENCE_PYTHON" ]; then
    echo "note: building with Python $PY_MM, releases use $REFERENCE_PYTHON."
    echo "      Fine for local use; will not match the release checksum."
    echo "      For an identical artifact: PYTHON=python$REFERENCE_PYTHON $0"
fi

echo "==> cleaning"
rm -rf "$BUILD" "$OUT"
mkdir -p "$BUILD" "$DIST"

echo "==> vendoring akropolis + dependencies"
"$PY" -m pip install --quiet --no-compile --target "$BUILD" "$ROOT"

echo "==> stripping compiled artifacts (see header)"
# Whole packages that exist only to back cryptography/paramiko's C layer.
rm -rf "$BUILD"/cryptography "$BUILD"/cryptography-*.dist-info \
       "$BUILD"/bcrypt "$BUILD"/bcrypt-*.dist-info \
       "$BUILD"/nacl "$BUILD"/PyNaCl-*.dist-info "$BUILD"/pynacl-*.dist-info \
       "$BUILD"/cffi "$BUILD"/cffi-*.dist-info \
       "$BUILD"/pycparser "$BUILD"/pycparser-*.dist-info \
       "$BUILD"/_yaml \
       "$BUILD"/bin
# Anything else compiled, plus bytecode caches.
find "$BUILD" -name '*.so' -delete
find "$BUILD" -name '__pycache__' -type d -prune -exec rm -rf {} +

# The .dist-info directories stay, but pruned to what is actually read at
# runtime. METADATA is not optional: paramiko resolves its own version through
# importlib.metadata at import time and raises PackageNotFoundError without it.
#
# Everything else is build-host residue that makes the artifact
# non-reproducible:
#   direct_url.json  absolute path of the source tree on the build machine
#   WHEEL            interpreter tag of the downloaded wheel (cp310 vs cp312)
#   RECORD           hashes of console scripts whose shebang is the build
#                    machine's interpreter path
#   INSTALLER,
#   REQUESTED        no runtime consumer
find "$BUILD" -maxdepth 2 -type f -path '*.dist-info/*' \
     ! -name 'METADATA' ! -name 'entry_points.txt' -delete
find "$BUILD" -type d -path '*.dist-info/*' -empty -delete

echo "==> writing manifest"
"$PY" - "$BUILD" <<'EOF' > "$BUILD/BUNDLE-MANIFEST.txt"
import pathlib, sys
root = pathlib.Path(sys.argv[1])
rows = []
for d in sorted(root.glob("*.dist-info")):
    name, _, version = d.name[: -len(".dist-info")].rpartition("-")
    rows.append((name, version))
print(f"Built with Python {sys.version_info.major}.{sys.version_info.minor}.")
print("The bundled set is interpreter-dependent; an artifact built on a newer")
print("interpreter may omit packages a 3.10 host needs.")
print()
print("Packages bundled inside this file:")
print()
for name, version in rows:
    print(f"  {name:<20} {version}")
print()
print("Supplied by the system, NOT bundled (apt install python3-<name>):")
print()
for name in ("cryptography", "bcrypt", "nacl"):
    print(f"  {name}")
EOF

cat > "$BUILD/__main__.py" <<'EOF'
import sys

from akropolis.cli import main

sys.exit(main())
EOF

echo "==> normalising timestamps"
find "$BUILD" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

echo "==> zipping"
"$PY" -m zipapp "$BUILD" \
    --python "/usr/bin/env python3" \
    --output "$OUT" \
    --compress
chmod +x "$OUT"

echo
echo "built: $OUT ($(du -h "$OUT" | cut -f1))"
"$OUT" --version
