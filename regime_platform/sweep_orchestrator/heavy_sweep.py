"""
Heavy Sweep Module (Phase B)
═════════════════════════════

Ports the heavy sweep from the deprecated Supabase Edge Function
(``supabase/functions/sweep/index.ts:runHeavySweepForSymbol``) to the
backend Python ``SweepOrchestrator``.

Heavy sweep runs on a per-symbol cron schedule defined by
``nt_symbol.sweep_cron`` (default ``0 20 * * *`` — daily 20:00 UTC, changed
from weekly 2026-09-08, worklog
20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md). Runs unconditionally
for every symbol every day — no confidence gate, no skip branch (see
"TimesFM confidence gate — removed" below).
NOTE: APScheduler's ``from_crontab`` uses PYTHON weekday convention
(0=Monday) — irrelevant now that the schedule is daily, still relevant
if a per-symbol override ever reintroduces a weekday-specific cron.

Per-symbol heavy sweep does:
  1. Fetch 2160 hourly candles (90 d) from TDVA
  2. Fetch TimesFM forecast (informational input to the EV blend below —
     does not gate whether this sweep runs)
  3. ATR(14) + 5 brick sizes (multipliers 0.75, 1.0, 1.5, 2.0, 3.0), plus
     3 optional ATR-anchored candidates (0.15x/0.3x/0.6x ATR) when
     heavy_sweep_atr_anchored_bricks_mode != off (config/feature_flags.yaml,
     default off — see worklog
     20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md)
  4. Grid search: up to 8 bricks × 3 SL × 6 TP = up to 144 combos (RenkoPipeline)
  5. HMM regime + Kelly sizing (parallel with grid)
  6. Pick best combo by Sharpe (MIN_RR/MAX_SHARPE/MIN_TRADES, default
     2.0/10.0/10 — config/feature_flags.yaml's min_rr/heavy_sweep_grid)
  7. Write optimised brick params to config/brick_params.yaml
  8. Compute EV with Markov T^N (Phase A)
  9. Build qualified signal payload and deliver it directly to Discord

(A "skip if already CALIBRATED" step and its sweep_skip_until value
existed here through 2026-09-09 — removed as fully dead code, worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md SS16: it was
computed and logged every run but never consumed by anything. Leftover
half of the TimesFM-confidence-gate removal in SS13 — the skip mechanism
it fed was deleted then, this half wasn't.)

T4 w/GPU runs 14 symbols in <30 s total — no stagger needed, no shadow
mode, no transition period. All Supabase Edge Functions are already OFF.

TimesFM confidence gate — removed 2026-09-09 (worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §13). From
2026-06-21 (migration 022_reduce_heavy_with_timesfm.sql) through today,
a gate here skipped the entire re-optimization whenever
``abs(p_timesfm - 0.5) > 0.10``. Removed outright, not bounded with a
timer, for two independent, each-sufficient reasons: (1) its only
justification was compute/timeout pressure on the old Supabase Edge
Function deployment (150s timeout colliding with Render cold starts
under burst load) — moot since the 2026-08 migration off Supabase Edge
Functions onto Lightning AI Studio removed that infrastructure
constraint entirely, and even before that migration the 2026-09-08
real-cost measurement (~29s/symbol, ~7 min for a full 14-symbol pass)
had already falsified the compute-savings premise on its own terms; (2)
the signal it gated on (TimesFM's directional confidence) was never
causally connected to the thing it was supposedly protecting (whether
the brick/SL/TP geometry still fits current volatility) — a confident
direction call says nothing about bracket fit. A 72h force-refresh
override (``heavy_sweep_force_refresh_hours``) was added 2026-09-09 to
bound the damage after 5 of 14 symbols were found skipped for 2+ days
straight, then removed again the same day once it was clear the gate
itself — not its duration — was the actual defect: bounding an
unjustified decision with a timer is not a substitute for removing the
decision, and time is not a fix for a logic error. TimesFM's forecast is
still fetched and fed into the EV blend every run; it simply no longer
gates whether the run happens at all.

NOTE (2026-08-09, historical): concurrency is CAPPED at
``HEAVY_SWEEP_MAX_CONCURRENCY`` symbols at a time. Originally added
because firing all 14 via ``asyncio.gather`` exhausted the (now-retired,
2026-08-25) shared Redis pool. The cap remains because it also keeps the
CPU-bound grid-search thread pool sane (each symbol spawns 3 to_thread
tasks; 14 × 3 = 42 threads otherwise).

This module is a mix-in: ``SweepOrchestrator`` inherits from
``HeavySweepMixin`` so the heavy-sweep methods have access to
``self._ev_engine``, ``self._sizer``, ``self._scheduler``, etc.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from regime_platform.core.settings import settings
from regime_platform.core.feature_flags import (
    get_heavy_sweep_max_sharpe,
    get_heavy_sweep_min_trades,
    get_min_rr,
)
from regime_platform.services.statistics.ev_engine import P_WIN_WEIGHTS_FULL
from regime_platform.sweep_orchestrator.eod_resolver import EXPIRY_BARS

# EXPIRY_BARS: single source of truth for "how many post-entry hours a
# real signal gets before eod_resolver.py calls it expired," imported
# (not duplicated) so the grid search's deadline-aware scoring (below)
# can never silently drift from what live resolution actually enforces —
# same reasoning as MIN_RR's own shared-config history (see that
# constant's comment further down).

# Note: write_brick_params + fetch_calibration_bias + log_sweep_result +
# fetch_candles_for_symbols are imported lazily inside their callers to
# avoid circular-import issues when hmmlearn (a heavy dep pulled in by
# regime_engine via orchestrator) is not installed in the dev env.
# All three are stable public APIs of their respective modules.

logger = logging.getLogger("regime.sweep.heavy")


# ─── Grid search constants (ported from edge fn _shared/utils.ts) ────────

SWEEP_SL_RANGE = [3, 5, 7]
SWEEP_TP_RANGE = [4, 5, 6, 8, 10, 12]

# MIN_RR: shared with the orchestrator's qualification gate via the same
# config/feature_flags.yaml value (default 2.0) — 2026-08-30
# config-centralization fix, moved off two independent env vars/Studio
# Secrets that had already diverged once in production (verified
# 2026-08-13: EURUSD/GBPUSD/USDCAD emergency sweeps failed with
# no_combo_passed_filters because heavy_sweep.py's hardcoded MIN_RR=2.0
# disagreed with orchestrator.py's env-var override MIN_RR=1.5). One
# shared config value, read by both modules AND passed explicitly into
# heavy_sweep_reachability.py's reachability_aware_pick (Phase 3a) below,
# makes that divergence structurally impossible now, not just less
# likely. See orchestrator.py:105-116 and config/feature_flags.yaml's
# own comment on ``min_rr`` for the full history.
MIN_RR = get_min_rr()
MAX_SHARPE = get_heavy_sweep_max_sharpe()
MIN_TRADES = get_heavy_sweep_min_trades()

# Heavy-sweep concurrency cap. Originally added (2026-08-09) because firing
# all 14 symbols at once exhausted the shared Redis pool; kept because it
# also caps the CPU-bound grid-search thread pool (each symbol spawns 3
# to_thread tasks — 14 × 3 = 42 threads otherwise). The grid searches
# dominate wall time anyway.
HEAVY_SWEEP_MAX_CONCURRENCY = 4


# ─── Result dataclass ────────────────────────────────────────────────────


@dataclass
class ComboResult:
    """One row of the renko grid search."""
    brick_size: float
    sl_bricks: int
    tp_bricks: int
    sharpe: float
    total_trades: int
    win_rate: float
    profit_factor: float = 0.0
    total_pnl_bricks: float = 0.0
    max_drawdown_bricks: float = 0.0
    # Root cause found 2026-09-08 (worklog
    # 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md): `sharpe` above is
    # computed by RenkoPipeline over the FULL uncapped trade set — a
    # simulated trade that takes 200+ hours to close still counts as a win
    # there, even though eod_resolver.py gives every real signal only
    # EXPIRY_HOURS to prove itself before calling it "expired." This field
    # recomputes the identical Sharpe formula
    # (mean(pnl_bricks)/std(pnl_bricks, ddof=1)) but zeroes any trade whose
    # hold time exceeds EXPIRY_HOURS first — see
    # deadline_aware_sharpe()'s docstring. Always computed (cheap — same
    # trade list already in hand); only USED for selection when
    # heavy_sweep_deadline_aware_scoring_mode != "off".
    deadline_aware_sharpe: float = 0.0


@dataclass
class HeavySweepResult:
    """Result of a single-symbol heavy sweep."""
    symbol: str
    skipped: bool = False
    skip_reason: str = ""
    error: Optional[str] = None
    best_combo: Optional[ComboResult] = None
    regime_label: str = "unknown"
    regime_conf: float = 0.0
    kelly_f: float = 0.0
    p_timesfm: Optional[float] = None
    qualified: bool = False
    n_combos_tested: int = 0
    duration_ms: int = 0
    persisted_yaml: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


# ─── Helpers (module-level so they can be unit-tested in isolation) ──────


def compute_atr(prices: list[float], period: int = 14) -> float:
    """Compute Average True Range over the last ``period`` bars.

    ATR is computed from hourly close-to-close ranges (no high/low data
    available from the close-only batch fetcher). This is a simplification
    of the full ATR formula (which uses True Range = max(H-L, |H-C_prev|,
    |L-C_prev|)), but it's what the original TS implementation effectively
    did too because the price array is close-only.

    Returns 0.0 if insufficient data.
    """
    if len(prices) < period + 1:
        return 0.0
    arr = np.array(prices, dtype=float)
    # True range approximation: |close[i] - close[i-1]|
    tr = np.abs(np.diff(arr))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    # Simple moving average of the last `period` true ranges
    return float(np.mean(tr[-period:]))


def generate_brick_sizes(atr: float, price: float) -> list[float]:
    """5 brick sizes scaled by volatility regime.

    Ported verbatim from ``supabase/functions/_shared/utils.ts:1718``.
    Multipliers: [0.75, 1.0, 1.5, 2.0, 3.0] of ``base = price × pct / 100``.
    """
    if atr <= 0 or price <= 0:
        return [0.5]
    vol_pct = (atr / price) * 100.0
    if vol_pct > 3.0:
        base_brick_pct = 0.75    # high vol (crypto, TSLA)
    elif vol_pct > 1.5:
        base_brick_pct = 0.60    # medium vol (most stocks)
    elif vol_pct > 0.5:
        base_brick_pct = 0.40    # low vol (indices, commodities)
    else:
        base_brick_pct = 0.25    # forex
    base = (price * base_brick_pct) / 100.0
    multipliers = [0.75, 1.0, 1.5, 2.0, 3.0]
    # L1: round to 6dp, not 4dp — 4dp truncates sub-pip granularity for
    # FX/metals (EURUSD-class bricks), silently narrowing the tested grid.
    bricks = [round(base * m, 6) for m in multipliers]
    # Deduplicate (forex can produce identical bricks after rounding)
    seen: set[float] = set()
    out: list[float] = []
    for b in bricks:
        if b not in seen and b > 0:
            seen.add(b)
            out.append(b)
    return out or [0.5]


def generate_atr_anchored_brick_sizes(atr: float) -> list[float]:
    """3 additional brick-size candidates anchored directly to ATR.

    Phase A of worklog
    20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md:
    ``generate_brick_sizes``'s 5 candidates are a coarse ``price × pct``
    formula, never anchored to the symbol's own ATR — the grid can (and,
    per ``nt_bracket_reachability``, for XAUUSD/XAUEUR/XAUAUD routinely
    does) contain zero candidates near the 0.5-1x ATR distance bucket that
    resolves reliably. These three multipliers give the grid something to
    find there. Additive only — callers decide whether/how to combine this
    with ``generate_brick_sizes``'s output (see
    ``heavy_sweep_atr_anchored_bricks_mode`` in ``config/feature_flags.yaml``).

    2026-09-07 CORRECTION (real-data backfill, same worklog): the original
    [0.5, 1.0, 2.0] multipliers were mathematically incapable of ever
    reaching the "0.5-1"/"1-2" distance/ATR buckets — the grid's own
    ``SWEEP_SL_RANGE``/``SWEEP_TP_RANGE`` combined with ``MIN_RR`` reject
    every ``tp_bricks`` below 5 (``tp=4`` never clears ``MIN_RR=1.5`` at
    any ``sl``), so the smallest reachable distance was
    ``5 * 0.5 = 2.5x ATR`` ("2-4" bucket) — never the proven-good "0.5-1"/
    "1-2" buckets. Confirmed empirically: a 70-anchor historical replay
    against real TDVA data (14 symbols) landed in "4+" or "2-4" every
    single time, "0.5-1" zero times. These multipliers are chosen so
    ``tp_bricks=5`` (the grid's own minimum viable value) lands inside
    "0.5-1"/"1-2"/"2-4" respectively: ``5 * 0.15 = 0.75x`` ("0.5-1"),
    ``5 * 0.3 = 1.5x`` ("1-2"), ``5 * 0.6 = 3.0x`` ("2-4", kept for a
    same-bucket comparison point against the old multipliers).
    """
    if atr <= 0:
        return []
    multipliers = [0.15, 0.3, 0.6]
    seen: set[float] = set()
    out: list[float] = []
    for m in multipliers:
        b = round(atr * m, 6)
        if b > 0 and b not in seen:
            seen.add(b)
            out.append(b)
    return out


def deadline_aware_sharpe(trades: list[dict]) -> float:
    """Sharpe over a trade set, but with any trade held longer than
    ``EXPIRY_BARS`` hours scored as 0 PnL first.

    Root cause fix, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md:
    ``ComboResult.sharpe`` (and the ``win_rate``/etc. it's derived from)
    comes from ``RenkoPipeline``'s own ``TradeJournal.get_stats()``, which
    scores every trade however long it actually took to close — no
    concept of a deadline. Live, ``eod_resolver.py`` gives every real
    signal only ``EXPIRY_BARS`` hours before calling it "expired" (PnL 0,
    per that module's ``compute_pnl``... as of the 2026-09-08 fix, expired
    is scored as real mark-to-market, not a flat 0 — but a *backtest*
    candidate that would clearly expire live still shouldn't get credit
    for a "win" it will never actually be given time to realize, which is
    exactly what this function corrects for at SELECTION time). Verified
    against ``renko/trade_journal.py`` before writing this: ``entry_ts``/
    ``exit_ts`` on each trade dict are bar indices when the backtest is
    run without explicit timestamps (exactly how ``_renko_grid_search_sync``
    calls it), so ``exit_ts - entry_ts`` is hours-held, directly
    comparable to ``EXPIRY_BARS``. The Sharpe formula itself
    (``mean(pnl_bricks) / std(pnl_bricks, ddof=1)``) is reused verbatim
    from ``TradeJournal.get_stats()``, not reinvented.

    ``trades`` is the raw ``result["trades"]`` list from
    ``RenkoPipeline.backtest_with_analytics`` (list of ``TradeRecord.to_dict()``).
    """
    if len(trades) <= 1:
        return 0.0
    capped_pnls = np.array([
        0.0 if (t["exit_ts"] - t["entry_ts"]) > EXPIRY_BARS else t["pnl_bricks"]
        for t in trades
    ])
    std = capped_pnls.std(ddof=1)
    return float(capped_pnls.mean() / std) if std > 0 else 0.0


def pick_best_combo(
    combos: list[ComboResult],
    min_rr: float = MIN_RR,
    max_sharpe: float = MAX_SHARPE,
    min_trades: int = MIN_TRADES,
    sharpe_fn=lambda c: c.sharpe,
) -> Optional[ComboResult]:
    """Pick the best combo by Sharpe, applying filter gates.

    Filters:
      • rr = tp/sl >= min_rr
      • sharpe <= max_sharpe (reject absurd outliers — likely overfit)
      • total_trades >= min_trades

    ``sharpe_fn`` selects which Sharpe value on ``ComboResult`` both the
    ``max_sharpe`` filter and the final argmax use — defaults to the
    original uncapped ``c.sharpe`` (unchanged behavior for every existing
    caller). ``select_best_combo_deadline_aware`` passes
    ``lambda c: c.deadline_aware_sharpe`` instead.
    """
    candidates = [
        c for c in combos
        if c.tp_bricks / max(c.sl_bricks, 1) >= min_rr
        and sharpe_fn(c) <= max_sharpe
        and c.total_trades >= min_trades
    ]
    if not candidates:
        return None
    return max(candidates, key=sharpe_fn)


@dataclass
class AtrAnchoredSelection:
    """Result of ``select_best_combo_with_atr_anchoring``.

    ``chosen`` is what the caller should actually use (the real,
    production combo). ``baseline``/``widened``/``changed`` are always
    populated when ``mode != "off"``, purely for logging/comparison —
    ``baseline`` is "what today's original-5-candidate grid would have
    picked" and ``widened`` is "what the full ATR-anchored grid picks."
    """
    chosen: Optional[ComboResult]
    baseline: Optional[ComboResult] = None
    widened: Optional[ComboResult] = None
    changed: bool = False


def select_best_combo_with_atr_anchoring(
    grid_results: list[ComboResult],
    original_brick_sizes: set[float],
    mode: str,
    min_rr: float = MIN_RR,
    max_sharpe: float = MAX_SHARPE,
    min_trades: int = MIN_TRADES,
) -> AtrAnchoredSelection:
    """Pick the real (production) combo, plus — in shadow/live mode —
    what the ATR-anchored-widened grid would have picked instead.

    Phase A of worklog
    20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md.
    Pure function (no I/O) so it's unit-testable in isolation, same shape
    as ``pick_best_combo``/``heavy_sweep_reachability.reachability_aware_pick``.

    Args:
        grid_results: the FULL grid — original candidates plus any
            ATR-anchored ones the caller backtested (empty/absent when
            mode is ``"off"``).
        original_brick_sizes: the brick sizes from ``generate_brick_sizes``
            only (never includes ATR-anchored candidates).
        mode: ``"off"`` | ``"shadow"`` | ``"live"``.

    Semantics:
      • ``"off"``: ``chosen`` is the pick over ``grid_results`` as given
        (the caller is expected to not have generated ATR-anchored
        candidates at all in this mode) — byte-identical to pre-Phase-A
        behavior.
      • ``"shadow"``: ``chosen`` is restricted to ``original_brick_sizes``
        only — zero live-behavior change. ``widened`` is computed over
        the full grid for comparison/logging only; it never overrides
        ``chosen``.
      • ``"live"``: ``chosen`` becomes ``widened`` whenever one exists,
        falling back to the original-only pick otherwise.
    """
    if mode == "off":
        chosen = pick_best_combo(grid_results, min_rr, max_sharpe, min_trades)
        return AtrAnchoredSelection(chosen=chosen)

    baseline = pick_best_combo(
        [c for c in grid_results if c.brick_size in original_brick_sizes],
        min_rr, max_sharpe, min_trades,
    )
    widened = pick_best_combo(grid_results, min_rr, max_sharpe, min_trades)
    changed = widened is not None and (
        baseline is None or (
            widened.brick_size, widened.sl_bricks, widened.tp_bricks
        ) != (baseline.brick_size, baseline.sl_bricks, baseline.tp_bricks)
    )
    chosen = widened if (mode == "live" and widened is not None) else baseline
    return AtrAnchoredSelection(
        chosen=chosen, baseline=baseline, widened=widened, changed=changed,
    )


@dataclass
class DeadlineAwareSelection:
    """Result of ``select_best_combo_deadline_aware``.

    ``chosen`` is what the caller should actually use. ``uncapped``/
    ``corrected``/``changed`` are always populated when ``mode != "off"``,
    purely for logging/comparison — ``uncapped`` is "what today's
    unmodified Sharpe would pick," ``corrected`` is "what the same
    candidates pick when scored with the 48h deadline applied first."
    """
    chosen: Optional[ComboResult]
    uncapped: Optional[ComboResult] = None
    corrected: Optional[ComboResult] = None
    changed: bool = False


def select_best_combo_deadline_aware(
    grid_results: list[ComboResult],
    mode: str,
    min_rr: float = MIN_RR,
    max_sharpe: float = MAX_SHARPE,
    min_trades: int = MIN_TRADES,
) -> DeadlineAwareSelection:
    """Pick the real (production) combo, plus — in shadow/live mode —
    what deadline-aware scoring picks from the SAME candidate set instead.

    Root cause fix, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.
    Unlike ``select_best_combo_with_atr_anchoring`` (which changed WHICH
    candidates are considered and turned out to be solving the wrong
    problem), this changes nothing about the candidate grid — only how
    each candidate already in it gets scored. Pure function (no I/O), same
    shape as the other two selection helpers in this module.

    Semantics:
      • ``"off"``: ``chosen`` is today's exact pick (``c.sharpe``,
        uncapped) — byte-identical to pre-fix behavior.
      • ``"shadow"``: ``chosen`` stays the uncapped pick — zero
        live-behavior change. ``corrected`` (picked by
        ``c.deadline_aware_sharpe``) is computed for comparison/logging
        only.
      • ``"live"``: ``chosen`` becomes ``corrected`` whenever one exists,
        falling back to the uncapped pick otherwise.
    """
    if mode == "off":
        chosen = pick_best_combo(grid_results, min_rr, max_sharpe, min_trades)
        return DeadlineAwareSelection(chosen=chosen)

    uncapped = pick_best_combo(grid_results, min_rr, max_sharpe, min_trades)
    corrected = pick_best_combo(
        grid_results, min_rr, max_sharpe, min_trades,
        sharpe_fn=lambda c: c.deadline_aware_sharpe,
    )
    changed = corrected is not None and (
        uncapped is None or (
            corrected.brick_size, corrected.sl_bricks, corrected.tp_bricks
        ) != (uncapped.brick_size, uncapped.sl_bricks, uncapped.tp_bricks)
    )
    chosen = corrected if (mode == "live" and corrected is not None) else uncapped
    return DeadlineAwareSelection(
        chosen=chosen, uncapped=uncapped, corrected=corrected, changed=changed,
    )


# ─── Mix-in ──────────────────────────────────────────────────────────────


class HeavySweepMixin:
    """Mix-in providing heavy-sweep methods to ``SweepOrchestrator``.

    The orchestrator inherits this so the heavy-sweep methods can access
    ``self._ev_engine``, ``self._sizer``, ``self._scheduler``, etc. without
    passing them as arguments.

    Methods added:
      • ``run_heavy_sweep_for_symbol(sym)`` — single-symbol heavy sweep
      • ``_heavy_sweep_group(cron_expr, symbols)`` — cron tick handler
      • ``_renko_grid_search_sync(prices, brick_sizes)`` — CPU-bound grid
      • ``_regime_sync(prices)`` — CPU-bound HMM regime
      • ``_kelly_sync(prices, asset_class)`` — CPU-bound Kelly sizing
      • ``_persist_brick_params_to_yaml(symbol, brick, sl, tp)`` — YAML write

    (``_fetch_p_historical_cached`` — the cached ``p_historical`` EV-blend
    fetch (R3, 2026-08-23) — was removed 2026-09-09,
    worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md:
    ``p_historical`` and ``calibration_enforcement.apply_calibration_adjustment``
    were two independently-computed mechanisms both correcting for symbol
    confidence off the same evidence; consolidated to
    ``apply_calibration_adjustment`` — the hard, post-hoc mechanism — as
    the sole calibration-correction path.)
    """

    # ── Cron tick handler ────────────────────────────────────────────

    async def _heavy_sweep_group(
        self,
        cron_expr: str,
        symbols: list[dict[str, Any]],
    ) -> None:
        """Process all symbols in one heavy-sweep cron group.

        T4 w/GPU runs 14 symbols in <30 s total — no stagger needed.
        Symbols are processed concurrently via ``asyncio.gather`` so the
        CPU-bound grid searches parallelise across the event loop's
        thread-pool workers.
        """
        logger.info(
            "[sweep:heavy] Cron tick [%s]: %d symbols — %s",
            cron_expr, len(symbols), [s["symbol"] for s in symbols],
        )
        if not symbols:
            return

        # Fetch 90 d × 24 h = 2160 hourly candles for ALL symbols in batch.
        # TDVA's /api/price/batch auto-chunks to 10 symbols per HTTP call.
        from regime_platform.sweep_orchestrator.price_fetcher import (
            SWEEP_CANDLE_HOURS, fetch_candles_for_symbols,
        )
        try:
            valid_prices = await fetch_candles_for_symbols(
                symbols, n_bars=SWEEP_CANDLE_HOURS, interval="1h",
            )
        except Exception as exc:
            logger.error("[sweep:heavy] Batch fetch failed for cron [%s]: %s", cron_expr, exc)
            return

        if not valid_prices:
            logger.warning("[sweep:heavy] No candles fetched for cron [%s]", cron_expr)
            return

        symbols_with_prices = [
            s for s in symbols if s["symbol"] in valid_prices
            and len(valid_prices[s["symbol"]]) >= 100
        ]
        if not symbols_with_prices:
            logger.warning(
                "[sweep:heavy] No symbols with >=100 candles for cron [%s]", cron_expr,
            )
            return

        # ── HMM: bootstrap any symbols not yet bootstrapped ──────────
        # The heavy sweep fetches 2160 bars (90d) — more than enough for
        # the HMM bootstrap (which only needs ~300). If a light sweep has
        # already bootstrapped the symbol, this is a no-op.
        for sym in symbols_with_prices:
            symbol = sym["symbol"]
            if symbol not in self._hmm_bootstrapped:
                pushed = self._hmm_model.bootstrap_from_ohlcv(
                    symbol, valid_prices[symbol],
                )
                if pushed > 0:
                    self._hmm_bootstrapped.add(symbol)

        # Concurrent per-symbol heavy sweeps. Each runs its CPU-bound
        # grid search via asyncio.to_thread, so they don't block each
        # other or the event loop. Concurrency is CAPPED to bound the
        # to_thread fan-out (see HEAVY_SWEEP_MAX_CONCURRENCY).
        semaphore = asyncio.Semaphore(HEAVY_SWEEP_MAX_CONCURRENCY)

        async def _run_capped(sym: dict[str, Any]):
            async with semaphore:
                return await self.run_heavy_sweep_for_symbol(
                    sym, valid_prices[sym["symbol"]],
                )

        tasks = [_run_capped(sym) for sym in symbols_with_prices]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Summary log
        ok = sum(1 for r in results if isinstance(r, HeavySweepResult) and not r.error)
        skipped = sum(1 for r in results if isinstance(r, HeavySweepResult) and r.skipped)
        failed = sum(1 for r in results if isinstance(r, Exception) or (isinstance(r, HeavySweepResult) and r.error))
        logger.info(
            "[sweep:heavy] Cron [%s] complete: %d ok, %d skipped, %d failed",
            cron_expr, ok, skipped, failed,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error("[sweep:heavy] Symbol task crashed: %s", r)

    # ── Per-symbol heavy sweep ───────────────────────────────────────

    async def run_heavy_sweep_for_symbol(
        self,
        sym: dict[str, Any],
        prices: Optional[list[float]] = None,
    ) -> HeavySweepResult:
        """Run the heavy sweep for one symbol, holding the symbol's
        in-process sweep lock for the duration.

        2026-09-08 (worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md):
        now that heavy sweep runs daily instead of weekly, an uncoordinated
        overlap with a light-sweep tick for the same symbol (every 5 min)
        went from a rare weekly coincidence to a real daily one. Reuses the
        SAME ``self._sweep_locks`` per-symbol ``asyncio.Lock`` the light
        sweep already holds during ``_process_symbol`` (orchestrator.py) —
        no new lock type. Heavy sweep BLOCKS (awaits the lock) rather than
        skipping, since it's not latency-sensitive and a light-sweep tick
        holds the lock for at most a few seconds; the light sweep's
        existing "if lock.locked(): skip" behavior means a light tick that
        lands during a heavy sweep's ~30-90s run is simply skipped until
        the next 5-minute tick, rather than reading/writing
        ``config/brick_params.yaml`` concurrently with the heavy sweep.
        """
        symbol = sym["symbol"]
        lock = self._sweep_locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            return await self._run_heavy_sweep_for_symbol_locked(sym, prices)

    async def _run_heavy_sweep_for_symbol_locked(
        self,
        sym: dict[str, Any],
        prices: Optional[list[float]] = None,
    ) -> HeavySweepResult:
        """Heavy sweep implementation — see ``run_heavy_sweep_for_symbol``
        for the lock this always runs under.

        Args:
            sym: nt_symbol row dict. Must have ``symbol``, ``asset_class``,
                and optionally ``tradingview_symbol``.
            prices: Pre-fetched 90 d hourly closes. If None, fetched inline.
                The cron-tick handler pre-fetches in batch for efficiency;
                standalone callers can pass None.

        Returns:
            HeavySweepResult with the best combo, regime, Kelly, and
            whether the result was qualified for signal push.
        """
        from regime_platform.sweep_orchestrator.orchestrator import (
            _compute_trade_signal,
            _derive_venue,
            _fetch_timesfm_cached,
            MARKOV_HOLD_STEPS,
            MIN_P_WIN,
        )

        symbol = sym["symbol"]
        start_ts = time.time()
        result = HeavySweepResult(symbol=symbol)

        try:
            # ── 1. Fetch candles if not pre-supplied ──────────────────
            if prices is None:
                from regime_platform.sweep_orchestrator.price_fetcher import (
                    SWEEP_CANDLE_HOURS, fetch_candles_for_symbols,
                )
                batch = await fetch_candles_for_symbols([sym], n_bars=SWEEP_CANDLE_HOURS)
                prices = batch.get(symbol, [])
            if len(prices) < 100:
                result.error = f"insufficient_data ({len(prices)} bars, need >=100)"
                logger.warning("[sweep:heavy] %s: %s", symbol, result.error)
                return result

            # ── 2. TimesFM forecast (informational — no gate) ──────────
            # No confidence-based skip here. Removed 2026-09-09 — see the
            # module docstring's "TimesFM confidence gate — removed"
            # section for the full history. This sweep always runs to
            # completion; TimesFM's forecast is fetched purely as an
            # input to the EV blend further down.
            p_timesfm_result = await _fetch_timesfm_cached(symbol, prices)
            p_timesfm: Optional[float] = None
            if p_timesfm_result is not None:
                p_timesfm, _ = p_timesfm_result
            result.p_timesfm = p_timesfm

            # ── 3. ATR(14) + 5 brick sizes ────────────────────────────
            current_price = float(prices[-1])
            atr = compute_atr(prices, period=14)
            brick_sizes = generate_brick_sizes(atr, current_price)

            # ATR-anchored candidates (Phase A, worklog
            # 20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md).
            # Additive only — see heavy_sweep_atr_anchored_bricks_mode's
            # own comment in config/feature_flags.yaml for off/shadow/live.
            from regime_platform.core.feature_flags import (
                get_heavy_sweep_atr_anchored_bricks_mode,
            )

            atr_anchored_mode = get_heavy_sweep_atr_anchored_bricks_mode()
            original_brick_sizes = set(brick_sizes)
            atr_anchored_sizes: list[float] = []
            if atr_anchored_mode != "off":
                atr_anchored_sizes = [
                    b for b in generate_atr_anchored_brick_sizes(atr)
                    if b not in original_brick_sizes
                ]
            all_brick_sizes = brick_sizes + atr_anchored_sizes
            logger.info(
                "[sweep:heavy] %s: ATR=%.4f, price=%.2f, bricks=%s%s",
                symbol, atr, current_price,
                [f"{b:.4f}" for b in brick_sizes],
                (
                    f" (+ATR-anchored {[f'{b:.4f}' for b in atr_anchored_sizes]}, "
                    f"mode={atr_anchored_mode})"
                ) if atr_anchored_sizes else "",
            )

            # ── 4. Grid + regime + Kelly in parallel ──────────────────
            # Each runs in asyncio.to_thread so the event loop stays
            # responsive (light-sweep ticks can fire concurrently).
            grid_task = asyncio.to_thread(
                self._renko_grid_search_sync, prices, all_brick_sizes, symbol,
            )
            regime_task = asyncio.to_thread(self._regime_sync, prices)
            kelly_task = asyncio.to_thread(
                self._kelly_sync, prices, sym.get("asset_class", ""),
            )
            grid_results, (regime_model, regime_snapshot), kelly_result = await asyncio.gather(
                grid_task, regime_task, kelly_task,
            )

            result.n_combos_tested = len(grid_results)
            result.regime_label = regime_snapshot.regime_label or "unknown"
            result.regime_conf = regime_snapshot.confidence
            result.kelly_f = kelly_result.recommended_f

            # ── 5. Pick best combo by Sharpe ──────────────────────────
            # See select_best_combo_with_atr_anchoring's docstring for the
            # off/shadow/live semantics — mode="off" is byte-identical to
            # pre-Phase-A behavior (all_brick_sizes == brick_sizes above).
            selection = select_best_combo_with_atr_anchoring(
                grid_results, original_brick_sizes, atr_anchored_mode,
            )
            best = selection.chosen
            if atr_anchored_mode in ("shadow", "live"):
                def _fmt(c: Optional[ComboResult]) -> str:
                    if c is None:
                        return "none"
                    return (
                        f"brick={c.brick_size:.4f} sl={c.sl_bricks} tp={c.tp_bricks} "
                        f"sharpe={c.sharpe:.4f}"
                    )

                logger.info(
                    "[sweep:heavy] %s: ATR-anchored bricks %s — baseline=%s | "
                    "widened=%s (changed=%s)",
                    symbol, atr_anchored_mode,
                    _fmt(selection.baseline), _fmt(selection.widened), selection.changed,
                )
                if atr_anchored_mode == "live" and selection.widened is not None:
                    logger.info(
                        "[sweep:heavy] %s: LIVE ATR-anchored pick applied", symbol,
                    )

                # Persist the comparison so the flip-to-live decision can be
                # made from a query, not by grepping logs — same reasoning
                # as heavy_sweep_reachability_mode's own shadow table.
                # Reuses bracket_reachability.py's bucket math (via
                # heavy_sweep_reachability._bucket_for_combo) rather than
                # re-deriving it here.
                from regime_platform.sweep_orchestrator.heavy_sweep_reachability import (
                    _bucket_for_combo,
                )
                from regime_platform.sweep_orchestrator.supabase_client import (
                    record_heavy_sweep_atr_anchored_shadow,
                )
                asyncio.create_task(record_heavy_sweep_atr_anchored_shadow(
                    symbol,
                    changed=selection.changed,
                    baseline_bucket=_bucket_for_combo(selection.baseline, atr),
                    baseline_sharpe=selection.baseline.sharpe if selection.baseline else None,
                    widened_bucket=_bucket_for_combo(selection.widened, atr),
                    widened_sharpe=selection.widened.sharpe if selection.widened else None,
                ))

            # ── 5a2. Deadline-aware rescoring — root cause fix, worklog
            # 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md. Applied
            # after ATR-anchoring (independent re-ranking over the same
            # grid_results — with atr_anchored_mode="off", grid_results is
            # just the original 5-brick-size grid, matching exactly what
            # this fix was validated against). "off" reproduces `best`
            # above exactly (both read c.sharpe uncapped) — no behavior
            # change when both flags are off, which is today's shipped
            # default for both.
            from regime_platform.core.feature_flags import (
                get_heavy_sweep_deadline_aware_scoring_mode,
            )

            deadline_mode = get_heavy_sweep_deadline_aware_scoring_mode()
            deadline_selection = select_best_combo_deadline_aware(grid_results, deadline_mode)
            best = deadline_selection.chosen
            if deadline_mode in ("shadow", "live"):
                def _fmt2(c: Optional[ComboResult]) -> str:
                    if c is None:
                        return "none"
                    return (
                        f"brick={c.brick_size:.4f} sl={c.sl_bricks} tp={c.tp_bricks} "
                        f"sharpe={c.sharpe:.4f} deadline_sharpe={c.deadline_aware_sharpe:.4f}"
                    )

                logger.info(
                    "[sweep:heavy] %s: deadline-aware scoring %s — uncapped=%s | "
                    "corrected=%s (changed=%s)",
                    symbol, deadline_mode,
                    _fmt2(deadline_selection.uncapped), _fmt2(deadline_selection.corrected),
                    deadline_selection.changed,
                )
                if deadline_mode == "live" and deadline_selection.corrected is not None:
                    logger.info(
                        "[sweep:heavy] %s: LIVE deadline-aware pick applied", symbol,
                    )

                from regime_platform.sweep_orchestrator.supabase_client import (
                    record_heavy_sweep_deadline_aware_shadow,
                )
                asyncio.create_task(record_heavy_sweep_deadline_aware_shadow(
                    symbol,
                    changed=deadline_selection.changed,
                    uncapped_sharpe=(
                        deadline_selection.uncapped.sharpe
                        if deadline_selection.uncapped else None
                    ),
                    corrected_sharpe=(
                        deadline_selection.corrected.deadline_aware_sharpe
                        if deadline_selection.corrected else None
                    ),
                ))
            if best is None:
                result.error = (
                    f"no_combo_passed_filters "
                    f"(tested {len(grid_results)} combos, "
                    f"MIN_RR={MIN_RR}, MAX_SHARPE={MAX_SHARPE}, "
                    f"MIN_TRADES={MIN_TRADES})"
                )
                result.duration_ms = int((time.time() - start_ts) * 1000)
                logger.warning(
                    "[sweep:heavy] %s: %s", symbol, result.error,
                )
                # Trace row so the failure is visible in nt_sweep_result
                # (previously silent — 2026-08-09 showed only successes).
                await self._log_heavy_failure_trace(
                    symbol, sym.get("asset_class", ""), result,
                )
                return result
            result.best_combo = best
            logger.info(
                "[sweep:heavy] %s: best combo brick=%.4f sl=%d tp=%d "
                "sharpe=%.4f trades=%d win_rate=%.4f",
                symbol, best.brick_size, best.sl_bricks, best.tp_bricks,
                best.sharpe, best.total_trades, best.win_rate,
            )

            # ── 5a. Reachability-aware combo pick — Phase 3a (2026-08-28,
            # worklog 20260825_CALIBRATION_REFRAME_SCOPE.md "Phase 3
            # scope") ─────────────────────────────────────────────────
            # A live simulation found the Sharpe-only pick above
            # sometimes lands in a distance/ATR bucket with negative
            # realized expectancy, and sometimes the grid never even
            # generates a candidate in a good bucket at all. shadow mode
            # (default) just measures how often that happens, over the
            # SAME unfiltered grid_results — best stays exactly what
            # pick_best_combo chose above.
            from regime_platform.core.feature_flags import get_heavy_sweep_reachability_mode

            reachability_mode = get_heavy_sweep_reachability_mode()
            if reachability_mode != "off":
                from regime_platform.sweep_orchestrator.heavy_sweep_reachability import (
                    fetch_bias_by_bucket,
                    reachability_aware_pick,
                )
                from regime_platform.sweep_orchestrator.supabase_client import (
                    record_heavy_sweep_reachability_shadow,
                )
                try:
                    bias_by_bucket = await fetch_bias_by_bucket(symbol)
                    # Explicit min_rr/max_sharpe/min_trades (not the
                    # function's own defaults) — guarantees this shadow
                    # comparison's "baseline" recomputation uses the
                    # EXACT SAME threshold values as pick_best_combo just
                    # used above, not a second, independently-resolved
                    # copy that could quietly drift from it.
                    reach = reachability_aware_pick(
                        grid_results, atr, bias_by_bucket,
                        min_rr=MIN_RR, max_sharpe=MAX_SHARPE, min_trades=MIN_TRADES,
                    )
                    asyncio.create_task(record_heavy_sweep_reachability_shadow(
                        symbol,
                        nothing_survived=reach["nothing_survived"],
                        pick_changed=reach["pick_changed"],
                        baseline_bucket=reach["baseline_bucket"],
                        baseline_sharpe=reach["baseline_sharpe"],
                        filtered_bucket=reach["filtered_bucket"],
                        filtered_sharpe=reach["filtered_sharpe"],
                    ))
                    logger.info(
                        "[sweep:heavy] %s: reachability %s — baseline bucket=%s "
                        "sharpe=%.4f | filtered bucket=%s sharpe=%s "
                        "(nothing_survived=%s, pick_changed=%s)",
                        symbol, reachability_mode,
                        reach["baseline_bucket"], reach["baseline_sharpe"] or 0.0,
                        reach["filtered_bucket"],
                        f"{reach['filtered_sharpe']:.4f}" if reach["filtered_sharpe"] is not None else "-",
                        reach["nothing_survived"], reach["pick_changed"],
                    )
                    if reachability_mode == "live":
                        if reach["filtered_combo"] is not None:
                            best = reach["filtered_combo"]
                            result.best_combo = best
                            logger.info(
                                "[sweep:heavy] %s: LIVE reachability pick applied — "
                                "brick=%.4f sl=%d tp=%d sharpe=%.4f",
                                symbol, best.brick_size, best.sl_bricks,
                                best.tp_bricks, best.sharpe,
                            )
                        else:
                            # Provisional fallback (worklog's "live-mode
                            # fallback" — left open pending real shadow
                            # data): keep today's Sharpe-only pick rather
                            # than skip the sweep outright. best/
                            # result.best_combo are already the baseline
                            # from pick_best_combo above — no-op here.
                            logger.warning(
                                "[sweep:heavy] %s: LIVE reachability found no "
                                "candidate — falling back to baseline Sharpe-only pick",
                                symbol,
                            )
                except Exception as exc:
                    logger.warning(
                        "[sweep:heavy] %s: reachability check failed (%s: %s), "
                        "using baseline pick",
                        symbol, type(exc).__name__, exc,
                    )

            venue_name = _derive_venue(sym)

            # ── 7. Persist optimised brick params to YAML ─────────────
            await self._persist_brick_params_to_yaml(
                symbol, best.brick_size, best.sl_bricks, best.tp_bricks,
            )
            result.persisted_yaml = True

            # ── 8. EV compute with HMM markov (replaces discrete chain) ─
            direction = "short" if "bear" in result.regime_label.lower() else "long"

            # Mirror of the light sweep's MARKOV_SOURCE_MODE branch
            # (orchestrator.py::_process_symbol) — heavy sweep previously
            # always used the legacy HmmModel unconditionally regardless
            # of the mode, exactly the "gate added to one path, missed on
            # the other" bug the MSE doc warns about (2026-08-26 HMM
            # consolidation). Read from the git-tracked
            # config/feature_flags.yaml (2026-08-26 config-centralization
            # correction) — no env var / Studio Secret involved, no
            # circular-import concern importing this directly.
            from regime_platform.core.feature_flags import get_markov_source_mode

            markov_source_mode = get_markov_source_mode()
            markov_T: dict[str, dict[str, float]] | None = None
            markov_current: str | None = None
            # Tier 1/2 outcome-linked evidence (2026-08-31, worklog
            # 20260831_SHADOW_TO_LIVE_ROADMAP.md) — see orchestrator.py's
            # identical mirror for the full rationale. Only populated
            # inside the actual shadow-mode comparison below.
            sim_legacy_markov_state: str | None = None
            sim_trend_hmm_markov_state: str | None = None
            if markov_source_mode != "trend_hmm_live":
                markov_T, markov_current = self._hmm_model.emit_markov(symbol)

            if markov_source_mode in ("trend_hmm_shadow", "trend_hmm_live"):
                try:
                    trend_hmm_T, trend_hmm_current = regime_model.trend_transition_matrix_3state(
                        regime_snapshot.trend_state
                    )
                    if markov_source_mode == "trend_hmm_shadow":
                        logger.info(
                            "[sweep:heavy] %s: markov shadow — legacy current_state=%s "
                            "row=%s | trend_hmm current_state=%s row=%s",
                            symbol, markov_current, markov_T,
                            trend_hmm_current, trend_hmm_T,
                        )
                        from regime_platform.sweep_orchestrator.supabase_client import (
                            record_markov_shadow_compare,
                        )
                        legacy_p_up = (markov_T or {}).get(markov_current or "", {}).get("UP", 0.5)
                        trend_p_up = trend_hmm_T.get(trend_hmm_current, {}).get("UP", 0.5)
                        sim_legacy_markov_state = markov_current
                        sim_trend_hmm_markov_state = trend_hmm_current
                        asyncio.create_task(record_markov_shadow_compare(
                            symbol,
                            state_agree=(markov_current == trend_hmm_current),
                            p_up_diff=legacy_p_up - trend_p_up,
                            legacy_state=markov_current,
                            trend_hmm_state=trend_hmm_current,
                        ))
                    elif markov_source_mode == "trend_hmm_live":
                        markov_T, markov_current = trend_hmm_T, trend_hmm_current
                except Exception as exc:
                    logger.warning(
                        "[sweep:heavy] %s: trend_hmm markov derivation failed (%s: %s), using %s",
                        symbol, type(exc).__name__, exc,
                        "legacy_hmm value" if markov_source_mode == "trend_hmm_shadow"
                        else "neutral uniform-matrix fallback",
                    )
                    if markov_source_mode == "trend_hmm_live" and markov_T is None:
                        markov_T = {
                            s: {t: 1.0 / 3.0 for t in ("UP", "DOWN", "FLAT")}
                            for s in ("UP", "DOWN", "FLAT")
                        }
                        markov_current = "FLAT"

            # ── 8a. TA indicator snapshot (migration 036) ──────────────
            # Pull the latest 5 TA values from the HMM feature buffer for
            # persistence to nt_sweep_result. Best-effort — None when the
            # buffer is empty (cold start). Heavy sweep doesn't write to
            # nt_regime_log (only the light sweep does), so we only need
            # the values for the log_sweep_result call below.
            from regime_platform.services.hmm import latest_ta
            ta_snapshot = latest_ta(self._hmm_buffer, symbol)

            # p_historical (R3, 2026-08-23) was retired as a live blend
            # input 2026-09-09 — see the class docstring above and
            # orchestrator.py's mirror comment for the full rationale
            # (duplicate calibration-correction mechanism, consolidated to
            # apply_calibration_adjustment).
            # R4 (2026-08-23): same non-convergence discount as the light
            # sweep, reusing regime_snapshot.trend_converged (populated by
            # RegimeHMM.predict() automatically via _regime_sync() above --
            # nothing extra to fit here).
            _regime_weight_override: dict[str, float] = {}
            if not getattr(regime_snapshot, "trend_converged", True):
                _scale = max(0.0, min(1.0, float(
                    getattr(settings, "REGIME_NONCONVERGENCE_WEIGHT_SCALE", 0.5)
                )))
                _regime_weight_override = {"p_regime": P_WIN_WEIGHTS_FULL["p_regime"] * _scale}
                logger.warning(
                    "[sweep:heavy] %s: trend HMM did not converge cleanly — "
                    "discounting p_regime weight to %.4f",
                    symbol, _regime_weight_override["p_regime"],
                )
            _trend_probs = dict(zip(
                # RegimeHMM.TREND_LABELS — accessed via the snapshot's
                # internal arrays; the orchestrator's _process_symbol
                # does the same zip.
                self._regime_trend_labels(),
                regime_snapshot.trend_probs,
            ))

            # Tier 2 HMM fix (2026-08-27, worklog
            # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md) — mirror of
            # the light sweep's gate (orchestrator.py::_process_symbol).
            # Same magnitude/sign gate as Tier 1, applied to trend_probs
            # itself. shadow mode persists the comparison but doesn't
            # change what compute_ev sees below.
            from regime_platform.core.feature_flags import get_trend_probs_gate_mode

            trend_probs_gate_mode = get_trend_probs_gate_mode()
            # Tier 1/2 outcome-linked evidence — see orchestrator.py's
            # identical mirror for the full rationale.
            sim_raw_p_regime: float | None = None
            sim_gated_p_regime: float | None = None
            if trend_probs_gate_mode in ("shadow", "live"):
                try:
                    _gated_trend_probs = regime_model.gated_trend_probs(_trend_probs)
                    if trend_probs_gate_mode == "shadow":
                        raw_p_regime = self._ev_engine.compute_p_regime(_trend_probs, direction)
                        gated_p_regime = self._ev_engine.compute_p_regime(_gated_trend_probs, direction)
                        logger.info(
                            "[sweep:heavy] %s: trend_probs gate shadow — raw "
                            "p_regime=%.4f, gated p_regime=%.4f (diff=%+.4f)",
                            symbol, raw_p_regime, gated_p_regime,
                            gated_p_regime - raw_p_regime,
                        )
                        from regime_platform.sweep_orchestrator.supabase_client import (
                            record_trend_probs_gate_shadow,
                        )
                        sim_raw_p_regime = raw_p_regime
                        sim_gated_p_regime = gated_p_regime
                        asyncio.create_task(record_trend_probs_gate_shadow(
                            symbol, raw_p_regime=raw_p_regime, gated_p_regime=gated_p_regime,
                        ))
                    elif trend_probs_gate_mode == "live":
                        _trend_probs = _gated_trend_probs
                except Exception as exc:
                    logger.warning(
                        "[sweep:heavy] %s: trend_probs gate failed (%s: %s), using ungated trend_probs",
                        symbol, type(exc).__name__, exc,
                    )

            with self._ev_engine.temp_weights(_regime_weight_override):
                ev_result = self._ev_engine.compute_ev(
                    trend_probs=_trend_probs,
                    direction=direction,
                    transition_matrix=markov_T,
                    current_state=markov_current,
                    p_timesfm=p_timesfm,
                    brick_size=best.brick_size,
                    sl_bricks=best.sl_bricks,
                    tp_bricks=best.tp_bricks,
                    n_steps=MARKOV_HOLD_STEPS,
                )

            # ── 9. Build + push qualified signal ──────────────────────
            trade_signal = _compute_trade_signal(
                result.regime_label, current_price,
                best.brick_size, best.sl_bricks, best.tp_bricks,
            )
            qualified = self._evaluate_heavy_qualification(
                ev_result, trade_signal, result.regime_conf, result.kelly_f,
                p_timesfm,
            )
            result.qualified = qualified

            # ── 8b. Macro filter (2026-08-28, worklog
            # 20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md) — mirror of
            # the light sweep's macro read (orchestrator.py::
            # _process_symbol). Same reasoning as the trend_probs gate
            # mirroring fix earlier this session: a gate added to one
            # sweep path and missed on the other is exactly the bug this
            # session already found twice. macro_label/macro_strength are
            # captured on nt_signal_sim regardless of mode; the Kelly
            # multiplier is only APPLIED (mutating result.kelly_f, which
            # feeds every downstream use — the qualified-signal push, the
            # nt_sweep_result payload, and the sim-capture payload below)
            # when macro_regime_mode=live.
            macro_label: str | None = None
            macro_strength: float | None = None
            if trade_signal["signal"] not in ("neutral", "unknown"):
                from regime_platform.core.feature_flags import get_macro_regime_mode
                macro_mode = get_macro_regime_mode()
                if macro_mode != "off":
                    from regime_platform.sweep_orchestrator.macro_filter import (
                        get_macro_regime,
                        kelly_macro_multiplier,
                    )
                    try:
                        macro_row = await get_macro_regime(symbol)
                        if macro_row:
                            macro_label = macro_row.get("macro_label")
                            macro_strength = macro_row.get("macro_strength")
                            if macro_mode == "live" and macro_label is not None:
                                macro_mult = kelly_macro_multiplier(
                                    macro_label, macro_strength, trade_signal["signal"],
                                )
                                result.kelly_f = result.kelly_f * macro_mult
                                logger.info(
                                    "[sweep:heavy] %s: macro filter LIVE — label=%s "
                                    "strength=%.4f mult=%.4f, kelly_f→%.6f",
                                    symbol, macro_label, macro_strength or 0.0,
                                    macro_mult, result.kelly_f,
                                )
                    except Exception as exc:
                        logger.warning(
                            "[sweep:heavy] %s: macro filter read failed (%s: %s), skipping",
                            symbol, type(exc).__name__, exc,
                        )

            # ── 8c. Trend filter (2026-09-10, worklog
            # 20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md
            # SS7) — mirror of the light sweep's trend read
            # (orchestrator.py::_process_symbol), same mirroring
            # discipline as the macro-filter block above. Medium-term
            # (10-50 day SMA crossover + RSI) read, separate concern from
            # macro_label/macro_strength above.
            trend_label: str | None = None
            trend_strength: float | None = None
            if trade_signal["signal"] not in ("neutral", "unknown"):
                from regime_platform.core.feature_flags import get_trend_regime_mode
                trend_mode = get_trend_regime_mode()
                if trend_mode != "off":
                    from regime_platform.sweep_orchestrator.trend_filter import (
                        get_trend_regime,
                        kelly_trend_multiplier,
                    )
                    try:
                        trend_row = await get_trend_regime(symbol)
                        if trend_row:
                            trend_label = trend_row.get("trend_label")
                            trend_strength = trend_row.get("trend_strength")
                            if trend_mode == "live" and trend_label is not None:
                                trend_mult = kelly_trend_multiplier(
                                    trend_label, trend_strength, trade_signal["signal"],
                                )
                                result.kelly_f = result.kelly_f * trend_mult
                                logger.info(
                                    "[sweep:heavy] %s: trend filter LIVE — label=%s "
                                    "strength=%.4f mult=%.4f, kelly_f→%.6f",
                                    symbol, trend_label, trend_strength or 0.0,
                                    trend_mult, result.kelly_f,
                                )
                    except Exception as exc:
                        logger.warning(
                            "[sweep:heavy] %s: trend filter read failed (%s: %s), skipping",
                            symbol, type(exc).__name__, exc,
                        )

            # ── Calibration bias (display only) ───────────────────────
            # Only fetch when qualified — an extra Supabase round-trip
            # that's only useful for the push payload's calibration_bias/
            # calibration_status fields on signals we'll actually publish
            # (mirrors orchestrator.py's identical light-sweep comment).
            cal = None
            if qualified:
                from regime_platform.sweep_orchestrator.supabase_client import (
                    fetch_calibration_bias,
                )
                cal = await fetch_calibration_bias(symbol, venue=venue_name)

            if qualified:
                await self._push_heavy_signal(
                    sym=sym,
                    symbol=symbol,
                    regime_label=result.regime_label,
                    regime_conf=result.regime_conf,
                    trade_signal=trade_signal,
                    best=best,
                    kelly_f=result.kelly_f,
                    ev_result=ev_result,
                    p_timesfm=p_timesfm,
                    cal=cal,
                )

            # ── 10. Log sweep result to Supabase ──────────────────────
            from regime_platform.sweep_orchestrator.supabase_client import (
                log_sweep_result,
            )
            await log_sweep_result({
                "symbol": symbol,
                "asset_class": sym.get("asset_class", ""),
                "source": "sweep-heavy",
                "brick_size": best.brick_size,
                "sl_bricks": best.sl_bricks,
                "tp_bricks": best.tp_bricks,
                "sharpe": round(best.sharpe, 4),
                "win_rate": round(best.win_rate, 4),
                "n_trades": best.total_trades,
                "profit_factor": round(best.profit_factor, 4),
                "regime": result.regime_label,
                "regime_conf": round(result.regime_conf, 4),
                "kelly_f": round(result.kelly_f, 6),
                "markov_p_up": 0.5,  # legacy column; real value is in EV blend
                "markov_p_dn": 0.5,
                "sweep_window": "90d",
                "sweep_duration_ms": int((time.time() - start_ts) * 1000),
                "n_combos_tested": len(grid_results),
                "effective_kelly": round(result.kelly_f * ev_result.ev_scale, 6),
                "signal": trade_signal["signal"],
                "ev": round(ev_result.ev, 4),
                "p_win": round(ev_result.p_win, 4),
                "qualified": qualified,
                "p_timesfm": round(p_timesfm, 4) if p_timesfm is not None else None,
                # ── FULL PRECISION PRICES (2026-08-05) ──────────────────
                # Round to 6dp, NOT `decimals` — `decimals` defaulted to
                # 2dp for XAUUSD/XAUEUR/XAUAUD/XAGUSD and truncated the
                # entry/stop/tp stored in nt_sweep_result. The full value
                # must be stored so Discord can deliver the same value.
                "entry_price": round(trade_signal["entry_price"], 6),
                "stop_loss": round(trade_signal["stop_loss"], 6),
                "take_profit": round(trade_signal["take_profit"], 6),
                "venue": venue_name,
                # ── TA indicators (migration 036) ──────────────────────
                # From HMM feature buffer's latest bar for this symbol
                # (captured in §8a above). All nullable.
                "ta_adx": ta_snapshot["ta_adx"],
                "ta_ao": ta_snapshot["ta_ao"],
                "ta_bb_power": ta_snapshot["ta_bb_power"],
                "ta_macd": ta_snapshot["ta_macd"],
                "ta_rsi": ta_snapshot["ta_rsi"],
            })

            # ── 10a. Signal-sim capture (nt_signal_sim) ───────────────
            # Mirror the light-sweep capture so heavy-sweep signals are
            # also scored by the EOD resolver. Fire-and-forget; must never
            # break the sweep. NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY
            # (default False) trims to qualified-only — opt-in.
            if trade_signal["signal"] not in ("neutral", "unknown"):
                try:
                    from regime_platform.sweep_orchestrator.supabase_client import (
                        log_signal_sim,
                    )
                    from regime_platform.sweep_orchestrator.orchestrator import (
                        _build_signal_sim_payload,
                    )
                    sim_payload = _build_signal_sim_payload(
                        symbol=symbol,
                        venue=venue_name,
                        ts=datetime.now(timezone.utc).isoformat(),
                        signal=trade_signal["signal"],
                        entry_price=trade_signal["entry_price"],
                        stop_loss=trade_signal["stop_loss"],
                        take_profit=trade_signal["take_profit"],
                        brick_size=best.brick_size,
                        sl_bricks=best.sl_bricks,
                        tp_bricks=best.tp_bricks,
                        p_win=ev_result.p_win,
                        ev=ev_result.ev,
                        regime=result.regime_label,
                        regime_conf=result.regime_conf,
                        source="sweep-heavy",
                        qualified=qualified,
                        kelly_f=result.kelly_f,
                        effective_kelly=result.kelly_f * ev_result.ev_scale,
                        atr=atr,
                        macro_label=macro_label,
                        macro_strength=macro_strength,
                        trend_label=trend_label,
                        trend_strength=trend_strength,
                        legacy_markov_state=sim_legacy_markov_state,
                        trend_hmm_markov_state=sim_trend_hmm_markov_state,
                        raw_p_regime=sim_raw_p_regime,
                        gated_p_regime=sim_gated_p_regime,
                    )
                    if settings.NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY and not qualified:
                        logger.debug(
                            "[sweep-heavy] %s: signal-sim capture skipped (qualified-only mode)",
                            symbol,
                        )
                    else:
                        asyncio.create_task(log_signal_sim(sim_payload))
                    # Part II paper portfolio: open a kelly-sized paper
                    # position for QUALIFIED heavy signals (fire-and-forget).
                    if qualified:
                        from regime_platform.sweep_orchestrator.paper_book import open_position
                        asyncio.create_task(open_position(sim_payload))
                        # ── 10a1. Realtime broadcast (Talaria dashboard) ──
                        # Fire-and-forget publish of the QUALIFIED heavy
                        # signal to the realtime:signals broadcast channel,
                        # built from the sim payload (6dp full-value prices).
                        # Never raises: broadcast_signal swallows failures
                        # and skips silently without a running event loop.
                        from regime_platform.sweep_orchestrator.realtime_publisher import (
                            broadcast_signal,
                            build_signal_payload,
                        )
                        broadcast_signal(build_signal_payload(
                            symbol=symbol,
                            direction=trade_signal["signal"],
                            entry_price=sim_payload["entry_price"],
                            stop_loss=sim_payload["stop_loss"],
                            take_profit=sim_payload["take_profit"],
                            kelly_f=sim_payload.get("kelly_f"),
                            effective_kelly=sim_payload.get("effective_kelly"),
                        ), plan_ids=sym.get("plan_ids") or [])
                except RuntimeError:
                    # No running event loop — skip capture.
                    pass
                except Exception as exc:
                    logger.warning(
                        "[sweep:heavy] %s: signal-sim capture failed: %s",
                        symbol, exc,
                    )

            result.duration_ms = int((time.time() - start_ts) * 1000)
            logger.info(
                "[sweep:heavy] %s: DONE in %dms (qualified=%s, combos=%d)",
                symbol, result.duration_ms, qualified, len(grid_results),
            )
            return result

        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.duration_ms = int((time.time() - start_ts) * 1000)
            logger.exception("[sweep:heavy] %s: FAILED", symbol)
            # Trace row so the failure is visible in nt_sweep_result
            # (previously silent — 2026-08-09 showed only successes).
            try:
                await self._log_heavy_failure_trace(
                    symbol, sym.get("asset_class", ""), result,
                )
            except Exception:
                pass  # trace is best-effort; never mask the original error
            return result

    # ── CPU-bound helpers (run in asyncio.to_thread) ─────────────────

    def _renko_grid_search_sync(
        self,
        prices: list[float],
        brick_sizes: list[float],
        symbol: str,
    ) -> list[ComboResult]:
        """CPU-bound renko grid search. Runs in asyncio.to_thread.

        For each (brick_size, sl, tp) combo, runs a full RenkoPipeline
        backtest and records the resulting Sharpe / trades / win_rate.

        B1a design: direct call to RenkoPipeline.backtest_with_analytics,
        no HTTP hop. The pipeline is constructed once per combo (cheap —
        just object init) and reset between runs.

        Total combos: 5 bricks × 3 SL × 6 TP = 90 per symbol.
        """
        from renko.config import RenkoConfig
        from renko.pipeline import RenkoPipeline

        results: list[ComboResult] = []
        for brick in brick_sizes:
            for sl in SWEEP_SL_RANGE:
                for tp in SWEEP_TP_RANGE:
                    # Pre-filter by MIN_RR — skip combos that can't pass
                    if tp / sl < MIN_RR:
                        continue
                    try:
                        config = RenkoConfig(
                            brick_size=brick,
                            sl_bricks=sl,
                            tp_bricks=tp,
                            dry_run=True,
                            live_enabled=False,
                        )
                        pipeline = RenkoPipeline(config)
                        bt = pipeline.backtest_with_analytics(
                            prices=prices, n_trials=1,
                        )
                        stats = bt.get("stats", {}).get(
                            "journal", bt.get("stats", {})
                        )
                        results.append(ComboResult(
                            brick_size=brick,
                            sl_bricks=sl,
                            tp_bricks=tp,
                            sharpe=float(stats.get("sharpe_estimate", 0.0)),
                            total_trades=int(stats.get("total_trades", 0)),
                            win_rate=float(stats.get("win_rate", 0.0)),
                            profit_factor=float(stats.get("profit_factor", 0.0)),
                            total_pnl_bricks=float(stats.get("total_pnl_bricks", 0.0)),
                            max_drawdown_bricks=float(stats.get("max_drawdown_bricks", 0.0)),
                            deadline_aware_sharpe=deadline_aware_sharpe(bt.get("trades", [])),
                        ))
                    except Exception as exc:
                        logger.warning(
                            "[sweep:heavy] %s: combo (brick=%.4f, sl=%d, tp=%d) failed: %s",
                            symbol, brick, sl, tp, exc,
                        )
                        continue
        return results

    def _regime_sync(self, prices: list[float]):
        """CPU-bound HMM regime detection. Runs in asyncio.to_thread.

        Returns ``(model, regime)`` — the fitted model is returned
        alongside the snapshot (not just the snapshot) so the caller can
        also call ``model.trend_transition_matrix_3state()`` for the
        MARKOV_SOURCE_MODE=trend_hmm_shadow/live branch (2026-08-26 HMM
        consolidation — mirrors the light sweep's `_process_symbol`,
        which already had this branch; heavy sweep did not, the exact
        "gate added to one path, missed on the other" bug the MSE
        integration doc warns about).
        """
        from regime_platform.core.regime_engine import RegimeHMM
        np_prices = np.array(prices, dtype=float)
        model = RegimeHMM.from_runtime_config_sync() if hasattr(
            RegimeHMM, "from_runtime_config_sync"
        ) else None
        if model is None:
            # L3: plain construct only. The async from_runtime_config()
            # variant cannot be awaited here — _regime_sync runs inside
            # asyncio.to_thread (no running loop in this thread) and the
            # old `if False else RegimeHMM()` dead branch hid that fact.
            model = RegimeHMM()
        model.fit(np_prices)
        return model, model.predict(np_prices)

    def _kelly_sync(self, prices: list[float], asset_class: str):
        """CPU-bound Kelly sizing. Runs in asyncio.to_thread."""
        from regime_platform.core.position_sizer import PositionRequest
        np_prices = np.array(prices, dtype=float)
        returns = np.diff(np.log(np_prices)).tolist()
        # Heavy sweep has no regime snapshot at this point — pass a
        # neutral placeholder. The real regime is computed in parallel
        # and the EV engine uses it. Kelly sizing here just needs the
        # return series to compute volatility-scaled fractional Kelly.
        annualise = 8760 if asset_class == "crypto" else 252
        # Build a minimal neutral regime snapshot
        from regime_platform.core.regime_engine import RegimeSnapshot
        neutral = RegimeSnapshot(
            vol_probs=[0.5, 0.5],
            trend_probs=[0.34, 0.33, 0.33],
            vol_state="med",
            trend_state="neutral",
            regime_label="unknown",
            confidence=0.5,
            n_bars_fitted=0,
        )
        return self._sizer.size(
            PositionRequest(
                returns=returns,
                kelly_fraction=0.5,
                target_vol=0.15,
                regime=neutral,
            ),
            annualise_factor=annualise,
        )

    def _regime_trend_labels(self) -> list[str]:
        """Return RegimeHMM.TREND_LABELS without circular-import issues."""
        from regime_platform.core.regime_engine import RegimeHMM
        return list(RegimeHMM.TREND_LABELS)

    # ── YAML persistence ─────────────────────────────────────────────

    async def _log_heavy_failure_trace(
        self,
        symbol: str,
        asset_class: str,
        result: "HeavySweepResult",
    ) -> None:
        """Best-effort trace row for a failed / no-combo heavy sweep.

        The heavy sweep previously wrote NO row when a symbol failed or had
        no combo pass the filters — the sweep summary counted them as
        ``failed`` in logs but the DB showed only the successful symbols
        (2026-08-09: 6 rows for 14 symbols). This leaves a nullable-``error``
        row so failed sweeps are visible in nt_sweep_result. Never raises.
        """
        try:
            from regime_platform.sweep_orchestrator.supabase_client import (
                log_sweep_result,
            )
            await log_sweep_result({
                "symbol": symbol,
                "asset_class": asset_class,
                "source": "sweep-heavy",
                "qualified": False,
                "regime": result.regime_label,
                "regime_conf": round(result.regime_conf, 4) if result.regime_conf else None,
                "kelly_f": round(result.kelly_f, 6) if result.kelly_f else None,
                "n_combos_tested": result.n_combos_tested,
                "sweep_window": "90d",
                "sweep_duration_ms": result.duration_ms,
                "error": (result.error or "")[:500],
                # nt_sweep_result has NOT NULL columns on these (verified
                # 2026-08-09: 23502 on brick_size/sl_bricks/tp_bricks/sharpe/
                # win_rate/n_trades) — send 0 sentinels so the trace row
                # inserts even though the sweep never produced a combo.
                "brick_size": 0.0,
                "sl_bricks": 0,
                "tp_bricks": 0,
                "sharpe": 0.0,
                "win_rate": 0.0,
                "n_trades": 0,
            })
        except Exception as exc:
            logger.debug(
                "[sweep:heavy] %s: failure trace row skipped (%s)",
                symbol, exc,
            )

    async def _persist_brick_params_to_yaml(
        self,
        symbol: str,
        brick: float,
        sl: int,
        tp: int,
    ) -> None:
        """Write optimised brick params to config/brick_params.yaml.

        This is the SOLE writer of optimised brick params. The light
        sweep reads from the same YAML via ``_load_brick_params``. The
        YAML cache is invalidated on write so the next light-sweep read
        picks up the new values.
        """
        from regime_platform.config.brick_params import write_brick_params
        # Run the synchronous YAML write in a thread to avoid blocking
        await asyncio.to_thread(
            write_brick_params,
            {symbol: {
                "brick_size": brick,
                "sl_bricks": sl,
                "tp_bricks": tp,
            }},
        )
        logger.info(
            "[sweep:heavy] %s: wrote brick params to YAML (brick=%.4f, sl=%d, tp=%d)",
            symbol, brick, sl, tp,
        )

    # ── Qualification + signal push ──────────────────────────────────

    def _evaluate_heavy_qualification(
        self,
        ev_result,
        trade_signal: dict[str, Any],
        regime_conf: float,
        kelly_f: float,
        p_timesfm: Optional[float],
    ) -> bool:
        """Evaluate whether a heavy-sweep signal is qualified for push.

        Mirrors the light-sweep qualification logic but simpler — the
        heavy sweep's job is to compute brick params, not to push many
        signals. We push only if:
          • signal is buy/sell (not neutral)
          • ev_result.allowed is True
          • p_win >= MIN_P_WIN
          • regime_conf >= MIN_REGIME_CONF
          • kelly_f >= MIN_KELLY
        """
        from regime_platform.sweep_orchestrator.orchestrator import (
            MIN_KELLY,
            MIN_REGIME_CONF,
            MIN_P_WIN,
        )
        if trade_signal["signal"] in ("neutral", "unknown"):
            return False
        if not ev_result.allowed:
            return False
        if ev_result.p_win < MIN_P_WIN:
            return False
        if regime_conf < MIN_REGIME_CONF:
            return False
        if kelly_f < MIN_KELLY:
            return False
        return True

    async def _push_heavy_signal(
        self,
        sym: dict[str, Any],
        symbol: str,
        regime_label: str,
        regime_conf: float,
        trade_signal: dict[str, Any],
        best: ComboResult,
        kelly_f: float,
        ev_result,
        p_timesfm: Optional[float],
        cal: Optional[dict[str, Any]],
    ) -> None:
        """Build the qualified heavy-sweep signal payload and deliver it
        to Discord directly. Same payload shape as the light sweep."""
        import json
        from regime_platform.sweep_orchestrator.orchestrator import _derive_venue

        now_ms = int(time.time() * 1000)
        venue_name = _derive_venue(sym)
        effective_kelly = kelly_f * ev_result.ev_scale

        # ── One arbiter for "is this symbol calibrated" (2026-09-09,
        # worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
        # SS16): classify via eod_correction._classify_bias — the exact,
        # registry-tunable function that actually drives live enforcement
        # — instead of fetch_calibration_bias's own internal, hardcoded
        # 0.05-threshold status, which could and did disagree with the
        # real 0.10-threshold verdict for any |bias| in [0.05, 0.10).
        # Same fix, same reasoning as routers/calibration.py's 2026-08-22
        # audit fix for the identical duplication.
        calibration_status = None
        if cal is not None:
            from regime_platform.sweep_orchestrator.eod_correction import (
                _classify_bias,
            )
            calibration_status = _classify_bias(cal["bias"])

        # ── EOD calibration enforcement (2026-08-10) ────────────────────
        # Heavy-sweep signals get the same live calibration correction as
        # light sweeps: read the latest nt_eod_actions recommendation and
        # shrink p_win toward 0.5 + halve Kelly when over/underconfident.
        calib_adj = None
        if settings.EOD_CALIBRATION_ENFORCE:
            from regime_platform.sweep_orchestrator.calibration_enforcement import (
                apply_calibration_adjustment,
            )
            try:
                calib_adj = await apply_calibration_adjustment(
                    symbol,
                    p_win=ev_result.p_win,
                    kelly_f=effective_kelly,
                )
                if calib_adj and calib_adj.get("applied"):
                    ev_result.p_win = calib_adj["p_win"]
                    effective_kelly = calib_adj["kelly_f"]
            except Exception as exc:
                logger.warning(
                    "[sweep:heavy] %s: calibration enforcement failed (%s) — proceeding raw",
                    symbol, type(exc).__name__,
                )

        agent_payload = json.dumps({
            "type": "heartbeat",
            "symbol": symbol,
            "ts": now_ms,
            "asset_class": sym.get("asset_class", ""),
            "venue": venue_name,
            "source": "sweep-heavy",
            "regime": regime_label,
            "regime_conf": round(regime_conf, 4),
            "signal": trade_signal["signal"],
            # ── FULL PRECISION PRICES (2026-08-05) ──────────────────────
            # Same rule as the light sweep heartbeat: 6dp, NOT `decimals`,
            # so Discord receives the same full entry/stop/tp values that
            # are stored in nt_sweep_result.
            "entry_price": round(trade_signal["entry_price"], 6),
            "stop_loss": round(trade_signal["stop_loss"], 6),
            "take_profit": round(trade_signal["take_profit"], 6),
            "aggression": ev_result.aggression,
            "brick_size": best.brick_size,
            "sl_bricks": best.sl_bricks,
            "tp_bricks": best.tp_bricks,
            "kelly_f": round(kelly_f, 6),
            "effective_kelly": round(effective_kelly, 6),
            "ev": round(ev_result.ev, 4),
            "ev_per_dollar": round(ev_result.ev_per_dollar, 4),
            "rr": round(ev_result.rr, 4),
            "ev_scale": round(ev_result.ev_scale, 4),
            "p_win": round(ev_result.p_win, 4),
            "p_regime": round(ev_result.p_regime, 4),
            "p_markov": round(ev_result.p_markov, 4),
            "p_timesfm": round(ev_result.p_timesfm, 4) if p_timesfm is not None else None,
            "p_historical": round(ev_result.p_historical, 4) if ev_result.p_historical else None,
            "sources_used": list(ev_result.sources_used),
            "weights_used": ev_result.weights_used or {},
            "timesfm_available": p_timesfm is not None,
            "calibration_bias": round(cal["bias"], 4) if cal else None,
            "calibration_status": calibration_status,
            "calibration_enforced": bool(calib_adj and calib_adj.get("applied")),
            "sharpe": round(best.sharpe, 4),
            "n_combos_tested": True,  # marker — agent can show "optimised" badge
            "qualified": True,
        })

        logger.info(
            "[sweep:heavy] %s: QUALIFIED %s signal (sharpe=%.4f, brick=%.4f, sl=%d, tp=%d)",
            symbol, trade_signal["signal"], best.sharpe,
            best.brick_size, best.sl_bricks, best.tp_bricks,
        )

        # ── Direct Discord delivery ──────────────────────────────────
        try:
            from regime_platform.sweep_orchestrator.discord_delivery import (
                maybe_send_discord_signal,
            )

            payload_obj = (
                json.loads(agent_payload)
                if isinstance(agent_payload, str)
                else agent_payload
            )
            asyncio.create_task(
                maybe_send_discord_signal(
                    payload_obj,
                    source="sweep-heavy",
                )
            )
        except Exception as discord_exc:
            logger.warning(
                "[sweep:heavy] %s: direct Discord dispatch failed — %s: %s",
                symbol, type(discord_exc).__name__, discord_exc,
            )
