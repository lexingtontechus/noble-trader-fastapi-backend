#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Noble Trader Stack — Single-command launcher
#
# Starts four services on the same machine:
#   Port 8001  – Main app    (HMM/Renko pipeline, auth, etc.)
#   Port 8002  – TimesFM     (Google TimesFM 2.5 forecast API)
#   Port 8003  – Sweep Orch  (APScheduler-based sweep orchestrator)
#   Port 8000  – Gateway     (reverse proxy – this is the public-facing port)
#
# Usage:
#   chmod +x start.sh
#   ./start.sh
#
# Environment variables (set in Lightning Studio → Secrets):
#   TIMESFM_API_KEYS     – Comma-separated API keys for /forecast (required)
#   TIMESFM_MODEL_ID     – HF model ID (default: google/timesfm-2.5-200m-pytorch)
#   TIMESFM_MAX_CONTEXT  – Max input length (default: 1024)
#   TIMESFM_MAX_HORIZON  – Max forecast horizon (default: 256)
#   RATE_LIMIT_PER_MIN   – TimesFM rate limit per key (default: 60)
#   PORT                 – Override public port (default: 8000)
#
# Startup behavior:
#   TimesFM is started FIRST (model load takes 30-60s, or 2-3 min on first
#   run when HuggingFace weights need to download). The script waits for
#   TimesFM's /health endpoint to return status="ok" (model loaded) before
#   starting the sweep orchestrator. This prevents the orchestrator's first
#   sweep cycle from hitting a 503 and falling into "TimesFM unavailable"
#   fallback mode.
# ═══════════════════════════════════════════════════════════════════════════════

set -e

PUBLIC_PORT="${PORT:-8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Config ─────────────────────────────────────────────────────────────────
# How long to wait for TimesFM model to load before giving up and starting
# the orchestrator anyway (it'll retry on its own). Default: 180s.
# First-ever run may need 300s+ (HF weight download). Subsequent runs
# typically load in 30-60s.
TIMESFM_READY_TIMEOUT="${TIMESFM_READY_TIMEOUT:-180}"

echo "═══════════════════════════════════════════════════════════════════════"
echo "  Noble Trader Stack — starting up"
echo "  Public port:  $PUBLIC_PORT"
echo "  Main app:     http://127.0.0.1:8001"
echo "  TimesFM:      http://127.0.0.1:8002"
echo "  Sweep Orch:   http://127.0.0.1:8003"
echo "  Gateway:      http://127.0.0.1:$PUBLIC_PORT"
echo "═══════════════════════════════════════════════════════════════════════"

# ── Cleanup handler ────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Shutting down all services..."
    kill "$ORCH_PID" 2>/dev/null || true
    kill "$MAIN_PID" 2>/dev/null || true
    kill "$TIMESFM_PID" 2>/dev/null || true
    kill "$GATEWAY_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    echo "All services stopped."
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── Helper: wait for a URL to return HTTP 200 ──────────────────────────────
# Args: url label timeout_seconds
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

# ── Helper: wait for TimesFM model to be fully loaded ─────────────────────
# TimesFM's /health returns {"status":"loading"} while the model is
# loading and {"status":"ok"} once it's ready. We need to check the JSON
# body, not just the HTTP status code (which is 200 in both cases).
# Args: timeout_seconds
wait_for_timesfm_ready() {
    local timeout="$1"
    local elapsed=0
    echo "  Waiting for TimesFM model to load (timeout: ${timeout}s)..."
    while [ "$elapsed" -lt "$timeout" ]; do
        # Check if the process is still alive
        if ! kill -0 "$TIMESFM_PID" 2>/dev/null; then
            echo "  ✗ TimesFM process died during model load!"
            return 1
        fi
        # Check health endpoint — parse JSON status field
        health_json=$(curl -sf http://127.0.0.1:8002/health 2>/dev/null || echo "")
        if [ -n "$health_json" ]; then
            status=$(echo "$health_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
            if [ "$status" = "ok" ]; then
                echo "  ✓ TimesFM model loaded (after ${elapsed}s)"
                return 0
            elif [ "$status" = "loading" ]; then
                # Still loading — show progress
                if [ $((elapsed % 10)) -eq 0 ] && [ "$elapsed" -gt 0 ]; then
                    echo "    ...still loading (${elapsed}s elapsed)"
                fi
            fi
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "  ⚠ TimesFM model not ready after ${timeout}s — starting orchestrator anyway"
    echo "    (orchestrator will retry TimesFM on each sweep cycle and recover automatically)"
    return 1
}

# ── 1. TimesFM Server (port 8002) — START FIRST ───────────────────────────
# TimesFM is the slowest service to start (model download + load can take
# 2-3 minutes on first run, 30-60s on subsequent runs). Start it first so
# the model is loading in parallel while we start the other services.
echo ""
echo "[1/4] Starting TimesFM on port 8002..."
PORT=8002 python server.py &
TIMESFM_PID=$!
echo "  PID: $TIMESFM_PID"
echo "  (model load happens in background — will wait for it below)"

# ── 2. Main App (port 8001) ───────────────────────────────────────────────
echo ""
echo "[2/4] Starting Main App on port 8001..."
PORT=8001 python main.py &
MAIN_PID=$!
echo "  PID: $MAIN_PID"

# ── Wait for main app to bind ──────────────────────────────────────────────
echo ""
echo "Waiting for Main App to bind..."
wait_for_http "http://127.0.0.1:8001/health" "Main App" 30 || true

# ── 3. Wait for TimesFM model to finish loading ───────────────────────────
# This is the critical step that was missing. The orchestrator must NOT
# start until TimesFM's model is loaded, otherwise the first sweep cycle
# will get a 503 (model still loading) and fall into fallback mode.
echo ""
echo "[3/4] Waiting for TimesFM model to finish loading..."
wait_for_timesfm_ready "$TIMESFM_READY_TIMEOUT" || true

# ── 4. Sweep Orchestrator (port 8003) ─────────────────────────────────────
# Now that TimesFM is ready (or we timed out), start the orchestrator.
# If TimesFM isn't ready yet, the orchestrator will still start and retry
# TimesFM on each sweep cycle (every ~5 minutes per symbol group).
echo ""
echo "[4/5] Starting Sweep Orchestrator on port 8003..."
SWEEP_ORCHESTRATOR_PORT=8003 python -m regime_platform.sweep_orchestrator &
ORCH_PID=$!
echo "  PID: $ORCH_PID"

# ── 5. Gateway / Proxy (public port) ──────────────────────────────────────
echo ""
echo "[5/5] Starting Gateway on port $PUBLIC_PORT..."
PORT="$PUBLIC_PORT" python proxy.py &
GATEWAY_PID=$!
echo "  PID: $GATEWAY_PID"

# ── Final health summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "  All services started. Health summary:"
if command -v curl &>/dev/null; then
    # Main app
    main_health=$(curl -sf http://127.0.0.1:8001/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "DOWN")
    echo "    Main App    : $main_health"

    # TimesFM
    timesfm_health=$(curl -sf http://127.0.0.1:8002/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "DOWN")
    echo "    TimesFM     : $timesfm_health"

    # Gateway
    gw_health=$(curl -sf http://127.0.0.1:$PUBLIC_PORT/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "DOWN")
    echo "    Gateway     : $gw_health"
fi
echo ""
echo "  Gateway is live at http://0.0.0.0:$PUBLIC_PORT"
echo "  Press Ctrl+C to stop all services."
echo "═══════════════════════════════════════════════════════════════════════"

# Wait for any child to exit (or Ctrl+C)
wait
