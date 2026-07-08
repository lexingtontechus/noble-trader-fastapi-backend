#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Noble Trader — Lightning AI Studio on_start.sh
#
# This script runs automatically when the Studio starts/resumes.
# Place in: /teamspace/studios/this_studio/.lightning_studio/on_start.sh
#
# Configure in Lightning AI Studio:
#   Settings → Studio → Start Script → ./on_start.sh
#
# Service order (important):
#   1. TimesFM (port 8002)   — START FIRST, slowest to load (30-60s model
#                              load, 2-3 min on first run downloading weights)
#   2. Main app (port 8001)  — starts in parallel with TimesFM loading
#   3. Gateway (port 8000)   — starts once main app is healthy
#   4. Orchestrator (8003)   — starts LAST, only after TimesFM model is loaded
#
# The orchestrator must not start until TimesFM is ready, otherwise its
# first sweep cycle will get a 503 and fall into "TimesFM unavailable"
# fallback mode (p_timesfm=None → no qualified signals → no Discord
# notifications, no Redis writes).
# ═══════════════════════════════════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../noble-trader"

# ── Config ─────────────────────────────────────────────────────────────────
TIMESFM_READY_TIMEOUT="${TIMESFM_READY_TIMEOUT:-180}"

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

# ── Helper: wait for HTTP 200 ──────────────────────────────────────────────
wait_for_http() {
    local url="$1"
    local label="$2"
    local timeout="$3"
    local elapsed=0
    while [ "$elapsed" -lt "$timeout" ]; do
        if curl -sf "$url" >/dev/null 2>&1; then
            echo "  ✓ $label is up (after ${elapsed}s)"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "  ⚠ $label not responding after ${timeout}s"
    return 1
}

# ── Helper: wait for TimesFM model to load ─────────────────────────────────
wait_for_timesfm_ready() {
    local timeout="$1"
    local elapsed=0
    echo "  Waiting for TimesFM model to load (timeout: ${timeout}s)..."
    while [ "$elapsed" -lt "$timeout" ]; do
        if ! kill -0 "$PID_TIMESFM" 2>/dev/null; then
            echo "  ✗ TimesFM process died during model load!"
            return 1
        fi
        health_json=$(curl -sf http://127.0.0.1:8002/health 2>/dev/null || echo "")
        if [ -n "$health_json" ]; then
            status=$(echo "$health_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            if [ "$status" = "ok" ]; then
                echo "  ✓ TimesFM model loaded (after ${elapsed}s)"
                return 0
            fi
            if [ $((elapsed % 10)) -eq 0 ] && [ "$elapsed" -gt 0 ]; then
                echo "    ...still loading (${elapsed}s elapsed)"
            fi
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "  ⚠ TimesFM model not ready after ${timeout}s — starting orchestrator anyway"
    return 1
}

# ── 1. TimesFM Server (port 8002) — START FIRST ───────────────────────────
# TimesFM is the slowest service (model download + load). Start it first
# so the model loads in parallel while we start the other services.
echo ""
echo "  [1/4] Starting TimesFM Server (port 8002)..."
PORT=8002 python server.py &
PID_TIMESFM=$!
echo "    PID: $PID_TIMESFM"

# ── 2. Main App (port 8001) ───────────────────────────────────────────────
# CRITICAL: set PORT=8001 explicitly. The previous script ran `python main.py`
# with no PORT env, so main.py defaulted to 8000 — which then conflicted
# with the gateway (proxy.py) that also binds to 8000. The gateway would
# crash with "Address already in use" and the public URL would never work.
echo ""
echo "  [2/4] Starting Main App (port 8001)..."
PORT=8001 python main.py &
PID_MAIN=$!
echo "    PID: $PID_MAIN"

# Wait for main app to bind
wait_for_http "http://127.0.0.1:8001/health" "Main App" 30 || true

# ── 3. Gateway Proxy (port 8000) — public-facing ──────────────────────────
# Start the gateway BEFORE the orchestrator so the orchestrator can use
# http://localhost:8000/forecast as its TimesFM URL (which proxies to 8002).
echo ""
echo "  [3/4] Starting Gateway Proxy (port 8000)..."
PORT=8000 python proxy.py &
PID_GATEWAY=$!
echo "    PID: $PID_GATEWAY"

# ── 4. Wait for TimesFM model to finish loading ───────────────────────────
# This is the critical step. The orchestrator must not start until the
# TimesFM model is loaded, otherwise the first sweep cycle fails.
echo ""
echo "  Waiting for TimesFM model to finish loading before starting orchestrator..."
wait_for_timesfm_ready "$TIMESFM_READY_TIMEOUT" || true

# ── 5. Sweep Orchestrator (port 8003) — START LAST ────────────────────────
echo ""
echo "  [4/4] Starting Sweep Orchestrator (port 8003)..."
SWEEP_ORCHESTRATOR_PORT=8003 python -m regime_platform.sweep_orchestrator &
PID_ORCH=$!
echo "    PID: $PID_ORCH"

# ── Final summary ──────────────────────────────────────────────────────────
echo ""
echo "  Main app PID:  $PID_MAIN"
echo "  TimesFM PID:   $PID_TIMESFM"
echo "  Orch PID:      $PID_ORCH"
echo "  Gateway PID:   $PID_GATEWAY"
echo "  Public URL:    https://8000-$(hostname -s 2>/dev/null || echo '...').cloudspaces.litng.ai"
echo ""

# Quick health summary
if command -v curl &>/dev/null; then
    echo "  Health summary:"
    main_h=$(curl -sf http://127.0.0.1:8001/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "DOWN")
    tfm_h=$(curl -sf http://127.0.0.1:8002/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "DOWN")
    gw_h=$(curl -sf http://127.0.0.1:8000/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "DOWN")
    echo "    Main App    : $main_h"
    echo "    TimesFM     : $tfm_h"
    echo "    Gateway     : $gw_h"
fi

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
