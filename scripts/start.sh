#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "Python 3.10–3.14 is required. Install Python 3.12 first." >&2
  exit 1
fi
exec "$PYTHON" "$ROOT/scripts/start.py" "$@"
