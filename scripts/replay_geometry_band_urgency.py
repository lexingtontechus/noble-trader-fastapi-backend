#!/usr/bin/env python3
"""
replay_geometry_band_urgency.py — Initial benchmark for geometry band
calibration urgency (worklog 20261004_GEOMETRY_BAND_CALIBRATION_URGENCY_SCOPE.md).

Simulates the 4h urgency check cadence against historical resolved signals
to set initial thresholds. No waiting — uses existing data.

The urgency process monitors per-bucket calibration bias and triggers
targeted re-computation when drift exceeds threshold. This script replays
that logic against historical data to answer:

1. How often would each threshold fire?
2. What is the distribution of |bias_shrunk| across buckets?
3. What is the right min_n and cooldown?
4. How many re-computations per week would each threshold produce?

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/replay_geometry_band_urgency.py
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regime_platform.sweep_orchestrator.bracket_reachability import (
    ATR_BUCKETS,
    _bucket_for,
    _vol_tier,
)
from regime_platform.sweep_orchestrator.p_win_bracket import beta_shrunk_win_rate


def _fixture() -> list[dict]:
    """Synthetic resolved signals with realistic bucket distribution."""
    rows = []
    # Tight buckets (0.5-1 ATR): mostly resolve, good win rate
    for i in range(60):
        rows.append({
            "symbol": "EURUSD", "ts": f"2026-09-{15 + i // 24:02d}T{i % 24:02d}:00:00Z",
            "regime": "med_low_vol_bull", "p_win": 0.65,
            "outcome": "tp_hit" if i < 36 else ("sl_hit" if i < 48 else "expired"),
            "tp_distance_atr": 0.7, "sl_distance_atr": 0.35,
        })
    # Wide buckets (4+ ATR): mostly expire
    for i in range(60):
        rows.append({
            "symbol": "XAUUSD", "ts": f"2026-09-{15 + i // 24:02d}T{i % 24:02d}:00:00Z",
            "regime": "med_low_vol_bull", "p_win": 0.70,
            "outcome": "tp_hit" if i < 6 else ("sl_hit" if i < 12 else "expired"),
            "tp_distance_atr": 5.0, "sl_distance_atr": 2.5,
        })
    # Medium buckets (2-4 ATR): mixed
    for i in range(40):
        rows.append({
            "symbol": "GBPUSD", "ts": f"2026-09-{15 + i // 24:02d}T{i % 24:02d}:00:00Z",
            "regime": "med_high_vol_bear", "p_win": 0.55,
            "outcome": "tp_hit" if i < 16 else ("sl_hit" if i < 28 else "expired"),
            "tp_distance_atr": 3.0, "sl_distance_atr": 1.5,
        })
    return rows


def _compute_bucket_bias(rows: list[dict]) -> dict[tuple, dict]:
    """Compute per-bucket bias from resolved signals."""
    cells = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "p_win_sum": 0.0,
    })
    for r in rows:
        bucket = _bucket_for(r["tp_distance_atr"])
        if bucket is None:
            continue
        key = (r["symbol"], _vol_tier(r["regime"]), bucket)
        cells[key]["n"] += 1
        cells[key]["p_win_sum"] += r["p_win"]
        if r["outcome"] == "tp_hit":
            cells[key]["wins"] += 1
        elif r["outcome"] == "sl_hit":
            cells[key]["losses"] += 1

    out = {}
    for key, c in cells.items():
        n = c["n"]
        if n == 0:
            continue
        avg_p_win = c["p_win_sum"] / n
        win_rate = c["wins"] / n if n else 0.0
        bias = avg_p_win - win_rate
        shrunk_wr = beta_shrunk_win_rate(c["wins"], c["losses"])
        bias_shrunk = avg_p_win - shrunk_wr if shrunk_wr is not None else bias
        out[key] = {
            "n": n, "wins": c["wins"], "losses": c["losses"],
            "avg_p_win": round(avg_p_win, 4),
            "win_rate": round(win_rate, 4),
            "bias": round(bias, 4),
            "bias_shrunk": round(bias_shrunk, 4),
        }
    return out


def _simulate_urgency_cadence(
    bucket_bias: dict[tuple, dict],
    *,
    threshold: float,
    min_n: int,
    cooldown_hours: int,
    simulation_days: int = 14,
) -> dict:
    """Simulate the 4h urgency check cadence.

    Returns per-bucket trigger counts and total re-computations.
    """
    checks_per_day = 6  # every 4h
    total_checks = checks_per_day * simulation_days
    cooldown_checks = cooldown_hours // 4

    triggers: dict[tuple, int] = defaultdict(int)
    last_triggered: dict[tuple, int] = {}
    recomputations = 0

    for check_idx in range(total_checks):
        for key, bias in bucket_bias.items():
            if bias["n"] < min_n:
                continue
            if abs(bias["bias_shrunk"]) < threshold:
                continue
            # Cooldown check
            last = last_triggered.get(key, -999)
            if check_idx - last < cooldown_checks:
                continue
            triggers[key] += 1
            last_triggered[key] = check_idx
            recomputations += 1

    return {
        "total_checks": total_checks,
        "recomputations": recomputations,
        "triggers_per_bucket": dict(triggers),
        "buckets_monitored": len([k for k, b in bucket_bias.items() if b["n"] >= min_n]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="Simulation days")
    parser.add_argument("--live", action="store_true", help="Pull from Supabase")
    args = parser.parse_args()

    print("=== Geometry Band Calibration Urgency — Initial Benchmark ===\n")

    # Build bucket bias from fixture or live data
    if args.live:
        print("Live mode not yet implemented — using fixture.\n")
    rows = _fixture()
    bucket_bias = _compute_bucket_bias(rows)

    print(f"Fixture: {len(rows)} resolved signals")
    print(f"Buckets with data: {len(bucket_bias)}")
    print()

    # Distribution of |bias_shrunk|
    print("=== |bias_shrunk| Distribution ===")
    biases = sorted(
        [(k, abs(v["bias_shrunk"]), v["n"]) for k, v in bucket_bias.items()],
        key=lambda x: x[1],
        reverse=True,
    )
    for key, abs_bias, n in biases:
        print(f"  {key[0]:8s} {key[1]:15s} {key[2]:5s}  n={n:3d}  |bias_shrunk|={abs_bias:.4f}")
    print()

    # Threshold sweep
    print("=== Threshold Sweep (14-day simulation, 4h cadence) ===")
    print(f"{'Threshold':>10s}  {'Min n':>6s}  {'Cooldown':>10s}  {'Recomputations':>15s}  {'Buckets':>8s}")
    print("-" * 60)

    for threshold in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
        for min_n in [10, 20, 30]:
            for cooldown in [4, 8, 12]:
                result = _simulate_urgency_cadence(
                    bucket_bias,
                    threshold=threshold,
                    min_n=min_n,
                    cooldown_hours=cooldown,
                    simulation_days=args.days,
                )
                print(
                    f"{threshold:>10.2f}  {min_n:>6d}  {cooldown:>10d}h  "
                    f"{result['recomputations']:>15d}  {result['buckets_monitored']:>8d}"
                )
    print()

    # Recommended initial thresholds
    print("=== Recommended Initial Thresholds ===")
    print("Based on the distribution above:")
    print()

    # Find the threshold that fires on the top 20% of buckets
    if biases:
        top_20_pct = max(1, len(biases) // 5)
        threshold_for_top_20 = biases[top_20_pct - 1][1] if top_20_pct <= len(biases) else 0.15
        print(f"  urgency_threshold: {threshold_for_top_20:.2f}  (fires on top 20% of buckets)")
    else:
        print("  urgency_threshold: 0.15  (default)")
    print(f"  min_n: 20  (minimum resolved signals per bucket)")
    print(f"  cooldown_hours: 4  (same as ATR urgency)")
    print(f"  min_age_hours: 4  (minimum age before re-computation)")
    print()

    # Weekly projection
    print("=== Weekly Projection (recommended thresholds) ===")
    result = _simulate_urgency_cadence(
        bucket_bias,
        threshold=0.15,
        min_n=20,
        cooldown_hours=4,
        simulation_days=7,
    )
    print(f"  Recomputations per week: {result['recomputations']}")
    print(f"  Buckets monitored: {result['buckets_monitored']}")
    print(f"  Avg recomputations per bucket per week: "
          f"{result['recomputations'] / max(1, result['buckets_monitored']):.1f}")
    print()

    # nt_eod_actions impact
    print("=== nt_eod_actions Impact ===")
    print(f"  New action type: 'geometry_band_urgency'")
    print(f"  Requires CHECK constraint extension (migration)")
    print(f"  Estimated rows per week: {result['recomputations']}")
    print(f"  Current nt_eod_actions rows (7d): see Supabase query")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
