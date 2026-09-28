#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Noble Trader — Lightning AI Studio on_stop.sh
#
# This script runs automatically when the Studio stops/sleeps.
# Cleans up background services gracefully to prevent data corruption.
#
# Place in: /teamspace/studios/this_studio/noble-trader/on_stop.sh
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "═══════════════════════════════════════════════════════════════════"
echo "  Noble Trader — on_stop.sh"
echo "  Gracefully shutting down services..."
echo "═══════════════════════════════════════════════════════════════════"

# ── Send SIGTERM to all Noble Trader services ────────────────
# SIGTERM allows clean shutdown (flush Redis, close connections, etc.)
for proc_name in \
    "python main.py" \
    "python server.py" \
    "python llm_server.py" \
    "python proxy.py" \
    "python -m regime_platform.sweep_orchestrator"
do
    PIDS=$(pgrep -f "$proc_name" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "  Stopping: $proc_name (PIDs: $(echo $PIDS | tr '\n' ' '))"
        kill -TERM $PIDS 2>/dev/null || true
    fi
done

# ── Wait for graceful shutdown ───────────────────────────────
sleep 3

# ── Force kill any remaining processes ───────────────────────
for proc_name in \
    "python main.py" \
    "python server.py" \
    "python llm_server.py" \
    "python proxy.py" \
    "python -m regime_platform.sweep_orchestrator"
do
    PIDS=$(pgrep -f "$proc_name" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "  Force killing: $proc_name (PIDs: $(echo $PIDS | tr '\n' ' '))"
        kill -KILL $PIDS 2>/dev/null || true
    fi
done

echo "  All services stopped."
echo "═══════════════════════════════════════════════════════════════════"
