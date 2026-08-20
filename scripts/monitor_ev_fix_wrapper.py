#!/usr/bin/env python3
"""
EV Engine Fix Monitor — cron wrapper (no_agent mode).

This wrapper runs monitor_ev_fix.py --once, captures its output, and returns
ONLY the lines that indicate a regression or status change — silent when
everything is healthy (silent output = no delivery in no_agent cron).

Regression conditions:
  - Any symbol classified as FIX_NOT_WORKING (raw_p_win > 0.98)
  - Any symbol that was previously FIX_WORKING but is now FIX_NOT_WORKING

Usage: invoked by Hermes cron job (job_id=3dbc7c566323, schedule */5 * * * *)
Cron runs Python (.py extension) via Python interpreter — no bash wrapper needed.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = "C:/Users/aloys/OneDrive/Documents/GitHub/noble-trader-workspace/noble-trader-fastapi-backend"
MONITOR_SCRIPT = os.path.join(REPO_ROOT, "scripts", "monitor_ev_fix.py")
RESULTS_FILE = os.path.join(REPO_ROOT, "scripts", "monitor_ev_fix_results.json")
DELIVERY_LOG = os.path.join(REPO_ROOT, "scripts", "monitor_ev_fix_delivery.log")


def load_last_results() -> dict:
    """Load the previous run's results to detect status changes."""
    try:
        with open(RESULTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_fix_statuses(output: str) -> dict[str, str]:
    """Extract symbol → fix_status from monitor output."""
    statuses: dict[str, str] = {}
    lines = output.strip().splitlines()
    for line in lines:
        parts = line.split()
        if len(parts) >= 8 and parts[0] in (
            "AUDUSD", "BTCUSD", "EURUSD", "GBPUSD", "USDCAD",
            "USDCHF", "USDJPY", "US30USD", "US500",
            "XAGUSD", "XAUAUD", "XAUEUR", "XAUUSD", "AAPL",
        ):
            sym = parts[0]
            status = parts[-1]
            if status.startswith("FIX_"):
                statuses[sym] = status
    return statuses


def _get_venv_python() -> str:
    """Find the venv Python executable in a platform-aware way.
    
    Tries .venv/Scripts/python.exe (Windows), .venv/bin/python (Linux/macOS),
    then falls back to bare 'python3' or 'python' on PATH.
    """
    # Windows venv
    win_venv = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
    if os.path.isfile(win_venv):
        return win_venv
    # Unix venv
    unix_venv = os.path.join(REPO_ROOT, ".venv", "bin", "python3")
    if os.path.isfile(unix_venv):
        return unix_venv
    unix_venv2 = os.path.join(REPO_ROOT, ".venv", "bin", "python")
    if os.path.isfile(unix_venv2):
        return unix_venv2
    # Fallback to system Python
    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return found
    return "python3"  # ultimate fallback


def main() -> int:
    # Ensure PYTHONPATH is set for venv imports
    env = os.environ.copy()
    src_path = os.path.join(REPO_ROOT, "src")
    env["PYTHONPATH"] = src_path

    python_exe = _get_venv_python()

    # Run the monitor in --once mode
    result = subprocess.run(
        [
            python_exe,
            MONITOR_SCRIPT,
            "--once",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=env,
    )

    output = result.stdout
    # If stdout is empty but stderr has content, surface it for debugging
    if not output:
        if result.stderr:
            print(f"MONITOR stderr: {result.stderr}", file=sys.stderr)
        print(f"MONITOR produced no output (exit code {result.returncode}, "
              f"python: {python_exe}). Skipping.", file=sys.stderr)
        return 1

    # Parse the current statuses
    current_statuses = parse_fix_statuses(output)
    if not current_statuses:
        print("Could not parse fix statuses from monitor output.", file=sys.stderr)
        return 2

    # Load previous statuses for comparison
    last_results = load_last_results()
    prev_statuses: dict[str, str] = {}
    if isinstance(last_results, dict) and "symbols" in last_results:
        for sym, data in last_results["symbols"].items():
            if isinstance(data, dict) and "fix_status" in data:
                prev_statuses[sym] = data["fix_status"]

    # Check for regressions
    regressions = []
    for sym, status in current_statuses.items():
        if status == "FIX_NOT_WORKING":
            prev = prev_statuses.get(sym, "")
            if prev in ("", "FIX_WORKING_DEGENERATE", "FIX_WORKING_MODERATE",
                        "FIX_WORKING_STRONG", "FIX_WORKING_NEGATIVE", "FIX_WORKING_MARGINAL"):
                regressions.append(f"REGRESSION: {sym} → {status} (was {prev})")
            else:
                regressions.append(f"ALERT: {sym} → {status}")

    # If there are regressions, print the full output for delivery
    if regressions:
        print(f"EV ENGINE FIX MONITOR — REGRESSIONS DETECTED")
        print("=" * 60)
        for r in regressions:
            print(r)
        print("=" * 60)
        print()
        print(output)
        return 0

    # No regressions — log status for persistence, but stay silent
    # (no_agent cron: empty stdout = no delivery)
    with open(DELIVERY_LOG, "a") as f:
        timestamp = output.split("EV Engine Fix Monitor — ")
        ts_str = timestamp[1].split("\n")[0] if len(timestamp) > 1 else "unknown"
        f.write(f"{ts_str}|statuses={json.dumps(current_statuses)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
