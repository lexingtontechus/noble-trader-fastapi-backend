#!/usr/bin/env python3
"""
24-hour EV Fix Evaluation — reads monitor delivery log + Supabase data.

Produces a summary table showing per-symbol calibration bias trends, raw_p_win
evolution, and regression events over the monitoring period.
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DELIVERY_LOG = REPO_ROOT / "scripts" / "monitor_ev_fix_delivery.log"
RESULTS_FILE = REPO_ROOT / "scripts" / "monitor_ev_fix_results.json"

SYMBOLS = [
    "AUDUSD", "BTCUSD", "EURUSD", "GBPUSD", "USDCAD",
    "USDCHF", "USDJPY", "US30USD", "US500",
    "XAGUSD", "XAUAUD", "XAUEUR", "XAUUSD", "AAPL",
]


def parse_delivery_log() -> list[dict]:
    """Parse the monitor delivery log into structured entries."""
    entries = []
    if not DELIVERY_LOG.exists():
        return entries
    with open(DELIVERY_LOG) as f:
        for line in f:
            line = line.strip()
            if '|statuses=' not in line:
                continue
            parts = line.split('|', 1)
            timestamp = parts[0]
            statuses_json = parts[1].split('=', 1)[1]
            statuses = json.loads(statuses_json)
            entries.append({'timestamp': timestamp, 'statuses': statuses})
    return entries


def parse_monitor_results() -> dict:
    """Load the latest monitor results JSON."""
    if not RESULTS_FILE.exists():
        return {}
    with open(RESULTS_FILE) as f:
        return json.load(f)


def evaluate_24h() -> None:
    entries = parse_delivery_log()
    if not entries:
        print("No monitoring data found in delivery log.")
        return

    # Determine time range
    first_ts = entries[0]['timestamp']
    last_ts = entries[-1]['timestamp']
    print(f"=== 24-HOUR EV FIX MONITOR EVALUATION ===")
    print(f"Monitoring period: {first_ts} to {last_ts}")
    print(f"Total checks: {len(entries)}")
    print()

    # Status distribution
    all_statuses = {}
    for e in entries:
        for sym, status in e['statuses'].items():
            all_statuses[status] = all_statuses.get(status, 0) + 1

    print("Status distribution (all symbols × all checks):")
    total = sum(all_statuses.values())
    for s in sorted(all_statuses.keys()):
        pct = all_statuses[s] / total * 100 if total else 0
        print(f"  {s}: {all_statuses[s]} ({pct:.1f}%)")
    print()

    # Per-symbol breakdown
    print("Per-symbol status history:")
    print(f"{'Symbol':<10} {'DEGEN':>6} {'MOD':>6} {'NEG':>6} {'MAR':>6} {'FAIL':>6} {'total':>6}")
    print("-" * 55)
    for sym in SYMBOLS:
        counts = defaultdict(int)
        for e in entries:
            status = e['statuses'].get(sym, 'NO_DATA')
            counts[status] += 1
        total = sum(counts.values())
        print(f"{sym:<10} {counts['FIX_WORKING_DEGENERATE']:>6} "
              f"{counts['FIX_WORKING_MODERATE']:>6} "
              f"{counts['FIX_WORKING_NEGATIVE']:>6} "
              f"{counts['FIX_WORKING_MARGINAL']:>6} "
              f"{counts['FIX_NOT_WORKING']:>6} {total:>6}")
    print()

    # Regression check
    regressions = []
    for i, e in enumerate(entries):
        for sym in SYMBOLS:
            status = e['statuses'].get(sym, '')
            if status == 'FIX_NOT_WORKING':
                prev = entries[i-1]['statuses'].get(sym, '') if i > 0 else ''
                regressions.append({
                    'timestamp': e['timestamp'],
                    'symbol': sym,
                    'from': prev,
                    'to': status,
                })

    if regressions:
        print(f"=== REGRESSIONS DETECTED: {len(regressions)} ===")
        for r in regressions:
            print(f"  {r['timestamp']} {r['symbol']}: {r['from']} → {r['to']}")
    else:
        print("=== REGRESSIONS: NONE ===")
        print(f"0 FIX_NOT_WORKING events across {len(entries)} checks × 14 symbols = {len(entries)*14} total samples")
    print()

    # Latest snapshot
    results = parse_monitor_results()
    if isinstance(results, list):
        results = results[-1] if results else {}

    latest_entry = entries[-1]['statuses']
    print("=== Latest Monitor Snapshot ===")
    print(f"{'Symbol':<10} {'raw_p_win':>10} {'cal_p_win':>10} {'qualified':>10} {'bias':>8} {'status':<25}")
    print("-" * 75)
    for sym in SYMBOLS:
        if isinstance(results, dict) and 'symbols' in results:
            info = results.get('symbols', {}).get(sym, {})
        else:
            info = results.get(sym, {})
        raw = info.get('raw_p_win', 'N/A')
        cal = info.get('calibrated_p_win', 'N/A')
        qual = '✓' if info.get('qualified') else '✗'
        bias_val = info.get('bias', 'N/A')
        status = info.get('fix_status', latest_entry.get(sym, 'N/A'))

        raw_str = f"{raw:.4f}" if isinstance(raw, (int, float)) else str(raw)
        cal_str = f"{cal:.4f}" if isinstance(cal, (int, float)) else str(cal)
        bias_str = f"{bias_val:.4f}" if isinstance(bias_val, (int, float)) else str(bias_val)

        print(f"{sym:<10} {raw_str:>10} {cal_str:>10} {qual:>10} {bias_str:>8} {status:<25}")
    print()

    # Notable trends
    print("=== Notable Trends ===")
    # Find symbols that changed status over the period
    for sym in SYMBOLS:
        statuses_over_time = [e['statuses'].get(sym, '') for e in entries]
        unique = set(statuses_over_time)
        unique.discard('NO_DATA')
        if len(unique) > 1:
            print(f"  {sym}: {statuses_over_time[0]} → {statuses_over_time[-1]}")
            # Highlight transitions from NEGATIVE to MODERATE or DEGENERATE to MODERATE
            transitions = []
            for i in range(1, len(statuses_over_time)):
                if statuses_over_time[i] != statuses_over_time[i-1]:
                    transitions.append(f"{statuses_over_time[i-1]}→{statuses_over_time[i]} @{entries[i]['timestamp']}")
            if transitions:
                print(f"    transitions: {', '.join(transitions[:3])}{'...' if len(transitions) > 3 else ''}")


if __name__ == "__main__":
    evaluate_24h()
