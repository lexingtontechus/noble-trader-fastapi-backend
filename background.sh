#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Noble Trader Stack — Background Execution (Lightning AI "play" button)
#
# Designed for Lightning AI Studio's persistent "play" button execution mode.
# Skips the gateway proxy since main.py now has the /forecast pass-through.
#
# Usage:
#   ./background.sh
#
# Environment variables (set in Lightning Studio → Secrets):
#   TIMESFM_API_KEYS  – Comma-separated API keys for /forecast (required)
# ───────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Cleanup stale processes ───────────────────────────────────
pkill -f "python main.py" 2>/dev/null || true
pkill -f "python server.py" 2>/dev/null || true
sleep 2

# ── Start services ─────────────────────────────────────────────
echo "Starting Noble Trader background stack..."
echo "  Main app  → will bind to PORT or fall back to 8000"
echo "  TimesFM   → port 8002"

python main.py &
PID_MAIN=$!

PORT=8002 python server.py &
PID_TIMESFM=$!

echo "  PID main:    $PID_MAIN"
echo "  PID timesfm: $PID_TIMESFM"

# Trap to kill both on exit
cleanup() {
    echo "Shutting down..."
    kill $PID_MAIN $PID_TIMESFM 2>/dev/null
    wait
}
trap cleanup EXIT INT TERM

# Wait for both (will keep running until killed)
wait
