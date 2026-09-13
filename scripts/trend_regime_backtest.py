#!/usr/bin/env python3
"""
Re-tunable walk-forward backtest for trend_filter.py's reversal_*
labels (RM-40, aapl_regime_staleness_vs_realized_price, worklog
20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md §9 and
20260913 follow-up).

**What this measures — read before trusting the numbers**: for each
day a fresh `reversal_up`/`reversal_down` label appears, whether the
RAW CLOSE PRICE N days later ended up on the side of that day's close
the label implied. This is a directional-correctness proxy on
`compute_trend_regime()`'s own output — it is NOT a simulated trade:
no entry/stop/target, no renko brick geometry, no EOD resolver
TP/SL-walk logic, no qualification gates. A real signal's actual
outcome depends on the PATH price takes (SL could be hit before the
"eventually correct" direction plays out) and the brick-based
stop/target distances this script never models. Treat a good number
here as "the classifier has real directional signal," not as "wiring
this into `kelly_trend_multiplier` live would improve win rate" — that
second claim needs a backtest hooked into the actual brick/SL-TP
simulation (`renko/pipeline.py`, `eod_resolver.py`'s own logic),
which this script deliberately does not attempt (out of scope for a
"which horizon/crossover-pair" tuning question).

**Why this exists**: the original 2026-09-10 backtest only checked a
5-day forward horizon and found ~52% (coinflip) — barely-there
directional edge. Re-run 2026-09-13 across 3/5/10/15-day horizons
found the 10-day hit rate was materially better (66.7% vs 5-day's
45.5%) on a small (33-event) sample. This script is that re-run,
promoted from a throwaway scratch file into reusable, re-runnable
code — supports a configurable lookback (for a bigger sample) and
sweeping alternate SMA crossover pairs (10/20, 20/50, ...) alongside
horizons, since both were flagged as open tuning parameters.

Usage:
  python3 scripts/trend_regime_backtest.py
  python3 scripts/trend_regime_backtest.py --lookback-days 500 --horizons 5,10,15,20
  python3 scripts/trend_regime_backtest.py --crossover-pairs 10:50,10:20,20:50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(".env.local")

import regime_platform.sweep_orchestrator.trend_filter as trend_filter
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
from regime_platform.sweep_orchestrator.trend_filter import compute_trend_regime

_MIN_SWEEP_HISTORY_DAYS = 80


@contextmanager
def _crossover_pair(sma_short_days: int, sma_long_days: int):
    """Temporarily override the SMA short/long window getters for one
    sweep of the backtest — no config file touched, no production
    behavior affected.

    Patched on `trend_filter`'s own module namespace, not
    `feature_flags`'s — `trend_filter.py` imports these functions by
    name (`from ... import get_trend_sma_short_days, ...`), so its
    namespace holds its own bound reference; patching
    `feature_flags.get_trend_sma_short_days` instead would silently do
    nothing (caught by running this and getting identical output for
    supposedly-different pairs on the first attempt).

    Patches `sma_short_days`/`sma_long_days`, NOT `sma_mid_days` — read
    `compute_trend_regime()` before assuming otherwise: the crossover
    sign (`short >= long`) and `trend_strength` (`|sma_10 - sma_50|`)
    both only ever compare short vs. long. `sma_mid_days` only feeds
    the informational `sma_20` return value and affects nothing the
    label/strength actually depends on — a first draft of this script
    patched mid/long instead and got identical output for supposedly
    different pairs because of exactly this, caught before trusting
    the numbers."""
    with (
        patch.object(trend_filter, "get_trend_sma_short_days", return_value=sma_short_days),
        patch.object(trend_filter, "get_trend_sma_long_days", return_value=sma_long_days),
    ):
        yield


def _run_walk_forward(
    closes_by_symbol: dict[str, list[float]],
    horizons: list[int],
    backtest_days: int,
) -> dict[int, list[tuple[str, str, bool]]]:
    """One full walk-forward pass over every symbol's closes, for
    whatever SMA pair is currently active (via feature_flags, possibly
    patched by `_crossover_pair`). Returns {horizon: [(symbol, label, hit), ...]}."""
    events_by_horizon: dict[int, list[tuple[str, str, bool]]] = {h: [] for h in horizons}
    max_horizon = max(horizons)

    for symbol, closes in closes_by_symbol.items():
        if not closes or len(closes) < _MIN_SWEEP_HISTORY_DAYS:
            continue

        n = len(closes)
        start_idx = max(_MIN_SWEEP_HISTORY_DAYS - 1, n - backtest_days - max_horizon)
        end_idx = n - max_horizon - 1

        prev_label = None
        for idx in range(start_idx, end_idx + 1):
            result = compute_trend_regime(closes[: idx + 1])
            if result is None:
                continue
            label = result["trend_label"]
            is_fresh_reversal = label in ("reversal_up", "reversal_down") and label != prev_label
            if is_fresh_reversal:
                direction = 1 if label == "reversal_up" else -1
                entry_price = closes[idx]
                for h in horizons:
                    future_idx = idx + h
                    if future_idx >= n:
                        continue
                    actual_move = closes[future_idx] - entry_price
                    hit = (actual_move > 0) == (direction > 0)
                    events_by_horizon[h].append((symbol, label, hit))
            prev_label = label

    return events_by_horizon


def _print_results(label: str, events_by_horizon: dict[int, list[tuple[str, str, bool]]]) -> None:
    print(f"\n=== {label} ===")
    for h, events in events_by_horizon.items():
        n_events = len(events)
        n_hits = sum(1 for _, _, hit in events if hit)
        rate = n_hits / n_events if n_events else float("nan")
        print(f"  horizon={h:>2}d: {n_hits:>3}/{n_events:<3} = {rate:.1%}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lookback-days", type=int, default=180, help="Bars fetched per symbol (default 180)")
    parser.add_argument("--backtest-days", type=int, default=None, help="Walk-forward window (default: lookback-days minus SMA/horizon buffer)")
    parser.add_argument("--horizons", type=str, default="3,5,10,15", help="Comma-separated forward-return horizons in days")
    parser.add_argument("--crossover-pairs", type=str, default=None, help="Comma-separated sma_short:sma_long pairs to sweep, e.g. '10:50,10:20,20:50'. Default: current live config only.")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated symbol subset (default: all active symbols)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    backtest_days = args.backtest_days or max(30, args.lookback_days - 50 - max(horizons) - 20)

    active = await fetch_active_symbols()
    if args.symbols:
        wanted = set(args.symbols.split(","))
        active = [s for s in active if s.get("symbol") in wanted]
    print(f"{len(active)} symbols, lookback={args.lookback_days}d, backtest_days={backtest_days}, horizons={horizons}")

    closes_by_symbol: dict[str, list[float]] = await fetch_tradingview_batch(active, args.lookback_days, "1D")
    for symbol, closes in closes_by_symbol.items():
        if not closes or len(closes) < _MIN_SWEEP_HISTORY_DAYS:
            print(f"  {symbol}: insufficient bars ({len(closes) if closes else 0}) -- skipped")

    if not args.crossover_pairs:
        events_by_horizon = _run_walk_forward(closes_by_symbol, horizons, backtest_days)
        _print_results("current live config (sma_mid/sma_long from feature_flags.yaml)", events_by_horizon)
        return

    for pair in args.crossover_pairs.split(","):
        short_str, long_str = pair.split(":")
        sma_short, sma_long = int(short_str), int(long_str)
        with _crossover_pair(sma_short, sma_long):
            events_by_horizon = _run_walk_forward(closes_by_symbol, horizons, backtest_days)
        _print_results(f"crossover pair {sma_short}/{sma_long} (short/long)", events_by_horizon)


if __name__ == "__main__":
    asyncio.run(main())
