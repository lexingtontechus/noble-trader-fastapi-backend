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
pkill -f "python llm_server.py" 2>/dev/null || true
pkill -f "python -m regime_platform.sweep_orchestrator" 2>/dev/null || true
sleep 2

# ── Start services ─────────────────────────────────────────────
echo "Starting Noble Trader background stack..."
echo "  Main app    → will bind to PORT or fall back to 8000"
echo "  TimesFM     → port 8002"
echo "  Local LLM   → port 8004"
echo "  Sweep Orch  → port 8003"

python main.py &
PID_MAIN=$!

PORT=8002 python server.py &
PID_TIMESFM=$!

LOCAL_LLM_PORT=8004 python llm_server.py &
PID_LLM=$!

SWEEP_ORCHESTRATOR_PORT=8003 python -m regime_platform.sweep_orchestrator &
PID_ORCH=$!

echo "  PID main:    $PID_MAIN"
echo "  PID timesfm: $PID_TIMESFM"
echo "  PID llm:     $PID_LLM"
echo "  PID orch:    $PID_ORCH"

# Trap to kill all on exit
cleanup() {
    echo "Shutting down..."
    kill $PID_MAIN $PID_TIMESFM $PID_LLM $PID_ORCH 2>/dev/null
    wait
}
trap cleanup EXIT INT TERM

# Wait for all (will keep running until killed)
wait
