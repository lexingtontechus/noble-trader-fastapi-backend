#!/usr/bin/env python3
"""
Historical replay of Phase A's ATR-anchored-bricks shadow comparison
(worklog 20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md).

Waiting on the live weekly heavy-sweep cadence to accumulate real shadow
comparisons takes weeks. This script answers the SAME "would the widened
grid pick something different, and what bucket does it land in" question
using real historical TDVA candles instead of waiting — same production
functions (generate_brick_sizes/generate_atr_anchored_brick_sizes/
_renko_grid_search_sync/select_best_combo_with_atr_anchoring), replayed at
multiple past anchor points instead of once, live, per week.

Not a pytest test (real network, not deterministic/CI-safe) — same
category as scripts/diagnose_hmm_convergence.py.

IMPORTANT — this is NOT a substitute for statistically independent
samples. TDVA's batch endpoint caps history at ~5000 hourly bars (~208
days) regardless of the requested n_bars (confirmed empirically). A
90-day (2160-bar) backtest window only leaves room for ~2 genuinely
non-overlapping windows in that span — true independence tops out there,
no matter how many anchors this script runs. The anchors below are a
ROBUSTNESS/CONSISTENCY check ("does the widened grid consistently prefer
the same bucket across many overlapping historical windows, or does it
flip around"), not a way to manufacture 10+ independent trials.

Deliberately does NOT write to nt_heavy_sweep_atr_anchored_shadow — that
table's counters are meant to reflect genuine live/shadow-mode operation
going forward; merging backfilled replay counts into the same running
tally would make `updated_at`/`n_compared` misleading later. Writes a
per-anchor CSV instead (OUTPUT_CSV below), incrementally as each anchor
finishes (not just once at the end) so results survive an interrupted run
and progress is visible while it's running.

2026-09-08 CORRECTION: the first two attempts used asyncio.to_thread with
CONCURRENCY=6, on the (wrong) assumption that would parallelize across 6
threads. Measured after 3 hours stuck on one run: CPU time was ~97% of a
SINGLE core's worth of wall-clock time — the GIL was serializing
everything, asyncio.to_thread bought nothing for this CPU-bound backtest
loop, and since results were only written after ALL jobs finished, 3 hours
of real compute produced zero retrievable output. Fixed two independent
ways: (1) ProcessPoolExecutor for real parallelism across actual cores
(bypasses the GIL entirely), (2) incremental per-anchor CSV writes + a
progress print per anchor, so a slow or interrupted run still leaves
usable partial results.

First multiplier attempt (2026-09-07, 0.5/1.0/2.0x ATR) also found the
widened grid never reached the proven-good "0.5-1" bucket — see
generate_atr_anchored_brick_sizes's docstring for the exact math. This run
uses the corrected 0.15/0.3/0.6x multipliers.

Usage: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/backfill_atr_anchored_bricks_shadow.py
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

MAX_BARS = 5000          # TDVA's observed batch-endpoint history cap
WINDOW_BARS = 2160       # same 90d window heavy_sweep.py's grid search uses
STEP_HOURS = 600         # ~25-day-spaced anchors (92% overlap at weekly
                         # spacing was both slower and barely more
                         # informative than this given how little new data
                         # each extra close-together anchor adds)
PROCESS_WORKERS = min(8, os.cpu_count() or 4)   # real cores, not GIL-bound
                                                  # threads — see module note
OUTPUT_CSV = "scripts/backfill_atr_anchored_bricks_shadow_results.csv"


@dataclass
class AnchorResult:
    symbol: str
    approx_anchor_ts: str          # UTC, approximated from bar-count offset
                                    # relative to fetch time (no real bar
                                    # timestamps in the close-only batch
                                    # fetcher) — hourly resolution, not exact.
    atr: float
    current_price: float
    baseline_brick_size: float | None
    baseline_sl_bricks: int | None
    baseline_tp_bricks: int | None
    baseline_sharpe: float | None
    baseline_bucket: str | None
    widened_brick_size: float | None
    widened_sl_bricks: int | None
    widened_tp_bricks: int | None
    widened_sharpe: float | None
    widened_bucket: str | None
    changed: bool


def _anchor_jobs(symbol: str, prices: list[float]) -> list[tuple[int, list[float]]]:
    """Returns (bar_index_from_end, window) pairs — the index lets the
    caller approximate a calendar timestamp without needing real bar
    timestamps (the close-only batch fetcher doesn't return any)."""
    if len(prices) < WINDOW_BARS:
        return []
    anchors = range(WINDOW_BARS - 1, len(prices), STEP_HOURS)
    return [(len(prices) - 1 - j, prices[j - WINDOW_BARS + 1: j + 1]) for j in anchors]


def _replay_one_anchor(
    symbol: str, hours_ago: int, window: list[float], fetch_time: datetime,
) -> AnchorResult | None:
    """Runs in a separate PROCESS (ProcessPoolExecutor) — must be
    module-level (picklable by reference) and self-contained (no shared
    state from the parent process; constructs its own HeavySweepMixin).
    Silences the same noisy, pre-existing, unrelated trade-journal/
    execution-gate loggers per-process (module-level suppression in the
    parent doesn't propagate to spawned worker processes on Windows).
    """
    logging.getLogger("renko.trade_journal").setLevel(logging.ERROR)
    logging.getLogger("regime_platform.services.execution_deprecation").setLevel(logging.ERROR)

    from regime_platform.sweep_orchestrator.heavy_sweep import (
        HeavySweepMixin,
        compute_atr,
        generate_atr_anchored_brick_sizes,
        generate_brick_sizes,
        select_best_combo_with_atr_anchoring,
    )
    from regime_platform.sweep_orchestrator.heavy_sweep_reachability import (
        _bucket_for_combo,
    )

    current_price = float(window[-1])
    atr = compute_atr(window, period=14)
    if atr <= 0:
        return None
    brick_sizes = generate_brick_sizes(atr, current_price)
    original = set(brick_sizes)
    anchored = [b for b in generate_atr_anchored_brick_sizes(atr) if b not in original]
    all_sizes = brick_sizes + anchored

    mixin = HeavySweepMixin()
    grid_results = mixin._renko_grid_search_sync(window, all_sizes, symbol)
    selection = select_best_combo_with_atr_anchoring(grid_results, original, "shadow")

    def _bucket(c):
        return _bucket_for_combo(c, atr) if c else None

    b, w = selection.baseline, selection.widened
    return AnchorResult(
        symbol=symbol,
        approx_anchor_ts=(fetch_time - timedelta(hours=hours_ago)).isoformat(),
        atr=round(atr, 6),
        current_price=round(current_price, 6),
        baseline_brick_size=b.brick_size if b else None,
        baseline_sl_bricks=b.sl_bricks if b else None,
        baseline_tp_bricks=b.tp_bricks if b else None,
        baseline_sharpe=round(b.sharpe, 4) if b else None,
        baseline_bucket=_bucket(b),
        widened_brick_size=w.brick_size if w else None,
        widened_sl_bricks=w.sl_bricks if w else None,
        widened_tp_bricks=w.tp_bricks if w else None,
        widened_sharpe=round(w.sharpe, 4) if w else None,
        widened_bucket=_bucket(w),
        changed=selection.changed,
    )


async def main() -> None:
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    active = await fetch_active_symbols()
    print(f"Fetching up to {MAX_BARS} hourly bars for {len(active)} symbols (1-2 batched TDVA calls)...", flush=True)
    fetch_time = datetime.now(timezone.utc)
    batch = await fetch_candles_for_symbols(active, n_bars=MAX_BARS, interval="1h")

    jobs: list[tuple[str, int, list[float]]] = []
    for sym in active:
        prices = batch.get(sym["symbol"], [])
        for hours_ago, window in _anchor_jobs(sym["symbol"], prices):
            jobs.append((sym["symbol"], hours_ago, window))

    print(f"Replaying {len(jobs)} (symbol, anchor) pairs across "
          f"{len({s for s, _, _ in jobs})} symbols, {PROCESS_WORKERS} worker "
          f"PROCESSES (real cores, not GIL-bound threads this time)...", flush=True)

    results: list[AnchorResult] = []
    t_start = time.time()
    loop = asyncio.get_running_loop()
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f, \
         ProcessPoolExecutor(max_workers=PROCESS_WORKERS) as pool:
        writer = csv.DictWriter(f, fieldnames=list(AnchorResult.__annotations__))
        writer.writeheader()
        f.flush()

        futures = [
            loop.run_in_executor(pool, _replay_one_anchor, s, h, w, fetch_time)
            for s, h, w in jobs
        ]
        for i, fut in enumerate(asyncio.as_completed(futures), start=1):
            r = await fut
            elapsed = time.time() - t_start
            if r is None:
                print(f"[{i}/{len(jobs)}] {elapsed:6.0f}s elapsed — (skipped: atr<=0)", flush=True)
                continue
            results.append(r)
            writer.writerow(asdict(r))
            f.flush()
            print(
                f"[{i}/{len(jobs)}] {elapsed:6.0f}s elapsed — {r.symbol}: "
                f"baseline={r.baseline_bucket} widened={r.widened_bucket} changed={r.changed}",
                flush=True,
            )

    print(f"\nWrote {len(results)} per-anchor rows to {OUTPUT_CSV} "
          f"in {time.time() - t_start:.0f}s total", flush=True)

    from collections import Counter
    by_symbol: dict[str, list[AnchorResult]] = {}
    for r in results:
        by_symbol.setdefault(r.symbol, []).append(r)

    print()
    header = f"{'Symbol':<10} {'n_anchors':>9} {'n_changed':>9} {'top baseline bucket':>22} {'top widened bucket':>22}"
    print(header)
    print("-" * len(header))
    for symbol in sorted(by_symbol):
        rows = by_symbol[symbol]
        n = len(rows)
        n_changed = sum(1 for r in rows if r.changed)
        baseline_counts = Counter(r.baseline_bucket for r in rows)
        widened_counts = Counter(r.widened_bucket for r in rows)
        top_baseline = baseline_counts.most_common(1)[0]
        top_widened = widened_counts.most_common(1)[0]
        print(
            f"{symbol:<10} {n:>9} {n_changed:>9} "
            f"{f'{top_baseline[0]} ({top_baseline[1]}/{n})':>22} "
            f"{f'{top_widened[0]} ({top_widened[1]}/{n})':>22}"
        )
        if len(widened_counts) > 1:
            print(f"           full widened-bucket distribution: {dict(widened_counts)}")


if __name__ == "__main__":
    asyncio.run(main())
