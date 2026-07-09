#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# Noble Trader — Log Viewer
#
# Tail service logs in real-time. Run from anywhere — it auto-detects the
# log directory relative to the repo.
#
# Usage:
#   ./logs.sh                  # tail all logs
#   ./logs.sh orchestrator     # tail only orchestrator
#   ./logs.sh timesfm          # tail only TimesFM
#   ./logs.sh main             # tail only main app
#   ./logs.sh gateway          # tail only gateway
#   ./logs.sh startup          # tail only startup log
#
# Or use tail directly:
#   tail -f logs/orchestrator.log
#   tail -f logs/*.log
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"

if [ ! -d "$LOG_DIR" ]; then
    echo "Log directory not found: $LOG_DIR"
    echo "Have the services been started? Run on_start.sh or background.py first."
    exit 1
fi

TARGET="${1:-all}"

case "$TARGET" in
    all)
        echo "Tailing ALL logs in $LOG_DIR/ (Ctrl+C to stop)"
        echo "═════════════════════════════════════════════════"
        tail -f "$LOG_DIR"/*.log
        ;;
    orch|orchestrator)
        tail -f "$LOG_DIR/orchestrator.log"
        ;;
    timesfm)
        tail -f "$LOG_DIR/timesfm.log"
        ;;
    main)
        tail -f "$LOG_DIR/main.log"
        ;;
    gateway|proxy)
        tail -f "$LOG_DIR/gateway.log"
        ;;
    startup)
        tail -f "$LOG_DIR/startup.log"
        ;;
    list|ls)
        echo "Available log files in $LOG_DIR/:"
        ls -lh "$LOG_DIR"/*.log 2>/dev/null || echo "  (no log files found)"
        ;;
    *)
        echo "Usage: $0 [all|orchestrator|timesfm|main|gateway|startup|list]"
        echo ""
        echo "Available logs:"
        ls -1 "$LOG_DIR"/*.log 2>/dev/null | sed 's|.*/||;s|\.log||;s|^|  |' || echo "  (none)"
        exit 1
        ;;
esac
