#!/usr/bin/env bash
# Run Cloudbase locally for development (macOS / Linux).
#
#   scripts/dev.sh            start on http://127.0.0.1:7900
#   scripts/dev.sh --seed     also add demo apps
#   scripts/dev.sh --reset    wipe dev data first
#   PORT=8000 scripts/dev.sh
#
# All data lives in .dev/ inside the repo; a real ~/.cloudbase is never touched.
# Login: admin / cloudbase-dev.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.dev"
DEV_HOME="$DEV/home"
VENV="$DEV/venv"
PY="$VENV/bin/python"
REQ="$ROOT/backend/requirements.txt"
PORT="${PORT:-7900}"
PASSWORD="cloudbase-dev"
SEED=0

for arg in "$@"; do
  case "$arg" in
    --seed)  SEED=1 ;;
    --reset) echo "[dev] removing previous dev data"; rm -rf "$DEV_HOME" ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done
mkdir -p "$DEV_HOME"

if [ ! -x "$PY" ]; then
  echo "[dev] creating virtual environment in .dev/venv"
  python3 -m venv "$VENV"
fi

HASH_FILE="$DEV/requirements.sha256"
HASH="$(sha256sum "$REQ" 2>/dev/null || shasum -a 256 "$REQ")"
HASH="${HASH%% *}"
if [ "$(cat "$HASH_FILE" 2>/dev/null || true)" != "$HASH" ]; then
  echo "[dev] installing backend dependencies"
  "$PY" -m pip install --disable-pip-version-check -q -r "$REQ"
  echo "$HASH" > "$HASH_FILE"
fi

export HOME="$DEV_HOME"
"$PY" "$ROOT/scripts/dev_seed.py" credentials "$PASSWORD"
[ "$SEED" = 1 ] && "$PY" "$ROOT/scripts/dev_seed.py" demo

echo
echo "  Cloudbase dev server:  http://127.0.0.1:$PORT"
echo "  Login:                 admin / $PASSWORD"
echo "  Stop with Ctrl+C"
echo

cd "$ROOT/backend"
exec "$PY" -m uvicorn main:app --host 127.0.0.1 --port "$PORT" --reload --reload-dir . --timeout-graceful-shutdown 3
