#!/usr/bin/env bash
#
# Build the single-file akropolis executable (a PEP 441 zipapp).
#
#   ./tools/build_pyz.sh          -> dist/akropolis
#
# The result is one executable file carrying akropolis plus its pure-Python
# dependencies (paramiko, Jinja2, PyYAML, rich, textual, requests, urllib3
# and their transitive pure-Python deps). Copy it anywhere and run it; there
# is no install step.
#
# WHAT IS DELIBERATELY *NOT* BUNDLED
# ----------------------------------
# zipimport cannot load compiled extension modules (.so) out of a zip, so
# paramiko's compiled dependencies -- and psycopg2's -- must come from the
# system:
#
#     sudo apt install python3-cryptography python3-bcrypt python3-nacl \
#                      python3-psycopg2
#
# This is a feature, not a workaround. cryptography stays on the distribution's
# security-update track instead of being frozen inside a release artifact that
# nobody re-cuts for six months. Bundling it would also make this file
# architecture-specific; as built, it runs on any CPython >= 3.10.
#
# psycopg2 is the one genuinely optional package in that apt line: the
# `monitor` dashboard guards its import and simply omits the PostgreSQL
# replication-slot panel detail without it. The other three are required
# because paramiko will not import without them.
#
# psycopg2-binary needs more care than the rest to strip. It ships its
# compiled module *inside* the package directory (psycopg2/_psycopg.cpython-
# *.so) AND a sibling psycopg2_binary.libs/ holding vendored shared objects
# (libpq, libssl, libkrb5, ...) named like `libpq-f521cc7d.so.5.17` -- they do
# NOT end in ".so", so a `-name '*.so'` sweep alone walks straight past every
# one of them. Hence the explicit directory removal below AND the `*.so*`
# glob, not just one or the other.
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
# Whole packages that exist only to back cryptography/paramiko/psycopg2's C layer.
rm -rf "$BUILD"/cryptography "$BUILD"/cryptography-*.dist-info \
       "$BUILD"/bcrypt "$BUILD"/bcrypt-*.dist-info \
       "$BUILD"/nacl "$BUILD"/PyNaCl-*.dist-info "$BUILD"/pynacl-*.dist-info \
       "$BUILD"/cffi "$BUILD"/cffi-*.dist-info \
       "$BUILD"/pycparser "$BUILD"/pycparser-*.dist-info \
       "$BUILD"/_yaml \
       "$BUILD"/psycopg2 "$BUILD"/psycopg2_binary-*.dist-info \
       "$BUILD"/psycopg2_binary.libs \
       "$BUILD"/bin
# Anything else compiled, plus bytecode caches. Note '*.so*', not '*.so':
# psycopg2-binary's vendored libs carry their soname version after the
# extension (see header) and a bare '*.so' pattern misses them silently.
find "$BUILD" -name '*.so*' -delete
find "$BUILD" -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "==> asserting nothing compiled survived"
leftover="$(find "$BUILD" -name '*.so*' -o -name '*.pyd')"
if [ -n "$leftover" ]; then
    echo "error: compiled artifacts survived the strip:" >&2
    echo "$leftover" >&2
    exit 1
fi

# The .dist-info directories stay, but pruned to what is actually read at
# runtime or owed to the licenses of what we're bundling. METADATA is not
# optional: paramiko resolves its own version through importlib.metadata at
# import time and raises PackageNotFoundError without it. The LICENSE* /
# COPYING* / NOTICE* / AUTHORS* files are not optional either: this archive
# redistributes these packages' source, paramiko among them under the LGPL,
# and the license text has to travel with the code it covers. Most wheels
# nest theirs under dist-info/licenses/ (below our maxdepth 2, so already
# untouched), but not all of them do -- mdurl 0.1.2 ships a bare top-level
# LICENSE file in its dist-info, which the old (METADATA-and-entry_points.txt-
# only) filter silently deleted. Caught by inspecting the built artifact
# directly rather than assuming the filter was exhaustive; see NOTES.md.
#
# Everything else pruned is build-host residue that makes the artifact
# non-reproducible:
#   direct_url.json  absolute path of the source tree on the build machine
#   WHEEL            interpreter tag of the downloaded wheel (cp310 vs cp312)
#   RECORD           hashes of console scripts whose shebang is the build
#                    machine's interpreter path
#   INSTALLER,
#   REQUESTED        no runtime consumer
find "$BUILD" -maxdepth 2 -type f -path '*.dist-info/*' \
     ! -name 'METADATA' ! -name 'entry_points.txt' \
     ! -iname 'LICENSE*' ! -iname 'LICENCE*' \
     ! -iname 'COPYING*' ! -iname 'NOTICE*' ! -iname 'AUTHORS*' \
     -delete
find "$BUILD" -type d -path '*.dist-info/*' -empty -delete

echo "==> writing third-party license manifest"
# One file, generated from what actually got bundled rather than hand-kept in
# sync, listing every vendored dependency's declared license. The full license
# texts themselves already travel inside the archive (see above); this is the
# human-readable index of what's in there and under what terms, shipped
# alongside the binary as dist/THIRD_PARTY_LICENSES.md. Shares its row/render
# logic with `akropolis licenses` (akropolis/licenses.py) so the two listings
# can't drift apart.
NOTE="akropolis (MIT) is distributed as a single-file zipapp that also carries the pure-Python packages it depends on -- their source ships inside this archive, not just akropolis's own. Each package's full license text ships alongside it, under the paths listed below; this file is the index, not a substitute for those texts.

paramiko is LGPL-2.1: the version bundled here is unmodified, readable Python source, sitting in this same archive next to the license that covers it."
PYTHONPATH="$ROOT" "$PY" - "$BUILD" "$NOTE" <<'EOF' > "$DIST/THIRD_PARTY_LICENSES.md"
import pathlib, sys

from akropolis import licenses

root = pathlib.Path(sys.argv[1])
note = sys.argv[2]
rows = licenses.rows_from_dist_info_dir(root)
sys.stdout.write(licenses.render(rows, note=note))
EOF

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
for name in ("cryptography", "bcrypt", "nacl", "psycopg2"):
    print(f"  {name}")
print()
print("psycopg2 is optional: the `monitor` dashboard degrades gracefully")
print("(no PostgreSQL replication-slot detail) without it.")
print()
print("Licenses for the bundled packages above: see THIRD_PARTY_LICENSES.md")
print("(shipped next to this binary) or <package>.dist-info/licenses/ inside")
print("this archive.")
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
