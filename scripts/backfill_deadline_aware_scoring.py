#!/usr/bin/env python3
"""
Multi-window historical validation of heavy_sweep_deadline_aware_scoring_mode
(worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md).

scripts/sim_deadline_aware_scoring.py already proved the fix on a single,
most-recent 90-day window (XAUUSD: 100%-expired/$0 -> 86.7% win/+$468).
This repeats that same real-outcome comparison — original (uncapped Sharpe,
today's actual method) vs corrected (deadline_aware_sharpe, the fix) —
across multiple historical anchor points per symbol, same real-outcome
methodology, so the decision to flip the mode doesn't rest on one window.

Reuses the production functions directly (ComboResult, deadline_aware_sharpe,
select_best_combo_deadline_aware from heavy_sweep.py) rather than
re-deriving the scoring logic a third time — if this script's answer ever
disagreed with production's, that would itself be a bug worth finding.

Multi-window mechanics (window count, step, process-pool pattern,
incremental CSV, per-process logger suppression) copied from
scripts/backfill_atr_anchored_bricks_shadow.py's SECOND (fixed) version —
the first version used asyncio.to_thread and a CONCURRENCY setting that
did nothing for ~3 hours because the GIL serializes this CPU-bound work;
do not repeat that mistake here.

Usage: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/backfill_deadline_aware_scoring.py
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
WINDOW_BARS = 2160        # same 90d window heavy_sweep.py's grid search uses
EXPIRY_HOURS = 48         # must match eod_resolver.EXPIRY_BARS exactly
STEP_HOURS = 600          # ~25-day-spaced anchors — same spacing rationale
                          # as the ATR-anchoring backfill (weekly spacing is
                          # >90% window overlap, not meaningfully independent)
PROCESS_WORKERS = min(8, os.cpu_count() or 4)
OUTPUT_CSV = "scripts/backfill_deadline_aware_scoring_results.csv"


@dataclass
class AnchorResult:
    symbol: str
    approx_anchor_ts: str
    method: str              # "original" (uncapped, today's actual method) | "corrected" (deadline-aware)
    brick_size: float
    sl_bricks: int
    tp_bricks: int
    selection_sharpe: float
    total_trades: int
    wins: int
    losses: int
    expired: int              # hold > EXPIRY_HOURS under the SAME ground truth, both methods
    win_pct_of_settled: float | None
    total_pnl_bricks: float
    total_pnl_dollars: float
    avg_hold_hours_settled: float | None
    changed_from_original: bool  # whether "corrected" picked a different combo than "original" this anchor


def _anchor_jobs(symbol: str, prices: list[float]) -> list[tuple[int, list[float]]]:
    if len(prices) < WINDOW_BARS:
        return []
    anchors = range(WINDOW_BARS - 1, len(prices), STEP_HOURS)
    return [(len(prices) - 1 - j, prices[j - WINDOW_BARS + 1: j + 1]) for j in anchors]


def _replay_one_anchor(
    symbol: str, hours_ago: int, window: list[float], fetch_time: datetime,
) -> list[AnchorResult]:
    """Runs in a separate process — module-level, self-contained."""
    logging.getLogger("renko.trade_journal").setLevel(logging.ERROR)
    logging.getLogger("regime_platform.services.execution_deprecation").setLevel(logging.ERROR)

    from renko.config import RenkoConfig
    from renko.pipeline import RenkoPipeline
    from regime_platform.sweep_orchestrator.heavy_sweep import (
        ComboResult,
        SWEEP_SL_RANGE, SWEEP_TP_RANGE, MIN_RR,
        compute_atr, deadline_aware_sharpe, generate_brick_sizes,
        select_best_combo_deadline_aware,
    )

    anchor_ts = (fetch_time - timedelta(hours=hours_ago)).isoformat()
    current_price = float(window[-1])
    atr = compute_atr(window, period=14)
    if atr <= 0:
        return []
    brick_sizes = generate_brick_sizes(atr, current_price)

    combos: list[ComboResult] = []
    trades_by_combo: dict[tuple[float, int, int], list[dict]] = {}
    for brick in brick_sizes:
        for sl in SWEEP_SL_RANGE:
            for tp in SWEEP_TP_RANGE:
                if tp / sl < MIN_RR:
                    continue
                try:
                    config = RenkoConfig(
                        brick_size=brick, sl_bricks=sl, tp_bricks=tp,
                        dry_run=True, live_enabled=False,
                    )
                    pipeline = RenkoPipeline(config)
                    result = pipeline.backtest_with_analytics(prices=window, n_trials=1)
                    trades = result.get("trades", [])
                    stats = result.get("stats", {}).get("journal", result.get("stats", {}))
                    key = (brick, sl, tp)
                    trades_by_combo[key] = trades
                    combos.append(ComboResult(
                        brick_size=brick, sl_bricks=sl, tp_bricks=tp,
                        sharpe=float(stats.get("sharpe_estimate", 0.0)),
                        total_trades=int(stats.get("total_trades", 0)),
                        win_rate=float(stats.get("win_rate", 0.0)),
                        deadline_aware_sharpe=deadline_aware_sharpe(trades),
                    ))
                except Exception:
                    continue

    # select_best_combo_deadline_aware in "shadow" mode gives us BOTH picks
    # (uncapped="original", corrected) from the exact production selection
    # function, over the exact same candidate pool, in one call.
    selection = select_best_combo_deadline_aware(combos, mode="shadow")

    def _summarize(method: str, picked: "ComboResult | None") -> AnchorResult | None:
        if picked is None:
            return None
        trades = trades_by_combo[(picked.brick_size, picked.sl_bricks, picked.tp_bricks)]
        wins = losses = expired = 0
        settled_hours = []
        total_pnl_bricks = 0.0
        for t in trades:
            hold = t["exit_ts"] - t["entry_ts"]
            if hold > EXPIRY_HOURS:
                expired += 1
                continue
            settled_hours.append(hold)
            if t["pnl_bricks"] > 0:
                wins += 1
            else:
                losses += 1
            total_pnl_bricks += t["pnl_bricks"]
        n_settled = wins + losses
        sharpe = picked.sharpe if method == "original" else picked.deadline_aware_sharpe
        return AnchorResult(
            symbol=symbol,
            approx_anchor_ts=anchor_ts,
            method=method,
            brick_size=picked.brick_size,
            sl_bricks=picked.sl_bricks,
            tp_bricks=picked.tp_bricks,
            selection_sharpe=round(sharpe, 4),
            total_trades=len(trades),
            wins=wins,
            losses=losses,
            expired=expired,
            win_pct_of_settled=round(100.0 * wins / n_settled, 1) if n_settled else None,
            total_pnl_bricks=round(total_pnl_bricks, 4),
            total_pnl_dollars=round(total_pnl_bricks * picked.brick_size, 2),
            avg_hold_hours_settled=round(sum(settled_hours) / len(settled_hours), 1) if settled_hours else None,
            changed_from_original=selection.changed,
        )

    out = []
    r1 = _summarize("original", selection.uncapped)
    if r1:
        out.append(r1)
    r2 = _summarize("corrected", selection.corrected)
    if r2:
        out.append(r2)
    return out


async def main() -> None:
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    active = await fetch_active_symbols()
    print(f"Fetching up to {MAX_BARS} hourly bars for {len(active)} symbols...", flush=True)
    fetch_time = datetime.now(timezone.utc)
    batch = await fetch_candles_for_symbols(active, n_bars=MAX_BARS, interval="1h")

    jobs: list[tuple[str, int, list[float]]] = []
    for sym in active:
        prices = batch.get(sym["symbol"], [])
        for hours_ago, window in _anchor_jobs(sym["symbol"], prices):
            jobs.append((sym["symbol"], hours_ago, window))

    print(f"Replaying {len(jobs)} (symbol, anchor) pairs across "
          f"{len({s for s, _, _ in jobs})} symbols, {PROCESS_WORKERS} worker processes...", flush=True)

    t_start = time.time()
    loop = asyncio.get_running_loop()
    all_results: list[AnchorResult] = []
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
            rows = await fut
            elapsed = time.time() - t_start
            for r in rows:
                all_results.append(r)
                writer.writerow(asdict(r))
            f.flush()
            summary = " | ".join(
                f"{r.method}: trades={r.total_trades} win%={r.win_pct_of_settled} "
                f"exp={r.expired} pnl$={r.total_pnl_dollars}"
                for r in sorted(rows, key=lambda r: r.method)
            ) if rows else "(no viable combo)"
            print(f"[{i}/{len(jobs)}] {elapsed:6.0f}s — {rows[0].symbol if rows else '?'}: {summary}", flush=True)

    print(f"\nWrote {len(all_results)} rows to {OUTPUT_CSV} in {time.time() - t_start:.0f}s", flush=True)

    # ── Aggregate summary: original vs corrected, across ALL anchors ──
    from collections import defaultdict
    agg: dict[str, dict[str, list]] = defaultdict(lambda: {"win_pct": [], "pnl": [], "expired_pct": []})
    for r in all_results:
        n_total = r.wins + r.losses + r.expired
        agg[r.method]["win_pct"].append(r.win_pct_of_settled if r.win_pct_of_settled is not None else 0.0)
        agg[r.method]["pnl"].append(r.total_pnl_dollars)
        agg[r.method]["expired_pct"].append(100.0 * r.expired / n_total if n_total else 0.0)

    print()
    print(f"{'Method':<12} {'n_anchors':>9} {'avg_win%':>9} {'avg_expired%':>13} {'total_pnl$':>12}")
    print("-" * 60)
    for method in ("original", "corrected"):
        vals = agg[method]
        n = len(vals["win_pct"])
        print(
            f"{method:<12} {n:>9} "
            f"{sum(vals['win_pct']) / n if n else 0:>9.1f} "
            f"{sum(vals['expired_pct']) / n if n else 0:>13.1f} "
            f"{sum(vals['pnl']):>12.2f}"
        )

    print()
    print(f"{'Symbol':<10} {'n_anchors':>9} {'n_changed':>9} {'orig avg win%':>14} {'corr avg win%':>14} {'orig pnl$':>10} {'corr pnl$':>10}")
    print("-" * 90)
    by_symbol: dict[str, dict[str, list]] = defaultdict(lambda: {"original": [], "corrected": []})
    for r in all_results:
        by_symbol[r.symbol][r.method].append(r)
    for symbol in sorted(by_symbol):
        orig = by_symbol[symbol]["original"]
        corr = by_symbol[symbol]["corrected"]
        n_anchors = max(len(orig), len(corr))
        n_changed = sum(1 for r in corr if r.changed_from_original)
        orig_win = sum(r.win_pct_of_settled or 0 for r in orig) / len(orig) if orig else 0
        corr_win = sum(r.win_pct_of_settled or 0 for r in corr) / len(corr) if corr else 0
        orig_pnl = sum(r.total_pnl_dollars for r in orig)
        corr_pnl = sum(r.total_pnl_dollars for r in corr)
        print(
            f"{symbol:<10} {n_anchors:>9} {n_changed:>9} "
            f"{orig_win:>14.1f} {corr_win:>14.1f} {orig_pnl:>10.2f} {corr_pnl:>10.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
