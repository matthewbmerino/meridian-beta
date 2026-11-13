#!/usr/bin/env bash
# Meridian Desk installer.
#
#   curl -fsSL https://meridian.matthewmerino.com/install.sh | bash
#
# Installs into ~/.meridian (override with MERIDIAN_HOME or --home), creates a
# private virtualenv, and leaves a `meridian` command in ~/.local/bin.
set -euo pipefail

MH="${MERIDIAN_HOME:-$HOME/.meridian}"
MANIFEST_URL="${MERIDIAN_UPDATE_URL:-https://meridian.matthewmerino.com/desk/manifest.json}"
LOCAL_TARBALL=""
RUN_SETUP=1
FORCE=0
VERSION=""

say()  { printf '\033[1;36m[meridian]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --home) MH="$2"; shift 2 ;;
    --url) MANIFEST_URL="$2"; shift 2 ;;
    --local) LOCAL_TARBALL="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --no-setup) RUN_SETUP=0; shift ;;
    --no-ui) shift ;;  # headless edition; accepted for compatibility
    --force) FORCE=1; shift ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

say "Meridian Desk installer"
printf '  home: %s\n' "$MH"

# ---------------------------------------------------------------------------
# Platform + prerequisites
# ---------------------------------------------------------------------------
case "$(uname -s)" in
  Darwin|Linux) ;;
  *) die "Meridian runs on macOS and Linux. On Windows, use WSL2." ;;
esac

# Meridian pins pandas 2.1 / numpy 1.26, which do not ship wheels for Python
# 3.13+. The system Python does not have to match: when it is newer (or older)
# than 3.11/3.12, uv provisions a managed 3.12 for the venv. Omarchy, Arch and
# recent Fedora all ship 3.13/3.14, so this is the common path, not an edge case.
PYTHON_BIN="${MERIDIAN_PYTHON:-$(command -v python3 || true)}"
PY_OK="no"
if [ -n "$PYTHON_BIN" ]; then
  PY_OK="$("$PYTHON_BIN" - <<'PY'
import sys
print("yes" if (3, 11) <= sys.version_info[:2] < (3, 13) else "no")
PY
)"
fi

UV_BIN="$(command -v uv || true)"
PYTHON_FOR_VENV="$PYTHON_BIN"

if [ "$PY_OK" != "yes" ]; then
  if [ -n "$PYTHON_BIN" ]; then
    warn "System Python $( "$PYTHON_BIN" -V 2>&1 | awk '{print $2}' ) is not 3.11/3.12 — the app pins a 3.12 stack."
  else
    warn "No python3 on PATH — provisioning a private 3.12."
  fi
  if [ -z "$UV_BIN" ]; then
    command -v curl >/dev/null 2>&1 || die "curl is required to fetch uv (or install Python 3.12 yourself)."
    say "Fetching a private uv (no sudo)"
    mkdir -p "$MH/bin"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$MH/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || true
    UV_BIN="$MH/bin/uv"
  fi
  [ -x "$UV_BIN" ] || die "Could not install uv. Install Python 3.12 or uv, then re-run."
  say "Provisioning Python 3.12 with uv"
  "$UV_BIN" python install 3.12 >/dev/null 2>&1 || die "Could not provision Python 3.12."
  PYTHON_FOR_VENV="3.12"
fi

if [ -d "$MH/app" ] && [ "$FORCE" != "1" ]; then
  die "Meridian is already installed at $MH/app. Run '$MH/.local/bin/meridian update' or pass --force."
fi

# ---------------------------------------------------------------------------
# Release: manifest or local tarball
# ---------------------------------------------------------------------------
mkdir -p "$MH/downloads" "$MH/logs" "$MH/run" "$MH/backups"

TARBALL=""
SHA=""
TARBALL_URL=""
if [ -n "$LOCAL_TARBALL" ]; then
  [ -f "$LOCAL_TARBALL" ] || die "no such file: $LOCAL_TARBALL"
  TARBALL="$LOCAL_TARBALL"
  [ -n "$VERSION" ] || VERSION="$(tar -xOf "$TARBALL" ./VERSION 2>/dev/null | tr -d '[:space:]' || echo local)"
  ok "using local release ${VERSION}"
else
  say "Fetching release manifest"
  command -v curl >/dev/null 2>&1 || die "curl is required."
  MANIFEST="$(curl -fsSL "$MANIFEST_URL")" || die "could not fetch $MANIFEST_URL"
  eval "$(python3 - "$MANIFEST" <<'PY'
import json, shlex, sys
m = json.loads(sys.argv[1])
print("VERSION=" + shlex.quote(str(m.get("version", ""))))
print("TARBALL_URL=" + shlex.quote(str(m.get("url", ""))))
print("SHA=" + shlex.quote(str(m.get("sha256", ""))))
PY
)"
  [ -n "$VERSION" ] && [ -n "$TARBALL_URL" ] || die "manifest is missing version or url"
  TARBALL="$MH/downloads/meridian-$VERSION.tar.gz"
  say "Downloading Meridian $VERSION"
  curl -fL --progress-bar "$TARBALL_URL" -o "$TARBALL.part" || die "download failed"
  mv "$TARBALL.part" "$TARBALL"
fi

if [ -n "$SHA" ]; then
  ACTUAL="$(python3 - "$TARBALL" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], "rb") as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
print(h.hexdigest())
PY
)"
  [ "$ACTUAL" = "$SHA" ] || die "checksum mismatch: expected $SHA, got $ACTUAL"
  ok "checksum verified"
fi

# ---------------------------------------------------------------------------
# Unpack + virtualenv
# ---------------------------------------------------------------------------
STAGE="$MH/app.new"
rm -rf "$STAGE"
mkdir -p "$STAGE"
say "Unpacking"
tar -xzf "$TARBALL" -C "$STAGE"
[ -f "$STAGE/requirements.txt" ] || die "release has no requirements.txt"
[ -d "$STAGE/backend" ] || die "release has no backend/"
[ -f "$STAGE/VERSION" ] && VERSION="$(tr -d '[:space:]' < "$STAGE/VERSION")"
CHANNEL="stable"
if [ -f "$STAGE/CHANNEL" ]; then
  CHANNEL="$(tr -d '[:space:]' < "$STAGE/CHANNEL")"
fi

# A forced reinstall moves the tree. Stop the old desk first or the running
# uvicorn/next processes keep serving deleted files.
if [ -x "$HOME/.local/bin/meridian" ]; then
  MERIDIAN_HOME="$MH" "$HOME/.local/bin/meridian" stop >/dev/null 2>&1 || true
fi

if [ -d "$MH/app" ]; then
  OLD="$MH/app.old-$(date +%Y%m%d-%H%M%S)"
  warn "Moving the existing app to $OLD"
  mv "$MH/app" "$OLD"
  # A forced reinstall must not orphan the book. Carry every non-code entry
  # (stores, sidecars, imports, snapshots, secrets) into the new tree.
  if [ -d "$OLD/data" ]; then
    mkdir -p "$STAGE/data"
    find "$OLD/data" -maxdepth 1 ! -name '*.py' ! -name '__pycache__' -exec cp -R {} "$STAGE/data/" \;
    ok "carried the existing data forward"
  fi
fi
mv "$STAGE" "$MH/app"

say "Creating the virtualenv"
# A reinstall or a failed first attempt can leave a venv behind. `uv venv` and
# `python -m venv` both refuse to reuse one, so replace it.
rm -rf "$MH/venv"
if [ -n "$UV_BIN" ] && "$UV_BIN" --version >/dev/null 2>&1; then
  "$UV_BIN" venv --python "$PYTHON_FOR_VENV" "$MH/venv" >/dev/null
  "$UV_BIN" pip install --python "$MH/venv/bin/python" -q -r "$MH/app/requirements.txt"
else
  [ -n "$PYTHON_FOR_VENV" ] && [ -x "$PYTHON_FOR_VENV" ] || die "No suitable Python for the virtualenv."
  "$PYTHON_FOR_VENV" -m venv "$MH/venv"
  "$MH/venv/bin/python" -m pip install -q --upgrade pip
  "$MH/venv/bin/python" -m pip install -q -r "$MH/app/requirements.txt"
fi
ok "dependencies installed"

mkdir -p "$MH/app/data"
chmod 700 "$MH/app/data" 2>/dev/null || true

# ---------------------------------------------------------------------------
# `meridian` command
# ---------------------------------------------------------------------------
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
cat > "$BIN/meridian" <<EOF
#!/bin/sh
export MERIDIAN_HOME="\${MERIDIAN_HOME:-$MH}"
export PYTHONPATH="$MH/app\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$MH/venv/bin/python" -m meridian_cli "\$@"
EOF
chmod +x "$BIN/meridian"
ok "installed $BIN/meridian"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not on PATH. Add: export PATH=\"$BIN:\$PATH\"" ;;
esac

echo
ok "Meridian $VERSION ($CHANNEL) installed at $MH"
if [ "$CHANNEL" = "beta" ]; then
  warn "Early beta: expect rough edges. Run \`meridian qa\`; report with \`meridian report\`."
fi
echo

if [ "$RUN_SETUP" = "1" ] && [ -t 0 ] && [ -t 1 ]; then
  exec "$BIN/meridian" setup
else
  echo "  Next: $BIN/meridian setup"
  echo "  Then: $BIN/meridian status"
fi
