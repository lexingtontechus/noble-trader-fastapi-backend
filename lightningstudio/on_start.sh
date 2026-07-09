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
# Logging:
#   Each service's stdout+stderr is written to a log file in:
#     /teamspace/studios/this_studio/noble-trader/logs/
#   View logs in real-time:
#     tail -f /teamspace/studios/this_studio/noble-trader/logs/*.log
#   Or view a specific service:
#     tail -f /teamspace/studios/this_studio/noble-trader/logs/orchestrator.log
#
# Service order (important):
#   1. TimesFM (port 8002)   — START FIRST, slowest to load (30-60s model
#                              load, 2-3 min on first run downloading weights)
#   2. Main app (port 8001)  — starts in parallel with TimesFM loading
#   3. Gateway (port 8000)   — starts once main app is healthy
#   4. Orchestrator (8003)   — starts LAST, only after TimesFM model is loaded
# ═══════════════════════════════════════════════════════════════════════════════

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../noble-trader"

# ── Config ─────────────────────────────────────────────────────────────────
TIMESFM_READY_TIMEOUT="${TIMESFM_READY_TIMEOUT:-180}"

# ── Setup log directory ───────────────────────────────────────────────────
# Each service gets its own log file. Logs are appended to (not overwritten)
# so you can see history across restarts. Rotate manually if they get large.
LOG_DIR="$SCRIPT_DIR/../noble-trader/logs"
mkdir -p "$LOG_DIR"

# Log file paths
LOG_TIMESFM="$LOG_DIR/timesfm.log"
LOG_MAIN="$LOG_DIR/main.log"
LOG_GATEWAY="$LOG_DIR/gateway.log"
LOG_ORCH="$LOG_DIR/orchestrator.log"
LOG_STARTUP="$LOG_DIR/startup.log"

# Function: log a message to both stdout and the startup log
log() {
    echo "$@" | tee -a "$LOG_STARTUP"
}

log "═══════════════════════════════════════════════════════════════════"
log "  Noble Trader — on_start.sh"
log "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
log "  Starting background services..."
log "  Logs: $LOG_DIR/"
log "═══════════════════════════════════════════════════════════════════"

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
            log "  ✓ $label is up (after ${elapsed}s)"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log "  ⚠ $label not responding after ${timeout}s"
    return 1
}

# ── Helper: wait for TimesFM model to load ─────────────────────────────────
wait_for_timesfm_ready() {
    local timeout="$1"
    local elapsed=0
    log "  Waiting for TimesFM model to load (timeout: ${timeout}s)..."
    log "  → Watch TimesFM load progress: tail -f $LOG_TIMESFM"
    while [ "$elapsed" -lt "$timeout" ]; do
        if ! kill -0 "$PID_TIMESFM" 2>/dev/null; then
            log "  ✗ TimesFM process died during model load!"
            log "  → Check $LOG_TIMESFM for the error"
            return 1
        fi
        health_json=$(curl -sf http://127.0.0.1:8002/health 2>/dev/null || echo "")
        if [ -n "$health_json" ]; then
            status=$(echo "$health_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            if [ "$status" = "ok" ]; then
                log "  ✓ TimesFM model loaded (after ${elapsed}s)"
                return 0
            fi
            if [ $((elapsed % 10)) -eq 0 ] && [ "$elapsed" -gt 0 ]; then
                log "    ...still loading (${elapsed}s elapsed)"
            fi
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    log "  ⚠ TimesFM model not ready after ${timeout}s — starting orchestrator anyway"
    return 1
}

# ── 1. TimesFM Server (port 8002) — START FIRST ───────────────────────────
# TimesFM is the slowest service (model download + load). Start it first
# so the model loads in parallel while we start the other services.
# Output goes to $LOG_TIMESFM (appended).
log ""
log "  [1/4] Starting TimesFM Server (port 8002)..."
log "    Log: $LOG_TIMESFM"
PORT=8002 python server.py >> "$LOG_TIMESFM" 2>&1 &
PID_TIMESFM=$!
log "    PID: $PID_TIMESFM"

# ── 2. Main App (port 8001) ───────────────────────────────────────────────
# CRITICAL: set PORT=8001 explicitly. The previous script ran `python main.py`
# with no PORT env, so main.py defaulted to 8000 — which then conflicted
# with the gateway (proxy.py) that also binds to 8000. The gateway would
# crash with "Address already in use" and the public URL would never work.
log ""
log "  [2/4] Starting Main App (port 8001)..."
log "    Log: $LOG_MAIN"
PORT=8001 python main.py >> "$LOG_MAIN" 2>&1 &
PID_MAIN=$!
log "    PID: $PID_MAIN"

# Wait for main app to bind
wait_for_http "http://127.0.0.1:8001/health" "Main App" 30 || true

# ── 3. Gateway Proxy (port 8000) — public-facing ──────────────────────────
# Start the gateway BEFORE the orchestrator so the orchestrator can use
# http://localhost:8000/forecast as its TimesFM URL (which proxies to 8002).
log ""
log "  [3/4] Starting Gateway Proxy (port 8000)..."
log "    Log: $LOG_GATEWAY"
PORT=8000 python proxy.py >> "$LOG_GATEWAY" 2>&1 &
PID_GATEWAY=$!
log "    PID: $PID_GATEWAY"

# ── 4. Wait for TimesFM model to finish loading ───────────────────────────
# This is the critical step. The orchestrator must not start until the
# TimesFM model is loaded, otherwise the first sweep cycle fails.
log ""
log "  Waiting for TimesFM model to finish loading before starting orchestrator..."
wait_for_timesfm_ready "$TIMESFM_READY_TIMEOUT" || true

# ── 5. Sweep Orchestrator (port 8003) — START LAST ────────────────────────
log ""
log "  [4/4] Starting Sweep Orchestrator (port 8003)..."
log "    Log: $LOG_ORCH"
SWEEP_ORCHESTRATOR_PORT=8003 python -m regime_platform.sweep_orchestrator >> "$LOG_ORCH" 2>&1 &
PID_ORCH=$!
log "    PID: $PID_ORCH"

# ── Final summary ──────────────────────────────────────────────────────────
log ""
log "  Main app PID:  $PID_MAIN  → log: $LOG_MAIN"
log "  TimesFM PID:   $PID_TIMESFM  → log: $LOG_TIMESFM"
log "  Orch PID:      $PID_ORCH  → log: $LOG_ORCH"
log "  Gateway PID:   $PID_GATEWAY  → log: $LOG_GATEWAY"
log "  Public URL:    https://8000-$(hostname -s 2>/dev/null || echo '...').cloudspaces.litng.ai"
log ""

# Quick health summary
if command -v curl &>/dev/null; then
    log "  Health summary:"
    main_h=$(curl -sf http://127.0.0.1:8001/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "DOWN")
    tfm_h=$(curl -sf http://127.0.0.1:8002/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "DOWN")
    gw_h=$(curl -sf http://127.0.0.1:8000/health 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "DOWN")
    log "    Main App    : $main_h"
    log "    TimesFM     : $tfm_h"
    log "    Gateway     : $gw_h"
fi

log ""
log "  Services running in background."
log ""
log "  ── How to view logs ──────────────────────────────────────────────"
log "  All logs:     tail -f $LOG_DIR/*.log"
log "  Orchestrator: tail -f $LOG_ORCH"
log "  TimesFM:      tail -f $LOG_TIMESFM"
log "  Main app:     tail -f $LOG_MAIN"
log "  Gateway:      tail -f $LOG_GATEWAY"
log "  Startup:      tail -f $LOG_STARTUP"
log "═══════════════════════════════════════════════════════════════════"

cleanup() {
    log "on_start.sh: Shutting down services..."
    kill $PID_MAIN $PID_TIMESFM $PID_ORCH $PID_GATEWAY 2>/dev/null
    wait
}
trap cleanup EXIT INT TERM

wait
