"""
Heavy Sweep Module (Phase B)
═════════════════════════════

Ports the heavy sweep from the deprecated Supabase Edge Function
(``supabase/functions/sweep/index.ts:runHeavySweepForSymbol``) to the
backend Python ``SweepOrchestrator``.

Heavy sweep runs on a per-symbol cron schedule defined by
``nt_symbol.sweep_cron`` (default ``0 20 * * sun`` — Sunday 20:00 UTC).
NOTE: APScheduler's ``from_crontab`` uses PYTHON weekday convention
(0=Monday), so the cron-conventional ``0 20 * * 0`` fires MONDAY — use
the ``sun`` name (or ``6``) for Sunday.
TimesFM confidence gate skips ~70 % of scheduled runs, so the effective
frequency is ~0.6 runs/week/symbol.

Per-symbol heavy sweep does:
  1. Skip check (sweep_skip_until from YAML metadata)
  2. Fetch 2160 hourly candles (90 d) from TDVA
  3. TimesFM confidence gate (cache-first, skip if confident)
  4. ATR(14) + 5 brick sizes (multipliers 0.75, 1.0, 1.5, 2.0, 3.0)
  5. Grid search: 5 bricks × 3 SL × 6 TP = 90 combos (RenkoPipeline)
  6. HMM regime + Kelly sizing (parallel with grid)
  7. Pick best combo by Sharpe (MIN_RR=2.0, MAX_SHARPE=10.0, MIN_TRADES=10)
  8. Calibration bias → set sweep_skip_until when CALIBRATED
  9. Write optimised brick params to config/brick_params.yaml
 10. Compute EV with Markov T^N (Phase A)
 11. Build + push qualified signal to signal.raw.noble_trader

T4 w/GPU runs 14 symbols in <30 s total — no stagger needed, no shadow
mode, no transition period. All Supabase Edge Functions are already OFF.

NOTE (2026-08-09): concurrency is CAPPED at ``HEAVY_SWEEP_MAX_CONCURRENCY``
symbols at a time. The naive "fire all 14 via asyncio.gather" design
exhausted the shared Redis pool (max_connections=10) on the first real
weekend run — every symbol opens a ``timesfm:cached`` read simultaneously,
and the last 4 (the metals) died with ``MaxConnectionsError``. The cap
also keeps the CPU-bound grid-search thread pool sane (each symbol spawns
3 to_thread tasks; 14 × 3 = 42 threads otherwise).

This module is a mix-in: ``SweepOrchestrator`` inherits from
``HeavySweepMixin`` so the heavy-sweep methods have access to ``self._redis``,
``self._ev_engine``, ``self._sizer``, ``self._scheduler``, etc.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

from regime_platform.core.settings import settings

# Note: write_brick_params + fetch_calibration_bias + log_sweep_result +
# fetch_candles_for_symbols are imported lazily inside their callers to
# avoid circular-import issues when hmmlearn (a heavy dep pulled in by
# regime_engine via orchestrator) is not installed in the dev env.
# All three are stable public APIs of their respective modules.

logger = logging.getLogger("regime.sweep.heavy")


# ─── Grid search constants (ported from edge fn _shared/utils.ts) ────────
import os as _os

SWEEP_SL_RANGE = [3, 5, 7]
SWEEP_TP_RANGE = [4, 5, 6, 8, 10, 12]

# MIN_RR: shared with the orchestrator's qualification gate via the same
# env var (default 2.0). When the user sets MIN_RR=1.5 for signal
# qualification, the grid search MUST use the same threshold — otherwise
# the heavy sweep pre-filter (line 737) skips combos the orchestrator
# would accept, causing emergency sweeps to fail with
# no_combo_passed_filters (verified 2026-08-13: EURUSD/GBPUSD/USDCAD
# with MIN_RR=2.0 hardcoded vs orchestrator MIN_RR=1.5 env).
# See orchestrator.py:105-111 for the canonical env-var definition.
MIN_RR = float(_os.getenv("MIN_RR", "2.0"))
MAX_SHARPE = float(_os.getenv("HEAVY_SWEEP_MAX_SHARPE", "10.0"))
MIN_TRADES = int(_os.getenv("HEAVY_SWEEP_MIN_TRADES", "10"))

# TimesFM confidence gate: skip heavy sweep if |p_timesfm - 0.5| > 0.1
TIMESFM_CONFIDENT_THRESHOLD = 0.10

# Heavy-sweep concurrency cap. Firing all 14 symbols at once (2026-08-09)
# exhausted the shared Redis pool (max_connections=10) — every symbol opens
# a `timesfm:cached` read at the same instant → MaxConnectionsError on the
# last 4 symbols. Cap concurrent per-symbol sweeps so the pool and the
# CPU-bound grid-search threads stay under their limits; the grid searches
# dominate wall time anyway.
HEAVY_SWEEP_MAX_CONCURRENCY = 4

# When calibration is CALIBRATED, skip the next heavy sweep for 12 h
SWEEP_SKIP_CALIBRATED_HOURS = 12


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


def pick_best_combo(
    combos: list[ComboResult],
    min_rr: float = MIN_RR,
    max_sharpe: float = MAX_SHARPE,
    min_trades: int = MIN_TRADES,
) -> Optional[ComboResult]:
    """Pick the best combo by Sharpe, applying filter gates.

    Filters:
      • rr = tp/sl >= min_rr
      • sharpe <= max_sharpe (reject absurd outliers — likely overfit)
      • total_trades >= min_trades
    """
    candidates = [
        c for c in combos
        if c.tp_bricks / max(c.sl_bricks, 1) >= min_rr
        and c.sharpe <= max_sharpe
        and c.total_trades >= min_trades
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.sharpe)


# ─── Mix-in ──────────────────────────────────────────────────────────────

# p_historical fetch cache (R3, 2026-08-23, worklog/
# 20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md). Calibration bias changes on
# the 4h EOD-correction / daily EOD-resolver cadence, not every sweep tick —
# a 15-minute cache (same TTL as the orchestrator's TimesFM cache) avoids a
# Supabase round-trip per symbol per tick for data that's effectively
# static within that window. Lives here (not orchestrator.py) because
# BOTH the light-sweep path (SweepOrchestrator._process_symbol) and the
# heavy-sweep path (this mixin) need it, and only this mixin's methods are
# guaranteed on a bare ``HeavySweepMixin`` test double (see
# tests/test_heavy_sweep_resilience.py's ``_make_orchestrator()``).
P_HISTORICAL_CACHE_TTL_S = 900  # 15 minutes


class HeavySweepMixin:
    """Mix-in providing heavy-sweep methods to ``SweepOrchestrator``.

    The orchestrator inherits this so the heavy-sweep methods can access
    ``self._redis``, ``self._ev_engine``, ``self._sizer``, ``self._scheduler``,
    etc. without passing them as arguments.

    Methods added:
      • ``run_heavy_sweep_for_symbol(sym)`` — single-symbol heavy sweep
      • ``_heavy_sweep_group(cron_expr, symbols)`` — cron tick handler
      • ``_renko_grid_search_sync(prices, brick_sizes)`` — CPU-bound grid
      • ``_regime_sync(prices)`` — CPU-bound HMM regime
      • ``_kelly_sync(prices, asset_class)`` — CPU-bound Kelly sizing
      • ``_persist_brick_params_to_yaml(symbol, brick, sl, tp)`` — YAML write
      • ``_fetch_p_historical_cached(symbol)`` — cached p_historical fetch
        (R3), shared with the light-sweep path via inheritance.
    """

    async def _fetch_p_historical_cached(self, symbol: str) -> Optional[float]:
        """Bayesian-shrunk realized win rate for ``symbol`` (R3 EV-blend
        source), cached for ``P_HISTORICAL_CACHE_TTL_S``. Mirrors
        ``orchestrator._fetch_timesfm_cached``.

        Requires ``self._p_historical_cache`` (a ``dict[str, tuple[float |
        None, float]]``) to exist — ``SweepOrchestrator.__init__`` sets it;
        test doubles constructing a bare mixin instance must stub it too.
        """
        cache = self._p_historical_cache
        now = time.time()
        cached = cache.get(symbol)
        if cached is not None:
            value, ts = cached
            if now - ts < P_HISTORICAL_CACHE_TTL_S:
                return value
            cache.pop(symbol, None)

        from regime_platform.sweep_orchestrator.calibration_enforcement import (
            fetch_historical_p_win,
        )
        try:
            value = await fetch_historical_p_win(symbol)
        except Exception as exc:
            logger.debug("%s: p_historical fetch failed: %s", symbol, exc)
            value = None
        cache[symbol] = (value, now)
        return value

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
        # other or the event loop. Concurrency is CAPPED: the shared
        # Redis pool has max_connections=10 and every symbol opens a
        # `timesfm:cached` read at start — firing all 14 at once
        # exhausted the pool (MaxConnectionsError on the metals,
        # 2026-08-09). The cap also bounds the to_thread fan-out.
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
        """Run the heavy sweep for one symbol.

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

            # ── 2. TimesFM confidence gate (cache-first) ──────────────
            # Skip the heavy sweep entirely if TimesFM is already confident
            # (|p_timesfm - 0.5| > 0.1). This negates the need for daily
            # heavy sweeps — TimesFM handles non-stationarity 24/7.
            # The `timesfm:cached` key is ADVISORY: a Redis failure (pool
            # exhausted, connection refused, Redis disabled) must degrade to
            # cache-miss and proceed — it must never fail the sweep
            # (2026-08-09: MaxConnectionsError killed the 4 metals).
            cached_p_str = None
            if self._redis is not None:
                try:
                    cached_p_str = await self._redis.get(f"timesfm:cached:{symbol}")
                except Exception as exc:
                    logger.warning(
                        "[sweep:heavy] %s: timesfm cache read failed (%s) — proceeding without cache",
                        symbol, type(exc).__name__,
                    )
            if cached_p_str:
                try:
                    cached_p = float(cached_p_str)
                    if abs(cached_p - 0.5) > TIMESFM_CONFIDENT_THRESHOLD:
                        result.skipped = True
                        result.skip_reason = (
                            f"timesfm_confident_cached (p={cached_p:.4f})"
                        )
                        result.duration_ms = int((time.time() - start_ts) * 1000)
                        logger.info(
                            "[sweep:heavy] %s: SKIPPED — %s",
                            symbol, result.skip_reason,
                        )
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt cache, proceed

            p_timesfm_result = await _fetch_timesfm_cached(symbol, prices)
            p_timesfm: Optional[float] = None
            if p_timesfm_result is not None:
                p_timesfm, _ = p_timesfm_result
                if abs(p_timesfm - 0.5) > TIMESFM_CONFIDENT_THRESHOLD:
                    # Cache the confident value so subsequent sweeps skip too
                    await self._redis.set(
                        f"timesfm:cached:{symbol}", p_timesfm, ex=86400,
                    )
                    result.skipped = True
                    result.skip_reason = (
                        f"timesfm_confident (p={p_timesfm:.4f})"
                    )
                    result.p_timesfm = p_timesfm
                    result.duration_ms = int((time.time() - start_ts) * 1000)
                    logger.info(
                        "[sweep:heavy] %s: SKIPPED — %s",
                        symbol, result.skip_reason,
                    )
                    return result

            result.p_timesfm = p_timesfm

            # ── 3. ATR(14) + 5 brick sizes ────────────────────────────
            current_price = float(prices[-1])
            atr = compute_atr(prices, period=14)
            brick_sizes = generate_brick_sizes(atr, current_price)
            logger.info(
                "[sweep:heavy] %s: ATR=%.4f, price=%.2f, bricks=%s",
                symbol, atr, current_price,
                [f"{b:.4f}" for b in brick_sizes],
            )

            # ── 4. Grid + regime + Kelly in parallel ──────────────────
            # Each runs in asyncio.to_thread so the event loop stays
            # responsive (light-sweep ticks can fire concurrently).
            grid_task = asyncio.to_thread(
                self._renko_grid_search_sync, prices, brick_sizes, symbol,
            )
            regime_task = asyncio.to_thread(self._regime_sync, prices)
            kelly_task = asyncio.to_thread(
                self._kelly_sync, prices, sym.get("asset_class", ""),
            )
            grid_results, regime_snapshot, kelly_result = await asyncio.gather(
                grid_task, regime_task, kelly_task,
            )

            result.n_combos_tested = len(grid_results)
            result.regime_label = regime_snapshot.regime_label or "unknown"
            result.regime_conf = regime_snapshot.confidence
            result.kelly_f = kelly_result.recommended_f

            # ── 5. Pick best combo by Sharpe ──────────────────────────
            best = pick_best_combo(grid_results)
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

            # ── 6. Calibration bias → sweep_skip_until ────────────────
            from regime_platform.sweep_orchestrator.supabase_client import (
                fetch_calibration_bias,
            )
            venue_name = _derive_venue(sym)
            cal = await fetch_calibration_bias(symbol, venue=venue_name)
            sweep_skip_until: Optional[datetime] = None
            if cal and cal.get("status") == "CALIBRATED":
                sweep_skip_until = datetime.now(timezone.utc) + timedelta(
                    hours=SWEEP_SKIP_CALIBRATED_HOURS,
                )
                logger.info(
                    "[sweep:heavy] %s: CALIBRATED — skip_until=%s",
                    symbol, sweep_skip_until.isoformat(),
                )

            # ── 7. Persist optimised brick params to YAML ─────────────
            await self._persist_brick_params_to_yaml(
                symbol, best.brick_size, best.sl_bricks, best.tp_bricks,
            )
            result.persisted_yaml = True

            # Cache p_timesfm so the next heavy sweep can skip if confident.
            # Best-effort — a Redis failure here must not fail the sweep.
            if p_timesfm is not None and self._redis is not None:
                try:
                    await self._redis.set(
                        f"timesfm:cached:{symbol}", p_timesfm, ex=86400,
                    )
                except Exception as exc:
                    logger.warning(
                        "[sweep:heavy] %s: timesfm cache write failed (%s) — ignoring",
                        symbol, type(exc).__name__,
                    )

            # ── 8. EV compute with HMM markov (replaces discrete chain) ─
            direction = "short" if "bear" in result.regime_label.lower() else "long"
            markov_T, markov_current = self._hmm_model.emit_markov(symbol)

            # ── 8a. TA indicator snapshot (migration 036) ──────────────
            # Pull the latest 5 TA values from the HMM feature buffer for
            # persistence to nt_sweep_result. Best-effort — None when the
            # buffer is empty (cold start). Heavy sweep doesn't write to
            # nt_regime_log (only the light sweep does), so we only need
            # the values for the log_sweep_result call below.
            from regime_platform.services.hmm import latest_ta
            ta_snapshot = latest_ta(self._hmm_buffer, symbol)

            # p_historical (R3, 2026-08-23): same Bayesian-shrunk realized
            # win-rate source as the light sweep, reused here for blend
            # consistency across both sweep paths — replaces the old
            # p_pattern=None (always-null server-side source; removed
            # rather than kept, see ev_engine.py's module docstring).
            _p_historical = await self._fetch_p_historical_cached(symbol)
            ev_result = self._ev_engine.compute_ev(
                trend_probs=dict(zip(
                    # RegimeHMM.TREND_LABELS — accessed via the snapshot's
                    # internal arrays; the orchestrator's _process_symbol
                    # does the same zip.
                    self._regime_trend_labels(),
                    regime_snapshot.trend_probs,
                )),
                direction=direction,
                p_historical=_p_historical,
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
                        ))
                    except Exception as exc:
                        logger.warning(
                            "[sweep:heavy] %s: combo (brick=%.4f, sl=%d, tp=%d) failed: %s",
                            symbol, brick, sl, tp, exc,
                        )
                        continue
        return results

    def _regime_sync(self, prices: list[float]):
        """CPU-bound HMM regime detection. Runs in asyncio.to_thread."""
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
        return model.predict(np_prices)

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
        """Build + push qualified heavy-sweep signal to Redis Stream.

        Same XADD path as the light sweep (``signal.raw.noble_trader``).
        The agent listener treats heavy + light signals identically —
        the heavy sweep just produces fewer, more-optimised signals.
        """
        import json
        from regime_platform.sweep_orchestrator.orchestrator import _derive_venue

        now_ms = int(time.time() * 1000)
        venue_name = _derive_venue(sym)
        effective_kelly = kelly_f * ev_result.ev_scale

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
            "calibration_status": cal["status"] if cal else None,
            "calibration_enforced": bool(calib_adj and calib_adj.get("applied")),
            "sharpe": round(best.sharpe, 4),
            "n_combos_tested": True,  # marker — agent can show "optimised" badge
            "qualified": True,
        })

        try:
            if not settings.NOBLE_TRADER_REDIS_PUBLISH_ENABLED:
                logger.info(
                    "[sweep:heavy] %s: Redis publish DISABLED — qualified %s signal, skipping Redis write",
                    symbol, trade_signal["signal"],
                )
            elif self._redis is None:
                # Redis not configured at all (proxy-retirement direction).
                # The signal is still persisted to Supabase + realtime, so
                # this is not a data loss — just no Redis stream write.
                logger.info(
                    "[sweep:heavy] %s: Redis not configured — skipping Redis write for qualified %s signal",
                    symbol, trade_signal["signal"],
                )
            else:
                pipe = self._redis.pipeline(transaction=False)
                pipe.publish("signal.raw.noble_trader", agent_payload)
                pipe.xadd(
                    "signal.raw.noble_trader",
                    {"payload": agent_payload},
                    maxlen=10000,
                    approximate=True,
                )
                await pipe.execute()
                logger.info(
                    "[sweep:heavy] %s: PUBLISHED %s signal (sharpe=%.4f, brick=%.4f, sl=%d, tp=%d)",
                    symbol, trade_signal["signal"], best.sharpe,
                    best.brick_size, best.sl_bricks, best.tp_bricks,
                )
        except Exception as exc:
            logger.error(
                "[sweep:heavy] %s: Redis push failed — %s", symbol, exc,
            )

        # ── Direct Discord delivery (Option 2 — decoupled) ─────────────
        # Same decoupling as the light sweep: when the Redis feed is off
        # (proxy retirement) the qualified heavy signal is sent DIRECTLY
        # to the Discord notifier, gated by
        # NOBLE_TRADER_DISCORD_SIGNALS_ENABLED. Skipped when Redis
        # delivery is active (aggregator already handles Discord).
        try:
            from regime_platform.sweep_orchestrator.discord_delivery import (
                maybe_send_discord_signal,
            )

            redis_delivery_active = bool(
                settings.NOBLE_TRADER_REDIS_PUBLISH_ENABLED
                and self._redis is not None
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
                    redis_delivery_active=redis_delivery_active,
                )
            )
        except Exception as discord_exc:
            logger.warning(
                "[sweep:heavy] %s: direct Discord dispatch failed — %s: %s",
                symbol, type(discord_exc).__name__, discord_exc,
            )
