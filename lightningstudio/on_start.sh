#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Noble Trader — Lightning AI Studio on_start.sh
#
# This script runs automatically when the Studio starts/resumes.
# Place in: /teamspace/studios/this_studio/.lightning_studio/on_start.sh
#
# Configure in Lightning AI Studio:
#   Settings → Studio → Start Script → ./on_start.sh
# ═══════════════════════════════════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../noble-trader"

echo "═══════════════════════════════════════════════════════════════════"
echo "  Noble Trader — on_start.sh"
echo "  Starting background services..."
echo "═══════════════════════════════════════════════════════════════════"

# ── Cleanup stale processes from previous sessions ──────────
pkill -f "python main.py" 2>/dev/null || true
pkill -f "python server.py" 2>/dev/null || true
pkill -f "python proxy.py" 2>/dev/null || true
pkill -f "python -m regime_platform.sweep_orchestrator" 2>/dev/null || true
sleep 2

echo "  [1/4] Starting Main App (port fallback → 8000)..."
python main.py &
PID_MAIN=$!

echo "  [2/4] Starting TimesFM Server (port 8002)..."
PORT=8002 python server.py &
PID_TIMESFM=$!

echo "  [3/4] Starting Sweep Orchestrator (port 8003)..."
SWEEP_ORCHESTRATOR_PORT=8003 python -m regime_platform.sweep_orchestrator &
PID_ORCH=$!

echo "  [4/4] Starting Gateway Proxy (port 8000)..."
PORT=8000 python proxy.py &
PID_GATEWAY=$!

echo ""
echo "  Main app PID:  $PID_MAIN"
echo "  TimesFM PID:   $PID_TIMESFM"
echo "  Orch PID:      $PID_ORCH"
echo "  Gateway PID:   $PID_GATEWAY"
echo "  Public URL:    https://8000-$(hostname -s 2>/dev/null || echo '...').cloudspaces.litng.ai"
echo ""
echo "  Services running in background. Terminal can be closed."
echo "═══════════════════════════════════════════════════════════════════"

cleanup() {
    echo "on_start.sh: Shutting down services..."
    kill $PID_MAIN $PID_TIMESFM $PID_ORCH $PID_GATEWAY 2>/dev/null
    wait
}
trap cleanup EXIT INT TERM

wait
