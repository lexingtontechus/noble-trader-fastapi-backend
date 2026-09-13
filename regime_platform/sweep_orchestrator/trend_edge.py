"""
Trend Edge by Symbol — win/loss + PnL evidence for the trend filter
════════════════════════════════════════════════════════════════════

worklog/20260912_EOD_CORRECTION_INTRADAY_SCOPE_AND_SELF_LEARNING_FOLLOWUPS.md
§12. Structural clone of ``macro_edge.py`` — that module already
answers this exact question for ``macro_label`` ("does trading WITH
the measured trend actually produce a better win rate / expectancy
than trading against it, for this symbol?"); this is the same
analysis keyed on ``trend_label`` instead.

Motivated directly by the "w/l is not p/l" distinction: a directional
hit-rate backtest (``scripts/trend_regime_backtest.py``, worklog §11)
found trend_label has no standalone directional edge — but a trend
signal isn't supposed to be judged on hit rate alone (real trend-
following systems often run <50% win rates and are still profitable,
because winners are sized larger than losers). This module checks the
metric that actually matters: realized win rate AND expectancy_r (an
R-multiple, the same bug-fixed formula ``calibration_bias_bucket.py``/
``macro_edge.py`` already use), not just directional correctness.

READ-ONLY DIAGNOSTIC, same discipline as ``macro_edge.py``: reads
win/loss-resolved ``nt_signal_sim`` rows that captured ``trend_label``
at signal time, buckets by whether the signal's direction agreed with
that label AND by a ``trend_strength`` tier (low/med/high) — the one
addition beyond ``macro_edge.py``'s own bucket key, since "does a
high-conviction trend read show a real edge even though the aggregate
doesn't" is the live open question here. Output
(``nt_trend_edge_by_symbol``) is evidence for a human to read before
deciding ``trend_regime_mode`` shadow -> live (or off) — it does NOT
auto-tune anything and does not feed back into ``kelly_trend_multiplier``.

Prerequisite this module depends on but does not control:
``trend_regime_mode`` must be ``shadow`` or ``live`` for
``nt_signal_sim.trend_label`` to ever be populated (flipped
``off`` -> ``shadow`` 2026-09-13 specifically to start this
accumulating). Running this before enough win/loss-resolved,
trend_label-captured signals exist will simply return an empty result
— not an error, just nothing to report yet.

Usage (standalone):
  cd noble-trader-fastapi-backend
  python3 -m regime_platform.sweep_orchestrator.trend_edge
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from regime_platform.sweep_orchestrator.bracket_reachability import _hours_between
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_all_rows,
    upsert_trend_edge_by_symbol,
)

logger = logging.getLogger("regime.sweep.trend_edge")

# Bull-implied labels per trend_filter.py's 5-value vocab
# (trending_up/trending_down/reversal_up/reversal_down/consolidating).
_BULL_LABELS = frozenset({"trending_up", "reversal_up"})
_BEAR_LABELS = frozenset({"trending_down", "reversal_down"})

# trend_strength is saturated to [0, 1] (trend_filter.py's own
# strength_saturation normalization) -- tier cut points chosen to split
# roughly into thirds without a config dependency, matching this
# diagnostic's read-only/no-tuning-input status.
_STRENGTH_TIER_LOW_MAX = 0.33
_STRENGTH_TIER_MED_MAX = 0.66


def _alignment_for(signal: str, trend_label: str | None) -> str | None:
    """``aligned`` / ``opposed`` / ``neutral``, or ``None`` for a row
    that can't be classified (missing trend_label). Mirrors
    ``macro_edge._alignment_for`` exactly, adapted to trend_label's
    5-value vocab instead of macro_label's 3-value one."""
    if not trend_label:
        return None
    if trend_label == "consolidating":
        return "neutral"
    if trend_label not in _BULL_LABELS and trend_label not in _BEAR_LABELS:
        return None
    implied = "bull" if trend_label in _BULL_LABELS else "bear"
    signal_direction = "bull" if signal == "buy" else "bear"
    return "aligned" if signal_direction == implied else "opposed"


def _strength_tier_for(trend_strength: float | None) -> str | None:
    """``low`` / ``med`` / ``high``, or ``None`` if missing. The one
    bucketing dimension beyond ``macro_edge.py``'s own design — tests
    whether a high-conviction trend read shows a real edge even though
    the aggregate (``scripts/trend_regime_backtest.py``) found none."""
    if trend_strength is None:
        return None
    if trend_strength < _STRENGTH_TIER_LOW_MAX:
        return "low"
    if trend_strength < _STRENGTH_TIER_MED_MAX:
        return "med"
    return "high"


async def build_trend_edge_table(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Compute + upsert the trend-edge-by-symbol table.

    Population: win/loss-resolved nt_signal_sim rows with both a
    captured trend_label AND trend_strength — same "expired excluded"
    rule as macro_edge.py / calibration_bias_bucket.py.
    """
    filters: dict[str, str] = {
        "outcome": "in.(tp_hit,sl_hit)",
        "trend_label": "not.is.null",
    }
    if symbols:
        filters["symbol"] = f"in.({','.join(symbols)})"

    try:
        rows = await fetch_all_rows(
            "nt_signal_sim",
            "symbol,ts,signal,outcome,resolved_at,trend_label,trend_strength,tp_bricks,sl_bricks",
            filters=filters,
            order="symbol.asc",
        )
    except Exception as exc:
        logger.error("trend_edge: fetch failed: %s: %r", type(exc).__name__, exc)
        return []
    if not rows:
        logger.info("trend_edge: no win/loss-resolved rows with a captured trend_label found")
        return []

    bucket_data: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "rr_sum": 0.0, "hours": []}
    )

    for row in rows:
        symbol = row.get("symbol")
        signal = row.get("signal")
        outcome = row.get("outcome")
        trend_label = row.get("trend_label")
        trend_strength = row.get("trend_strength")
        if symbol is None or signal not in ("buy", "sell"):
            continue
        alignment = _alignment_for(signal, trend_label)
        strength_tier = _strength_tier_for(trend_strength)
        if alignment is None or strength_tier is None:
            continue

        d = bucket_data[(symbol, alignment, strength_tier)]
        d["n"] += 1
        if outcome == "tp_hit":
            d["wins"] += 1
            tp_bricks = row.get("tp_bricks") or 0
            sl_bricks = row.get("sl_bricks") or 0
            if sl_bricks:
                d["rr_sum"] += float(tp_bricks) / float(sl_bricks)
        hrs = _hours_between(row.get("ts"), row.get("resolved_at"))
        if hrs is not None:
            d["hours"].append(hrs)

    output_rows: list[dict[str, Any]] = []
    for (symbol, alignment, strength_tier), d in bucket_data.items():
        n = d["n"]
        win_rate = d["wins"] / n
        avg_rr_winners = d["rr_sum"] / d["wins"] if d["wins"] else None
        # Same bug-fixed formula as calibration_bias_bucket.py/macro_edge.py:
        # well-defined even at zero wins (it's just -1.0), don't gate on
        # avg_rr_winners.
        expectancy_r = round(win_rate * (avg_rr_winners or 0.0) - (1 - win_rate) * 1.0, 4)
        hours = d["hours"]
        output_rows.append({
            "symbol": symbol,
            "alignment": alignment,
            "strength_tier": strength_tier,
            "n": n,
            "win_rate": round(win_rate, 4),
            "avg_rr_winners": round(avg_rr_winners, 4) if avg_rr_winners is not None else None,
            "expectancy_r": expectancy_r,
            "median_hours_to_resolve": _percentile(hours, 50),
            "p75_hours": _percentile(hours, 75),
            "p90_hours": _percentile(hours, 90),
        })

    try:
        await upsert_trend_edge_by_symbol(output_rows)
    except Exception as exc:
        logger.error("trend_edge: upsert failed: %s: %r", type(exc).__name__, exc)

    logger.info(
        "trend_edge: computed %d bucket row(s) across %d symbol(s)",
        len(output_rows), len({r["symbol"] for r in output_rows}),
    )
    return output_rows


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    import numpy as np
    return round(float(np.percentile(values, pct)), 2)


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No trend-edge rows computed.")
        return
    print(
        f"{'Symbol':<10} {'Alignment':<9} {'Tier':<5} {'n':>6} {'WinRate':>8} {'AvgRR':>7} "
        f"{'Expectancy':>11} {'medHr':>7} {'p75':>7} {'p90':>7}"
    )
    print("-" * 96)
    for row in sorted(rows, key=lambda r: (r["symbol"], r["alignment"], r["strength_tier"])):
        rr = row["avg_rr_winners"]
        med = row["median_hours_to_resolve"]
        p75 = row["p75_hours"]
        p90 = row["p90_hours"]
        print(
            f"{row['symbol']:<10} {row['alignment']:<9} {row['strength_tier']:<5} {row['n']:>6} "
            f"{row['win_rate'] * 100:>7.1f}% "
            f"{rr if rr is not None else '-':>7} "
            f"{row['expectancy_r']:>+11.3f} "
            f"{med if med is not None else '-':>7} "
            f"{p75 if p75 is not None else '-':>7} "
            f"{p90 if p90 is not None else '-':>7}"
        )


async def main() -> None:
    """CLI entry: build the trend-edge table and print a text report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = await build_trend_edge_table()
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
