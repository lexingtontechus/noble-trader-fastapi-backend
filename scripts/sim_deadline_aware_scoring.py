#!/usr/bin/env python3
"""
Simulates the ACTUAL root-cause fix: the heavy-sweep grid search scores
candidate brackets over a 90-day window with no deadline, then the live
resolver gives every real signal only 48 hours (EXPIRY_BARS=48,
eod_resolver.py) to prove itself. A bracket whose "wins" mostly take
100+ hours looks great in the backtest and mostly times out live.

This does NOT widen the candidate grid (that was already tried and
didn't address the actual defect — see worklog
20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md).
It changes ONE thing: how a candidate's Sharpe is scored.

Mechanism (verified against renko/pipeline.py and renko/trade_journal.py
before writing this, not assumed):
  - RenkoPipeline.backtest_with_analytics(prices=window) calls
    self.backtest(), which ticks through `window` calling
    process_tick(price, ts=float(i)) when no explicit timestamps are
    given — so TradeRecord.entry_ts/exit_ts are bar INDICES, and
    (exit_ts - entry_ts) is exactly "hours held" since each bar is 1
    hour (confirmed: TradeJournal.get_stats() already computes
    avg_duration_seconds the same way).
  - TradeJournal.get_stats()'s sharpe_estimate is
    mean(pnl_bricks) / std(pnl_bricks, ddof=1) over ALL trades, however
    long they took to close. Reused verbatim here — not reinvented.

The fix simulated: for every trade with (exit_ts - entry_ts) > 48,
treat pnl_bricks as 0 (exactly eod_resolver.compute_pnl's convention for
"expired") BEFORE computing Sharpe/win-rate, instead of after using it to
select. Pick the best combo under this corrected Sharpe (same MIN_RR/
MAX_SHARPE/MIN_TRADES filters as pick_best_combo) instead of the
uncapped one. Then compare, under the SAME 48h ground truth both times,
what each selection actually produces.

Single most-recent 90-day window per symbol (not a multi-anchor replay —
this is testing whether the scoring fix itself works, not re-litigating
robustness across time, which the earlier ATR-anchoring backfill already
covered and found wanting for a different reason).

Usage: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/sim_deadline_aware_scoring.py
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

warnings.filterwarnings("ignore")

WINDOW_BARS = 2160        # same 90d window heavy_sweep.py's grid search uses
EXPIRY_HOURS = 48         # must match eod_resolver.py's EXPIRY_BARS exactly
PROCESS_WORKERS = min(8, os.cpu_count() or 4)
OUTPUT_CSV = "scripts/sim_deadline_aware_scoring_results.csv"


@dataclass
class SymbolSimResult:
    symbol: str
    method: str              # "original" (today's uncapped scoring) | "corrected" (48h-capped scoring)
    brick_size: float
    sl_bricks: int
    tp_bricks: int
    selection_sharpe: float  # the Sharpe value the combo was CHOSEN by (uncapped for original, capped for corrected)
    total_trades: int        # == "qualified" for this table's purposes
    wins: int
    losses: int
    expired: int             # hold > 48h under the SAME ground-truth rule, both methods
    win_pct_of_settled: float | None
    total_pnl_bricks: float
    total_pnl_dollars: float
    avg_hold_hours_settled: float | None


def _simulate_symbol(symbol: str, window: list[float]) -> list[SymbolSimResult]:
    """Runs in a separate process (ProcessPoolExecutor) — module-level,
    self-contained, no shared state from the parent."""
    logging.getLogger("renko.trade_journal").setLevel(logging.ERROR)
    logging.getLogger("regime_platform.services.execution_deprecation").setLevel(logging.ERROR)

    import numpy as np
    from renko.config import RenkoConfig
    from renko.pipeline import RenkoPipeline
    from regime_platform.sweep_orchestrator.heavy_sweep import (
        MAX_SHARPE, MIN_RR, MIN_TRADES,
        SWEEP_SL_RANGE, SWEEP_TP_RANGE,
        compute_atr, generate_brick_sizes,
    )

    current_price = float(window[-1])
    atr = compute_atr(window, period=14)
    if atr <= 0:
        return []
    brick_sizes = generate_brick_sizes(atr, current_price)

    candidates = []  # each: dict with brick/sl/tp + raw trade list
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
                    candidates.append({
                        "brick_size": brick, "sl_bricks": sl, "tp_bricks": tp,
                        "trades": trades,
                    })
                except Exception:
                    continue

    def _corrected_pnls(trades: list[dict]) -> list[float]:
        out = []
        for t in trades:
            hold = t["exit_ts"] - t["entry_ts"]
            out.append(0.0 if hold > EXPIRY_HOURS else t["pnl_bricks"])
        return out

    def _sharpe(pnls: list[float]) -> float:
        if len(pnls) <= 1:
            return 0.0
        arr = np.array(pnls)
        std = arr.std(ddof=1)
        return float(arr.mean() / std) if std > 0 else 0.0

    def _passes_filters(sharpe: float, n_trades: int) -> bool:
        return sharpe <= MAX_SHARPE and n_trades >= MIN_TRADES

    # ── "original" selection: today's actual method — uncapped Sharpe ──
    original_scored = []
    for c in candidates:
        raw_pnls = [t["pnl_bricks"] for t in c["trades"]]
        sharpe = _sharpe(raw_pnls)
        if _passes_filters(sharpe, len(c["trades"])):
            original_scored.append((sharpe, c))
    original_best = max(original_scored, key=lambda x: x[0]) if original_scored else None

    # ── "corrected" selection: same trades, 48h-capped Sharpe ──
    corrected_scored = []
    for c in candidates:
        capped_pnls = _corrected_pnls(c["trades"])
        sharpe = _sharpe(capped_pnls)
        if _passes_filters(sharpe, len(c["trades"])):
            corrected_scored.append((sharpe, c))
    corrected_best = max(corrected_scored, key=lambda x: x[0]) if corrected_scored else None

    def _summarize(method: str, picked) -> SymbolSimResult | None:
        if picked is None:
            return None
        sharpe, c = picked
        trades = c["trades"]
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
        return SymbolSimResult(
            symbol=symbol,
            method=method,
            brick_size=c["brick_size"],
            sl_bricks=c["sl_bricks"],
            tp_bricks=c["tp_bricks"],
            selection_sharpe=round(sharpe, 4),
            total_trades=len(trades),
            wins=wins,
            losses=losses,
            expired=expired,
            win_pct_of_settled=round(100.0 * wins / n_settled, 1) if n_settled else None,
            total_pnl_bricks=round(total_pnl_bricks, 4),
            total_pnl_dollars=round(total_pnl_bricks * c["brick_size"], 2),
            avg_hold_hours_settled=round(sum(settled_hours) / len(settled_hours), 1) if settled_hours else None,
        )

    out = []
    r1 = _summarize("original", original_best)
    if r1:
        out.append(r1)
    r2 = _summarize("corrected", corrected_best)
    if r2:
        out.append(r2)
    return out


async def main() -> None:
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_candles_for_symbols
    from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols

    active = await fetch_active_symbols()
    print(f"Fetching most recent {WINDOW_BARS} hourly bars for {len(active)} symbols...", flush=True)
    batch = await fetch_candles_for_symbols(active, n_bars=WINDOW_BARS, interval="1h")

    jobs = [
        (sym["symbol"], batch[sym["symbol"]][-WINDOW_BARS:])
        for sym in active
        if len(batch.get(sym["symbol"], [])) >= WINDOW_BARS
    ]
    print(f"Simulating {len(jobs)} symbols, {PROCESS_WORKERS} worker processes...", flush=True)

    t_start = time.time()
    loop = asyncio.get_running_loop()
    all_results: list[SymbolSimResult] = []
    with ProcessPoolExecutor(max_workers=PROCESS_WORKERS) as pool:
        futures = [loop.run_in_executor(pool, _simulate_symbol, s, w) for s, w in jobs]
        for i, fut in enumerate(asyncio.as_completed(futures), start=1):
            rows = await fut
            elapsed = time.time() - t_start
            for r in rows:
                all_results.append(r)
                print(f"[{i}/{len(jobs)}] {elapsed:6.0f}s — {r.symbol} ({r.method}): "
                      f"trades={r.total_trades} win%={r.win_pct_of_settled} "
                      f"expired={r.expired} pnl_bricks={r.total_pnl_bricks}", flush=True)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(SymbolSimResult.__annotations__))
        writer.writeheader()
        for r in all_results:
            writer.writerow(asdict(r))
    print(f"\nWrote {len(all_results)} rows to {OUTPUT_CSV} in {time.time() - t_start:.0f}s", flush=True)

    print()
    header = (f"{'Symbol':<10} {'Method':<10} {'brick':>8} {'sl':>3} {'tp':>3} "
              f"{'trades':>7} {'win':>5} {'loss':>5} {'exp':>5} {'win%':>6} {'pnl$':>10} {'avgHrs':>7}")
    print(header)
    print("-" * len(header))
    for r in sorted(all_results, key=lambda r: (r.symbol, r.method)):
        print(
            f"{r.symbol:<10} {r.method:<10} {r.brick_size:>8.4f} {r.sl_bricks:>3} {r.tp_bricks:>3} "
            f"{r.total_trades:>7} {r.wins:>5} {r.losses:>5} {r.expired:>5} "
            f"{r.win_pct_of_settled if r.win_pct_of_settled is not None else '-':>6} "
            f"{r.total_pnl_dollars:>10.2f} "
            f"{r.avg_hold_hours_settled if r.avg_hold_hours_settled is not None else '-':>7}"
        )


if __name__ == "__main__":
    asyncio.run(main())
