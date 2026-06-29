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
# ═══════════════════════════════════════════════════════════════════════════════

set -e

PUBLIC_PORT="${PORT:-8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

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

# ── 1. Main App (port 8001) ────────────────────────────────────────────────
echo ""
echo "[1/3] Starting Main App on port 8001..."
PORT=8001 python main.py &
MAIN_PID=$!
echo "  PID: $MAIN_PID"

# ── 2. TimesFM Server (port 8002) ──────────────────────────────────────────
echo ""
echo "[2/3] Starting TimesFM on port 8002..."
PORT=8002 python server.py &
TIMESFM_PID=$!
echo "  PID: $TIMESFM_PID"

# ── Wait for both to bind ──────────────────────────────────────────────────
echo ""
echo "Waiting for services to bind..."
sleep 8

# Quick health checks (non-blocking — don't fail startup)
if command -v curl &>/dev/null; then
    curl -sf http://127.0.0.1:8001/health >/dev/null 2>&1 && \
        echo "  ✓ Main app is healthy" || \
        echo "  ⚠ Main app not responding yet (may still be starting)"

    curl -sf http://127.0.0.1:8002/health >/dev/null 2>&1 && \
        echo "  ✓ TimesFM is healthy" || \
        echo "  ⚠ TimesFM not responding yet (model may still be loading — this is normal)"
fi

# ── 3. Sweep Orchestrator (port 8003) ───────────────────────────────────────
echo ""
echo "[3/4] Starting Sweep Orchestrator on port 8003..."
SWEEP_ORCHESTRATOR_PORT=8003 python -m regime_platform.sweep_orchestrator &
ORCH_PID=$!
echo "  PID: $ORCH_PID"

# ── 4. Gateway / Proxy (public port) ───────────────────────────────────────
echo ""
echo "[4/4] Starting Gateway on port $PUBLIC_PORT..."
PORT="$PUBLIC_PORT" python proxy.py &
GATEWAY_PID=$!
echo "  PID: $GATEWAY_PID"

echo ""
echo "═══════════════════════════════════════════════════════════════════════"
echo "  All 4 services running. Gateway is live at http://0.0.0.0:$PUBLIC_PORT"
echo "  Press Ctrl+C to stop all services."
echo "═══════════════════════════════════════════════════════════════════════"

# Wait for any child to exit (or Ctrl+C)
wait
