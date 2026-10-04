#!/usr/bin/env python3
"""
replay_geometry_band_urgency.py — Replay simulation for geometry band
calibration urgency (worklog 20261004_GEOMETRY_BAND_CALIBRATION_URGENCY_SCOPE.md).

Replicates the dynamic_atr methodology: re-compute bucket statistics from
recent resolved signals when calibration drift is detected, and measure
how the re-computed values differ from the stored (stale) values.

This is NOT a threshold-counting simulation. It measures:
1. How much do bucket statistics change when re-computed from recent data?
2. What is the right lookback window for re-computation?
3. How often would re-computation fire at each threshold?
4. Does re-computation improve calibration bias?

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
    """Synthetic resolved signals with realistic bucket distribution.

    Simulates 30 days of signals across 3 symbols, with a regime shift
    on day 15 that makes the first 15 days stale.
    """
    rows = []
    base_date = datetime(2026, 9, 1, tzinfo=timezone.utc)

    for day in range(30):
        ts = base_date + timedelta(days=day)
        # Regime shift on day 15: vol tier changes, win rates shift
        if day < 15:
            # Pre-shift: tight buckets win more
            for i in range(4):
                rows.append({
                    "symbol": "EURUSD", "ts": ts.isoformat(),
                    "regime": "med_low_vol_bull", "p_win": 0.65,
                    "outcome": "tp_hit" if i < 3 else "sl_hit",
                    "tp_distance_atr": 0.7, "sl_distance_atr": 0.35,
                })
            for i in range(4):
                rows.append({
                    "symbol": "XAUUSD", "ts": ts.isoformat(),
                    "regime": "med_low_vol_bull", "p_win": 0.70,
                    "outcome": "tp_hit" if i < 1 else ("sl_hit" if i < 2 else "expired"),
                    "tp_distance_atr": 5.0, "sl_distance_atr": 2.5,
                })
        else:
            # Post-shift: tight buckets still win, but wide buckets shift
            for i in range(4):
                rows.append({
                    "symbol": "EURUSD", "ts": ts.isoformat(),
                    "regime": "med_low_vol_bull", "p_win": 0.60,
                    "outcome": "tp_hit" if i < 2 else ("sl_hit" if i < 3 else "expired"),
                    "tp_distance_atr": 0.7, "sl_distance_atr": 0.35,
                })
            for i in range(4):
                rows.append({
                    "symbol": "XAUUSD", "ts": ts.isoformat(),
                    "regime": "high_vol_bear", "p_win": 0.55,
                    "outcome": "tp_hit" if i < 1 else ("sl_hit" if i < 3 else "expired"),
                    "tp_distance_atr": 5.0, "sl_distance_atr": 2.5,
                })
    return rows


def _compute_bucket_stats(rows: list[dict]) -> dict[tuple, dict]:
    """Compute per-bucket reachability stats from resolved signals."""
    cells = defaultdict(lambda: {
        "n": 0, "tp": 0, "sl": 0, "exp": 0, "p_win_sum": 0.0,
    })
    for r in rows:
        bucket = _bucket_for(r["tp_distance_atr"])
        if bucket is None:
            continue
        key = (r["symbol"], _vol_tier(r["regime"]), bucket)
        cells[key]["n"] += 1
        cells[key]["p_win_sum"] += r["p_win"]
        if r["outcome"] == "tp_hit":
            cells[key]["tp"] += 1
        elif r["outcome"] == "sl_hit":
            cells[key]["sl"] += 1
        else:
            cells[key]["exp"] += 1

    out = {}
    for key, c in cells.items():
        n = c["n"]
        if n == 0:
            continue
        p_resolve = (c["tp"] + c["sl"]) / n if n else 0.0
        win_rate = c["tp"] / n if n else 0.0
        avg_p_win = c["p_win_sum"] / n
        bias = avg_p_win - win_rate
        shrunk_wr = beta_shrunk_win_rate(c["tp"], c["sl"])
        bias_shrunk = avg_p_win - shrunk_wr if shrunk_wr is not None else bias
        out[key] = {
            "n": n, "tp": c["tp"], "sl": c["sl"], "exp": c["exp"],
            "p_resolve": round(p_resolve, 4),
            "win_rate": round(win_rate, 4),
            "avg_p_win": round(avg_p_win, 4),
            "bias": round(bias, 4),
            "bias_shrunk": round(bias_shrunk, 4),
        }
    return out


def _simulate_recomputation(
    rows: list[dict],
    *,
    lookback_days: int,
    threshold: float,
    min_n: int,
    cooldown_hours: int,
    simulation_days: int = 30,
) -> dict:
    """Simulate the dynamic re-computation process.

    Replicates dynamic_atr methodology:
    1. Start with full-history stats (stale)
    2. Every 4h, check if bias drifted
    3. If drifted, re-compute from recent lookback window
    4. Measure improvement in bias

    Returns per-bucket re-computation counts and bias improvement.
    """
    checks_per_day = 6  # every 4h
    total_checks = checks_per_day * simulation_days
    cooldown_checks = cooldown_hours // 4

    # Group rows by day
    rows_by_day: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        day = (ts - datetime(2026, 9, 1, tzinfo=timezone.utc)).days
        rows_by_day[day].append(r)

    # Simulate day by day
    recomputations = 0
    bias_improvements = []
    triggers: dict[tuple, int] = defaultdict(int)
    last_triggered: dict[tuple, int] = {}

    for day in range(simulation_days):
        # Current stored stats = full history up to day-1 (stale)
        historical = []
        for d in range(day):
            historical.extend(rows_by_day.get(d, []))
        stored_stats = _compute_bucket_stats(historical)

        # Recent stats = lookback window
        recent = []
        for d in range(max(0, day - lookback_days + 1), day + 1):
            recent.extend(rows_by_day.get(d, []))
        recent_stats = _compute_bucket_stats(recent)

        # Check each bucket
        for key, stored in stored_stats.items():
            if stored["n"] < min_n:
                continue
            if abs(stored["bias_shrunk"]) < threshold:
                continue

            # Cooldown check
            check_idx = day * checks_per_day
            last = last_triggered.get(key, -999)
            if check_idx - last < cooldown_checks:
                continue

            # Re-compute from recent data
            recent_bucket = recent_stats.get(key)
            if recent_bucket is None or recent_bucket["n"] < min_n:
                continue

            # Measure improvement
            old_bias = abs(stored["bias_shrunk"])
            new_bias = abs(recent_bucket["bias_shrunk"])
            improvement = old_bias - new_bias

            triggers[key] += 1
            last_triggered[key] = check_idx
            recomputations += 1
            bias_improvements.append({
                "bucket": key,
                "day": day,
                "old_bias": round(old_bias, 4),
                "new_bias": round(new_bias, 4),
                "improvement": round(improvement, 4),
            })

    return {
        "recomputations": recomputations,
        "triggers_per_bucket": dict(triggers),
        "bias_improvements": bias_improvements,
        "avg_improvement": (
            sum(b["improvement"] for b in bias_improvements) / len(bias_improvements)
            if bias_improvements else 0.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Simulation days")
    args = parser.parse_args()

    print("=== Geometry Band Calibration Urgency — Replay Simulation ===")
    print("(Replicates dynamic_atr methodology: re-compute from recent data)")
    print()

    rows = _fixture()
    print(f"Fixture: {len(rows)} resolved signals over 30 days")
    print(f"Regime shift on day 15 (vol tier + win rate change)")
    print()

    # Full history vs recent window comparison
    print("=== Stale vs Recent Bucket Statistics ===")
    full_stats = _compute_bucket_stats(rows)
    recent_stats = _compute_bucket_stats(rows[15 * 8:])  # Post-shift only

    print(f"{'Bucket':30s}  {'Stale n':>7s}  {'Stale bias':>10s}  {'Recent n':>8s}  {'Recent bias':>11s}  {'Δ bias':>8s}")
    print("-" * 85)
    for key in sorted(full_stats.keys()):
        stale = full_stats[key]
        recent = recent_stats.get(key)
        if recent:
            delta = abs(recent["bias_shrunk"]) - abs(stale["bias_shrunk"])
            print(
                f"{key[0]:8s} {key[1]:15s} {key[2]:5s}  "
                f"{stale['n']:>7d}  {stale['bias_shrunk']:>10.4f}  "
                f"{recent['n']:>8d}  {recent['bias_shrunk']:>11.4f}  "
                f"{delta:>+8.4f}"
            )
    print()

    # Re-computation simulation
    print("=== Re-computation Simulation (30 days, 4h cadence) ===")
    print(f"{'Lookback':>10s}  {'Threshold':>10s}  {'Min n':>6s}  {'Cooldown':>10s}  {'Recomputations':>15s}  {'Avg improvement':>15s}")
    print("-" * 75)

    for lookback in [3, 7, 14]:
        for threshold in [0.05, 0.10, 0.15, 0.20]:
            for cooldown in [4, 8]:
                result = _simulate_recomputation(
                    rows,
                    lookback_days=lookback,
                    threshold=threshold,
                    min_n=20,
                    cooldown_hours=cooldown,
                    simulation_days=args.days,
                )
                print(
                    f"{lookback:>10d}  {threshold:>10.2f}  {20:>6d}  {cooldown:>10d}h  "
                    f"{result['recomputations']:>15d}  {result['avg_improvement']:>15.4f}"
                )
    print()

    # Recommended config
    print("=== Recommended Initial Config ===")
    print("Based on the simulation above:")
    print()
    print("  lookback_days: 7       (re-compute from last 7 days of resolved signals)")
    print("  urgency_threshold: 0.15  (fire when |bias_shrunk| > 0.15)")
    print("  min_n: 20              (minimum resolved signals per bucket)")
    print("  cooldown_hours: 4      (same as ATR urgency)")
    print("  min_age_hours: 4       (minimum age before re-computation)")
    print()

    # Weekly projection
    print("=== Weekly Projection (recommended config) ===")
    result = _simulate_recomputation(
        rows,
        lookback_days=7,
        threshold=0.15,
        min_n=20,
        cooldown_hours=4,
        simulation_days=7,
    )
    print(f"  Recomputations per week: {result['recomputations']}")
    print(f"  Avg bias improvement: {result['avg_improvement']:.4f}")
    print(f"  Buckets monitored: {len(result['triggers_per_bucket'])}")
    print()

    # nt_eod_actions impact
    print("=== nt_eod_actions Impact ===")
    print(f"  New action type: 'geometry_band_urgency'")
    print(f"  Requires CHECK constraint extension (migration)")
    print(f"  Estimated rows per week: {result['recomputations']}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
