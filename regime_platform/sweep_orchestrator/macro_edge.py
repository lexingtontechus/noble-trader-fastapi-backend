"""
Macro Edge by Symbol — win/loss + PnL evidence for the macro filter
════════════════════════════════════════════════════════════════════

worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md. The macro filter
(``macro_filter.py``) is deliberately price-only — it does not look at
win/loss or PnL. This module is the separate, outcome-based read that
answers the question the filter itself can't: does trading WITH the
measured macro trend actually produce a better win rate / expectancy
than trading against it, for this symbol?

READ-ONLY DIAGNOSTIC, same discipline as ``bracket_reachability.py`` /
``calibration_bias_bucket.py``: it reads win/loss-resolved
``nt_signal_sim`` rows that already captured ``macro_label`` at signal
time (migration 124), buckets by whether the signal's direction agreed
with that label, and computes win_rate / avg_rr_winners / expectancy_r
per bucket — the exact same (bug-fixed) formula
``calibration_bias_bucket.py`` uses, reused rather than reinvented.
Also computes median/p75/p90 hours-to-resolve per bucket (migration
126) — same ``_hours_between`` helper and percentile approach
``bracket_reachability.py`` already uses, keyed by alignment instead of
distance/ATR bucket: does trading WITH the macro trend also resolve
faster, not just win more.

The output (``nt_macro_edge_by_symbol``) is evidence for a human to read
before setting ``macro.boost_scale``/``macro.penalty_scale``
(``config/feature_flags.yaml``) — it does NOT auto-tune those values and
does not feed back into live Kelly sizing. Nothing downstream reads this
table yet.

Usage (standalone):
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.macro_edge
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import _hours_between
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    upsert_macro_edge_by_symbol,
)

logger = logging.getLogger("regime.sweep.macro_edge")

_PAGE_SIZE = 1000


async def _fetch_all(table: str, select: str, filters: dict[str, str]) -> list[dict[str, Any]]:
    """Mirrors calibration_bias_bucket.py's own local copy — each module
    keeps its own pagination helper rather than sharing one (existing
    convention in this codebase, not a new one introduced here)."""
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    params = {"select": select, "order": "symbol.asc", "limit": str(_PAGE_SIZE), **filters}
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


def _alignment_for(signal: str, macro_label: str | None) -> str | None:
    """``aligned`` / ``opposed`` / ``neutral``, or ``None`` for a row that
    can't be classified (missing macro_label — pre-migration-124 rows)."""
    if not macro_label:
        return None
    if macro_label == "neutral":
        return "neutral"
    implied = "bull" if signal == "buy" else "bear"
    return "aligned" if macro_label == implied else "opposed"


async def build_macro_edge_table(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Compute + upsert the macro-edge-by-symbol table.

    Population: win/loss-resolved nt_signal_sim rows with a captured
    macro_label — same "expired excluded" rule as
    calibration_bias_bucket.py / nt_calibration_records.
    """
    filters: dict[str, str] = {
        "outcome": "in.(tp_hit,sl_hit)",
        "macro_label": "not.is.null",
    }
    if symbols:
        filters["symbol"] = f"in.({','.join(symbols)})"

    try:
        rows = await _fetch_all(
            "nt_signal_sim",
            "symbol,ts,signal,outcome,resolved_at,macro_label,tp_bricks,sl_bricks",
            filters,
        )
    except Exception as exc:
        logger.error("macro_edge: fetch failed: %s", exc)
        return []
    if not rows:
        logger.info("macro_edge: no win/loss-resolved rows with a captured macro_label found")
        return []

    bucket_data: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "rr_sum": 0.0, "hours": []}
    )

    for row in rows:
        symbol = row.get("symbol")
        signal = row.get("signal")
        outcome = row.get("outcome")
        macro_label = row.get("macro_label")
        if symbol is None or signal not in ("buy", "sell"):
            continue
        alignment = _alignment_for(signal, macro_label)
        if alignment is None:
            continue

        d = bucket_data[(symbol, alignment)]
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
    for (symbol, alignment), d in bucket_data.items():
        n = d["n"]
        win_rate = d["wins"] / n
        avg_rr_winners = d["rr_sum"] / d["wins"] if d["wins"] else None
        # Same bug-fixed formula as calibration_bias_bucket.py: well-defined
        # even at zero wins (it's just -1.0), so don't gate on avg_rr_winners.
        expectancy_r = round(win_rate * (avg_rr_winners or 0.0) - (1 - win_rate) * 1.0, 4)
        hours = d["hours"]
        output_rows.append({
            "symbol": symbol,
            "alignment": alignment,
            "n": n,
            "win_rate": round(win_rate, 4),
            "avg_rr_winners": round(avg_rr_winners, 4) if avg_rr_winners is not None else None,
            "expectancy_r": expectancy_r,
            "median_hours_to_resolve": (
                round(float(np.percentile(hours, 50)), 2) if hours else None
            ),
            "p75_hours": round(float(np.percentile(hours, 75)), 2) if hours else None,
            "p90_hours": round(float(np.percentile(hours, 90)), 2) if hours else None,
            # Stamp every rebuild (Batch C 2026-09-20) so mixed-age rows
            # are visible; full rebuild writes this on all cells.
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })

    try:
        await upsert_macro_edge_by_symbol(output_rows)
    except Exception as exc:
        logger.error("macro_edge: upsert failed: %s", exc)

    logger.info(
        "macro_edge: computed %d bucket row(s) across %d symbol(s)",
        len(output_rows), len({r["symbol"] for r in output_rows}),
    )
    return output_rows


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No macro-edge rows computed.")
        return
    print(
        f"{'Symbol':<10} {'Alignment':<9} {'n':>6} {'WinRate':>8} {'AvgRR':>7} "
        f"{'Expectancy':>11} {'medHr':>7} {'p75':>7} {'p90':>7}"
    )
    print("-" * 90)
    for row in sorted(rows, key=lambda r: (r["symbol"], r["alignment"])):
        rr = row["avg_rr_winners"]
        med = row["median_hours_to_resolve"]
        p75 = row["p75_hours"]
        p90 = row["p90_hours"]
        print(
            f"{row['symbol']:<10} {row['alignment']:<9} {row['n']:>6} "
            f"{row['win_rate'] * 100:>7.1f}% "
            f"{rr if rr is not None else '-':>7} "
            f"{row['expectancy_r']:>+11.3f} "
            f"{med if med is not None else '-':>7} "
            f"{p75 if p75 is not None else '-':>7} "
            f"{p90 if p90 is not None else '-':>7}"
        )


async def main() -> None:
    """CLI entry: build the macro-edge table and print a text report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = await build_macro_edge_table()
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
