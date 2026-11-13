#!/usr/bin/env bash
# Build a Meridian Beta release tarball, checksum, and update manifest.
#
#   scripts/build_release.sh [--base-url URL]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
CHANNEL="$(tr -d '[:space:]' < "$ROOT/CHANNEL" 2>/dev/null || echo stable)"
BASE_URL="https://meridian.matthewmerino.com/desk"

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) BASE_URL="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
APP="$STAGE/meridian-$VERSION"
mkdir -p "$APP"

copy() { if [ -e "$ROOT/$1" ]; then cp -R "$ROOT/$1" "$APP/"; fi; }

mkdir -p "$APP/backend" "$APP/analysis" "$APP/agent" "$APP/meridian_cli" "$APP/data"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/backend/" "$APP/backend/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/analysis/" "$APP/analysis/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/agent/" "$APP/agent/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/meridian_cli/" "$APP/meridian_cli/"
# The data/ directory is a Python package whose stores live beside the code.
# Ship the modules only; the operator's stores are created on first run.
find "$ROOT/data" -maxdepth 1 -name '*.py' -exec cp {} "$APP/data/" \;
[ -f "$ROOT/data/AGENTS.md" ] && cp "$ROOT/data/AGENTS.md" "$APP/data/"
cp "$ROOT/config.py" "$ROOT/requirements.txt" "$ROOT/pyproject.toml" "$APP/"
cp "$ROOT/LICENSE" "$APP/" 2>/dev/null || true
cp "$ROOT/CHANNEL" "$APP/" 2>/dev/null || true
cp "$ROOT/SECURITY.md" "$APP/" 2>/dev/null || true
cp "$ROOT/install.sh" "$APP/"
[ -f "$ROOT/docs/contributing.md" ] && cp "$ROOT/docs/contributing.md" "$APP/CONTRIBUTING.md"
cp "$ROOT/VERSION" "$APP/"
cp "$ROOT/AGENTS.md" "$APP/" 2>/dev/null || true
if [ -f "$ROOT/README-desk.md" ]; then cp "$ROOT/README-desk.md" "$APP/README.md"; elif [ -f "$ROOT/README.md" ]; then cp "$ROOT/README.md" "$APP/README.md"; fi
cp -R "$ROOT/skills" "$APP/"
cp -R "$ROOT/docs" "$APP/"
cp -R "$ROOT/fixtures" "$APP/"

mkdir -p "$ROOT/dist"
TARBALL="$ROOT/dist/meridian-$VERSION.tar.gz"
tar -C "$APP" -czf "$TARBALL" .
SHA="$(python3 - "$TARBALL" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"
cp "$TARBALL" "$ROOT/dist/latest.tar.gz"

NOTES_FILE="$ROOT/RELEASE_NOTES.md"
NOTES_JSON="$(python3 - "$NOTES_FILE" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
notes = []
started = False
if p.exists():
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            if started:
                break
            started = True
            continue
        if started and line.strip().startswith(("-", "*")):
            notes.append(line.strip().lstrip("-* ").strip())
print(json.dumps(notes, ensure_ascii=False))
PY
)"

cat > "$ROOT/dist/manifest.json" <<EOF
{
  "version": "$VERSION",
  "channel": "$CHANNEL",
  "released": "$(date -u +%Y-%m-%d)",
  "url": "$BASE_URL/meridian-$VERSION.tar.gz",
  "sha256": "$SHA",
  "notes": $NOTES_JSON
}
EOF

SIZE="$(du -h "$TARBALL" | cut -f1)"
echo "built  dist/meridian-$VERSION.tar.gz  ($SIZE)"
echo "sha256 $SHA"
echo "manifest dist/manifest.json"
echo
echo "publish:"
echo "  scp dist/meridian-$VERSION.tar.gz dist/latest.tar.gz dist/manifest.json \\"
echo "      user@your-host:/var/www/meridian-beta/"
