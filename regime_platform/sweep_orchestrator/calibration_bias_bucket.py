"""
Calibration Bias by Bucket + Entry Drift — Phase 2 diagnostics
════════════════════════════════════════════════════════════════

Phase 2 of the calibration reframe
(worklog/20260825_CALIBRATION_REFRAME_SCOPE.md). Phase 1's
``bracket_reachability.py`` showed that "OVERCONFIDENT" hides at least
three distinct failure modes: bracket-reachability, directional
miscalibration, and confidence/magnitude miscalibration. This module
builds two READ-ONLY diagnostics that expose all three side by side,
without hardcoding a mode classifier:

  * ``nt_calibration_bias_by_bucket`` — per (symbol, distance/ATR
    bucket), computed from win/loss-resolved ``nt_signal_sim`` rows
    (the same population ``nt_calibration_records`` is built from):
    ``bias`` (avg_p_win - win_rate, the existing calibration formula),
    ``pct_expired`` (joined from ``nt_bracket_reachability`` — the
    reachability lens), ``expectancy_r`` (win_rate-weighted realized
    reward:risk — the directional/magnitude lens), and
    ``daily_expectancy_r`` (expectancy normalized by typical hold
    time — the "which edge is actually worth deploying" lens).
  * ``nt_entry_drift`` — per symbol, mean + stdev of post-entry price
    drift at 1h/4h/24h horizons, across ALL resolved signals (any
    outcome). Answers "does price typically run in our favor or
    against us right after entry" and how much that varies — a live
    check on Phase 1's ATR sizing assumption.

Nothing downstream reads either table yet. No change to
``eod_correction.py``'s classification/actions, ``auto_tuner.py``'s
health metric, or the migration-119 SQL views — see the worklog for
why enforcement changes wait for human review of this output.

Usage (standalone):
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.calibration_bias_bucket

Design rules: best-effort only, matching ``bracket_reachability.py`` —
a failure here is logged and never raised into the sweep tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import (
    ATR_BUCKETS,
    _bucket_for,
    _current_atr_by_symbol,
    _hours_between,
    _vol_tier,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    upsert_calibration_bias_by_bucket,
    upsert_entry_drift,
)

logger = logging.getLogger("regime.sweep.calibration_bias_bucket")

_PAGE_SIZE = 1000


async def _fetch_all(table: str, select: str, filters: dict[str, str]) -> list[dict[str, Any]]:
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    params = {"select": select, "order": "ts.asc" if table == "nt_signal_sim" else "symbol.asc", "limit": str(_PAGE_SIZE), **filters}
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url(table), headers=_headers(), params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    return rows


# ─── nt_calibration_bias_by_bucket ─────────────────────────────────

async def build_calibration_bias_by_bucket(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Compute + upsert the calibration-bias-by-bucket table.

    Population: win/loss-resolved nt_signal_sim rows — same population
    nt_calibration_records is built from (expired rows are excluded by
    construction, matching the existing calibration formula).
    """
    filters: dict[str, str] = {"outcome": "in.(tp_hit,sl_hit)"}
    if symbols:
        filters["symbol"] = f"in.({','.join(symbols)})"

    try:
        rows = await _fetch_all(
            "nt_signal_sim",
            "symbol,ts,entry_price,take_profit,atr,p_win,outcome,resolved_at,tp_bricks,sl_bricks,regime",
            filters,
        )
    except Exception as exc:
        logger.error("calibration_bias_bucket: fetch failed: %s", exc)
        return []
    if not rows:
        logger.info("calibration_bias_bucket: no win/loss-resolved nt_signal_sim rows found")
        return []

    missing_atr_symbols = sorted({
        r["symbol"] for r in rows if not r.get("atr") and r.get("symbol")
    })
    try:
        proxy_atr = await _current_atr_by_symbol(missing_atr_symbols)
    except Exception as exc:
        logger.warning("calibration_bias_bucket: ATR proxy fetch failed: %s", exc)
        proxy_atr = {}

    bucket_data: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "n": 0, "wins": 0, "losses": 0, "p_win_sum": 0.0,
            "rr_sum": 0.0, "hours": [], "n_stored": 0, "n_proxy": 0,
        }
    )

    for row in rows:
        symbol = row.get("symbol")
        entry = row.get("entry_price")
        tp = row.get("take_profit")
        outcome = row.get("outcome")
        if symbol is None or entry is None or tp is None:
            continue

        atr = row.get("atr")
        atr_source = "stored"
        if not atr:
            atr = proxy_atr.get(symbol)
            atr_source = "proxy_current"
        if not atr:
            continue

        # Batch B: proxy ATR is a look-ahead approximation (current ATR for
        # historical rows). Exclude from expectancy/bias so live gates never
        # train on contaminated buckets. Still counted for n_proxy visibility.
        if atr_source == "proxy_current":
            continue

        ratio = abs(float(tp) - float(entry)) / atr
        bucket = _bucket_for(ratio)
        if bucket is None:
            continue
        vol_tier = _vol_tier(row.get("regime"))

        d = bucket_data[(symbol, vol_tier, bucket)]
        d["n"] += 1
        d["n_stored"] += 1
        p_win = row.get("p_win")
        if p_win is not None:
            d["p_win_sum"] += float(p_win)
        if outcome == "tp_hit":
            d["wins"] += 1
            tp_bricks = row.get("tp_bricks") or 0
            sl_bricks = row.get("sl_bricks") or 0
            if sl_bricks:
                d["rr_sum"] += float(tp_bricks) / float(sl_bricks)
        else:
            d["losses"] += 1
        hrs = _hours_between(row.get("ts"), row.get("resolved_at"))
        if hrs is not None:
            d["hours"].append(hrs)

    try:
        reachability_rows = await _fetch_all(
            "nt_bracket_reachability", "symbol,vol_tier,distance_atr_bucket,pct_expired", {},
        )
    except Exception as exc:
        logger.warning("calibration_bias_bucket: reachability join fetch failed: %s", exc)
        reachability_rows = []
    pct_expired_by_bucket = {
        (r["symbol"], r.get("vol_tier", "unknown"), r["distance_atr_bucket"]): r.get("pct_expired")
        for r in reachability_rows
    }

    output_rows: list[dict[str, Any]] = []
    for (symbol, vol_tier, bucket), d in bucket_data.items():
        n = d["n"]
        avg_p_win = d["p_win_sum"] / n
        win_rate = d["wins"] / n
        bias = round(avg_p_win - win_rate, 4)
        avg_rr_winners = d["rr_sum"] / d["wins"] if d["wins"] else None
        # expectancy is well-defined even at zero wins (it's just -1.0 —
        # win_rate * avg_rr_winners is 0 regardless of avg_rr_winners
        # being undefined), so don't gate this on avg_rr_winners.
        expectancy_r = round(win_rate * (avg_rr_winners or 0.0) - (1 - win_rate) * 1.0, 4)
        median_hours = round(float(np.percentile(d["hours"], 50)), 2) if d["hours"] else None
        daily_expectancy_r = (
            round(expectancy_r / (median_hours / 24.0), 4) if median_hours else None
        )
        output_rows.append({
            "symbol": symbol,
            "vol_tier": vol_tier,
            "distance_atr_bucket": bucket,
            "n": n,
            "avg_p_win": round(avg_p_win, 4),
            "win_rate": round(win_rate, 4),
            "bias": bias,
            "pct_expired": pct_expired_by_bucket.get((symbol, vol_tier, bucket)),
            "avg_rr_winners": round(avg_rr_winners, 4) if avg_rr_winners is not None else None,
            "expectancy_r": expectancy_r,
            "median_hours_to_resolve": median_hours,
            "daily_expectancy_r": daily_expectancy_r,
            "atr_source": "stored",
        })

    try:
        await upsert_calibration_bias_by_bucket(output_rows)
    except Exception as exc:
        logger.error("calibration_bias_bucket: upsert failed: %s", exc)

    logger.info(
        "calibration_bias_bucket: computed %d bucket row(s) across %d symbol(s)",
        len(output_rows), len({r["symbol"] for r in output_rows}),
    )
    return output_rows


# ─── nt_entry_drift ─────────────────────────────────────────────────

async def build_entry_drift(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Compute + upsert per-symbol entry-drift stats.

    Population: ALL resolved nt_signal_sim rows (any outcome) — drift
    measures post-entry market behavior, independent of whether the
    trade itself hit TP/SL/expired.
    """
    filters: dict[str, str] = {"outcome": "not.is.null"}
    if symbols:
        filters["symbol"] = f"in.({','.join(symbols)})"

    try:
        rows = await _fetch_all(
            "nt_signal_sim",
            "symbol,drift_1h_pct,drift_4h_pct,drift_24h_pct",
            filters,
        )
    except Exception as exc:
        logger.error("entry_drift: fetch failed: %s", exc)
        return []
    if not rows:
        logger.info("entry_drift: no resolved nt_signal_sim rows found")
        return []

    by_symbol: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"1h": [], "4h": [], "24h": []}
    )
    for row in rows:
        symbol = row.get("symbol")
        if symbol is None:
            continue
        d = by_symbol[symbol]
        for key, col in (("1h", "drift_1h_pct"), ("4h", "drift_4h_pct"), ("24h", "drift_24h_pct")):
            val = row.get(col)
            if val is not None:
                d[key].append(float(val))

    output_rows: list[dict[str, Any]] = []
    for symbol, d in by_symbol.items():
        n = max(len(d["1h"]), len(d["4h"]), len(d["24h"]))
        if n == 0:
            continue
        row: dict[str, Any] = {"symbol": symbol, "n": n}
        for key in ("1h", "4h", "24h"):
            vals = d[key]
            if vals:
                row[f"avg_drift_{key}_pct"] = round(float(np.mean(vals)), 4)
                row[f"stdev_drift_{key}_pct"] = round(float(np.std(vals)), 4)
            else:
                row[f"avg_drift_{key}_pct"] = None
                row[f"stdev_drift_{key}_pct"] = None
        output_rows.append(row)

    try:
        await upsert_entry_drift(output_rows)
    except Exception as exc:
        logger.error("entry_drift: upsert failed: %s", exc)

    logger.info("entry_drift: computed %d symbol row(s)", len(output_rows))
    return output_rows


# ─── CLI reporting ──────────────────────────────────────────────────

def _print_bias_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No calibration-bias-by-bucket rows computed.")
        return
    print(
        f"{'Symbol':<10} {'VolTier':<13} {'Bucket':<8} {'n':>6} {'avgP':>6} {'winR':>6} "
        f"{'bias':>7} {'%Exp':>6} {'RRwin':>6} {'exR':>7} {'medHr':>7} {'dExR':>7} {'src':<14}"
    )
    print("-" * 115)
    for row in sorted(rows, key=lambda r: (r["symbol"], r.get("vol_tier", ""), r["distance_atr_bucket"])):
        def fmt(v, spec="{:.3f}"):
            return spec.format(v) if v is not None else "-"
        print(
            f"{row['symbol']:<10} {row.get('vol_tier', 'unknown'):<13} {row['distance_atr_bucket']:<8} {row['n']:>6} "
            f"{fmt(row['avg_p_win']):>6} {fmt(row['win_rate']):>6} {fmt(row['bias'], '{:+.3f}'):>7} "
            f"{fmt(row['pct_expired']):>6} {fmt(row['avg_rr_winners'], '{:.2f}'):>6} "
            f"{fmt(row['expectancy_r'], '{:+.3f}'):>7} {fmt(row['median_hours_to_resolve'], '{:.1f}'):>7} "
            f"{fmt(row['daily_expectancy_r'], '{:+.3f}'):>7} {row['atr_source']:<14}"
        )


def _print_drift_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No entry-drift rows computed.")
        return
    print(f"{'Symbol':<10} {'n':>6} {'avg1h':>7} {'sd1h':>6} {'avg4h':>7} {'sd4h':>6} {'avg24h':>7} {'sd24h':>6}")
    print("-" * 62)
    for row in sorted(rows, key=lambda r: r["symbol"]):
        def fmt(v):
            return f"{v:.3f}" if v is not None else "-"
        print(
            f"{row['symbol']:<10} {row['n']:>6} "
            f"{fmt(row['avg_drift_1h_pct']):>7} {fmt(row['stdev_drift_1h_pct']):>6} "
            f"{fmt(row['avg_drift_4h_pct']):>7} {fmt(row['stdev_drift_4h_pct']):>6} "
            f"{fmt(row['avg_drift_24h_pct']):>7} {fmt(row['stdev_drift_24h_pct']):>6}"
        )


async def main() -> None:
    """CLI entry: build both Phase 2 diagnostics and print text reports."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bias_rows = await build_calibration_bias_by_bucket()
    drift_rows = await build_entry_drift()
    print()
    _print_bias_report(bias_rows)
    print()
    _print_drift_report(drift_rows)


if __name__ == "__main__":
    asyncio.run(main())
