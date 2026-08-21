#!/usr/bin/env bash
# noble — CLI wrapper for Noble Trader calibration tools
# Usage: ./noble.sh calibration

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/Scripts/python.exe"
SRC="$REPO_ROOT/src"

if [ ! -f "$VENV_PY" ]; then
  echo "ERROR: .venv/Scripts/python.exe not found at $VENV_PY" >&2
  echo "Run: cd $REPO_ROOT && uv sync" >&2
  exit 1
fi

export PYTHONPATH="$SRC"

case "${1:-}" in
  calibration)
    exec "$VENV_PY" "$REPO_ROOT/scripts/noble_calibration.py"
    ;;
  *)
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  calibration   Run 24hr calibration report (scripts/noble_calibration.py)"
    echo ""
    exit 1
    ;;
esac
