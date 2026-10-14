"""
Trend Filter — medium-term (10-50 day) SMA crossover + RSI read
════════════════════════════════════════════════════════════════════

worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS5/SS7.
Sibling to ``macro_filter.py`` (long-range, 200-day, static level check),
not a rewrite of it — this module answers a different question: is
there a *recent* SMA-10/SMA-50 crossover (a reversal), or an
*established* trend, or neither (consolidating)? RSI(14) supplies the
momentum/overbought-oversold read the same external analysis used.

Deliberately separate from ``macro_filter.py``'s output — same
"two separate parts, not one metric" convention that module already
establishes for macro vs. win/loss evidence.

Mode gate (``config/feature_flags.yaml``, ``trend_regime_mode``):
  off:    skip the read entirely (no Supabase round-trip). Default.
  shadow: compute + capture trend_label/trend_strength on nt_signal_sim,
          do NOT touch Kelly sizing.
  live:   additionally multiply effective_kelly by
          ``kelly_trend_multiplier()``.

Usage (standalone daily refresh):
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.trend_filter
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np

from regime_platform.core.feature_flags import (
    get_trend_boost_scale,
    get_trend_crossover_freshness_days,
    get_trend_penalty_scale,
    get_trend_rsi_overbought,
    get_trend_rsi_oversold,
    get_trend_rsi_period,
    get_trend_sma_long_days,
    get_trend_sma_mid_days,
    get_trend_sma_short_days,
    get_trend_strength_saturation,
)
from regime_platform.services.hmm.model import _compute_rsi
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_active_symbols,
    fetch_max_computed_at,
    fetch_trend_regime,
    upsert_trend_regime,
)

logger = logging.getLogger("regime.sweep.trend_filter")

# Extra daily bars fetched per refresh beyond the strict minimum, so a
# short data gap (a market holiday, a late API backfill) doesn't flip a
# symbol to "insufficient history" on a borderline day.
_FETCH_BUFFER_DAYS = 20


def compute_trend_regime(daily_closes: list[float]) -> dict[str, Any] | None:
    """Pure calculation — no I/O. Returns ``None`` if there isn't enough
    history for the longest SMA window plus the crossover-recency
    lookback.

    Unlike ``compute_macro_regime``, this needs no ``prev_*`` argument
    for continuity — crossover recency (``days_since_crossover``) is
    derived directly from the closes array itself by recomputing the
    SMA-short-vs-SMA-long sign for each of the last
    ``crossover_freshness_days + 1`` trailing days and finding where (if
    anywhere within that window) the sign last flipped. A flip found
    further back than the freshness window is treated as an established
    trend, not a fresh reversal — ``days_since_crossover`` reports the
    sentinel ``freshness + 1`` in that case rather than an exact (and
    untracked) day count.
    """
    sma_short_days = get_trend_sma_short_days()
    sma_mid_days = get_trend_sma_mid_days()
    sma_long_days = get_trend_sma_long_days()
    freshness = get_trend_crossover_freshness_days()
    rsi_period = get_trend_rsi_period()

    n = len(daily_closes)
    min_bars = max(sma_long_days, rsi_period + 1) + freshness
    if n < min_bars:
        return None

    def _sma(days: int, end_idx: int) -> float:
        window = daily_closes[end_idx - days + 1: end_idx + 1]
        return sum(window) / days

    # Trailing sign series for the last (freshness + 1) days, oldest to
    # newest, where sign = +1 when SMA_short >= SMA_long, else -1.
    signs: list[int] = []
    for offset in range(freshness, -1, -1):
        idx = n - 1 - offset
        short = _sma(sma_short_days, idx)
        long_ = _sma(sma_long_days, idx)
        signs.append(1 if short >= long_ else -1)

    today_sign = signs[-1]
    days_since_crossover = freshness + 1  # sentinel: no flip found in window
    for i in range(len(signs) - 2, -1, -1):
        if signs[i] != today_sign:
            days_since_crossover = len(signs) - 1 - i
            break

    last_crossover_direction = "bullish" if today_sign > 0 else "bearish"

    sma_10 = _sma(sma_short_days, n - 1)
    sma_20 = _sma(sma_mid_days, n - 1)
    sma_50 = _sma(sma_long_days, n - 1)

    saturation = get_trend_strength_saturation()
    raw_strength = abs(sma_10 - sma_50) / sma_50 if sma_50 else 0.0
    trend_strength = min(1.0, raw_strength / saturation) if saturation > 0 else 0.0

    rsi_series = _compute_rsi(np.asarray(daily_closes, dtype=float), period=rsi_period)
    rsi_14 = float(rsi_series[-1]) if len(rsi_series) else 50.0

    if rsi_14 >= get_trend_rsi_overbought():
        rsi_state = "overbought"
    elif rsi_14 <= get_trend_rsi_oversold():
        rsi_state = "oversold"
    else:
        rsi_state = "neutral"

    is_fresh = days_since_crossover <= freshness
    if is_fresh:
        trend_label = "reversal_up" if last_crossover_direction == "bullish" else "reversal_down"
    elif rsi_state == "neutral":
        trend_label = "consolidating"
    else:
        trend_label = "trending_up" if last_crossover_direction == "bullish" else "trending_down"

    return {
        "sma_10": round(sma_10, 6),
        "sma_20": round(sma_20, 6),
        "sma_50": round(sma_50, 6),
        "rsi_14": round(rsi_14, 4),
        "rsi_state": rsi_state,
        "days_since_crossover": days_since_crossover,
        "last_crossover_direction": last_crossover_direction,
        "trend_label": trend_label,
        "trend_strength": round(trend_strength, 4),
    }


def kelly_trend_multiplier(
    trend_label: str | None,
    trend_strength: float | None,
    direction: str,
) -> float:
    """Kelly multiplier for one trade given the current trend read.

    ``direction`` is the trade's own signal (``"buy"``/``"sell"`` —
    callers only invoke this for non-neutral signals). ``consolidating``,
    a missing label, or a missing strength is a no-op (``1.0``); an
    aligned label boosts, an opposed label penalizes, scaled by
    ``trend_strength`` (0-1) and clamped to ``[0.25, 1.5]`` — same hard
    safety band as ``kelly_macro_multiplier`` and every other multiplier
    in this codebase.
    """
    if not trend_label or trend_label == "consolidating" or trend_strength is None:
        return 1.0

    is_bullish = trend_label in ("reversal_up", "trending_up")
    is_bearish = trend_label in ("reversal_down", "trending_down")
    if not is_bullish and not is_bearish:
        return 1.0

    implied = "bull" if direction == "buy" else "bear"
    strength = max(0.0, min(1.0, float(trend_strength)))
    agrees = (implied == "bull" and is_bullish) or (implied == "bear" and is_bearish)
    if agrees:
        mult = 1.0 + strength * get_trend_boost_scale()
    else:
        mult = 1.0 - strength * get_trend_penalty_scale()
    return max(0.25, min(1.5, mult))


async def get_trend_regime(symbol: str) -> dict[str, Any] | None:
    """Live per-tick read used by both sweep paths — thin wrapper over
    ``supabase_client.fetch_trend_regime``, kept as its own function so
    the sweep paths import from this module (matching ``macro_filter``'s
    own convention) rather than reaching into ``supabase_client``
    directly."""
    return await fetch_trend_regime(symbol)


async def refresh_trend_regime(symbols: list[str] | None = None, force: bool = False) -> list[dict[str, Any]]:
    """Job: fetch fresh daily bars, recompute, upsert ``nt_trend_regime``.
    Returns the computed rows. Best-effort: logs and returns an empty
    list on failure, never raises.

    Does its own daily-bar batch fetch rather than sharing
    ``macro_filter.refresh_macro_regime``'s — an acceptable second daily
    TradingView batch call at this cadence (once/day/symbol), not a hot
    path; sharing the fetch is a possible future optimization, not a
    build blocker (worklog SS7.5).

    ``force=True`` bypasses the freshness guard — used by the startup
    catch-up when the table is detected as stale. When ``force=False``
    (the periodic schedule, HARM-04), a lightweight 2-bar probe fetch
    checks whether any daily bar has closed since the last computation —
    if not, the expensive fetch is skipped entirely.
    """
    try:
        active = await fetch_active_symbols()
    except Exception as exc:
        logger.error("trend_filter: fetch_active_symbols failed: %s: %r", type(exc).__name__, exc)
        return []
    if symbols:
        active = [s for s in active if s.get("symbol") in symbols]
    if not active:
        logger.info("trend_filter: no active symbols to refresh")
        return []

    # ── HARM-04 freshness guard ────────────────────────────────────────
    # Skip the expensive daily-bar fetch when no new daily close has
    # arrived since the last computation. Uses a 2-bar probe (negligible
    # bandwidth) to check the latest bar's timestamp against max(computed_at).
    # The guard is bypassed with force=True (startup catch-up).
    if not force:
        try:
            last_computed = await fetch_max_computed_at("nt_trend_regime")
            if last_computed is not None:
                probe_result = await fetch_tradingview_batch(
                    active, 2, "1D", include_timestamps=True
                )
                if isinstance(probe_result, tuple):
                    _, ts_map = probe_result
                else:
                    ts_map = {}
                computed_epoch = last_computed.timestamp()
                fresh = True
                for sym_name, ts_list in ts_map.items():
                    if ts_list and len(ts_list) >= 1:
                        latest_ts = ts_list[-1]
                        if latest_ts is not None and latest_ts > computed_epoch:
                            fresh = False
                            break
                if fresh:
                    logger.debug(
                        "trend_filter: freshness guard skipped — daily bars "
                        "unchanged since %s", last_computed
                    )
                    return []
        except Exception as exc:
            logger.debug("trend_filter: freshness probe failed (%s) — proceeding", exc)
            # Fail-open: proceed with full refresh

    sma_long_days = get_trend_sma_long_days()
    freshness = get_trend_crossover_freshness_days()
    rsi_period = get_trend_rsi_period()
    n_bars = max(sma_long_days, rsi_period + 1) + freshness + _FETCH_BUFFER_DAYS
    try:
        daily_closes_by_symbol = await fetch_tradingview_batch(active, n_bars, "1D")
    except Exception as exc:
        logger.error("trend_filter: daily bar fetch failed: %s: %r", type(exc).__name__, exc)
        return []

    output_rows: list[dict[str, Any]] = []
    for sym in active:
        symbol = sym.get("symbol")
        closes = daily_closes_by_symbol.get(symbol)
        if not symbol or not closes:
            continue

        computed = compute_trend_regime(closes)
        if computed is None:
            logger.warning(
                "trend_filter: %s: insufficient daily history (%d bars, need >=%d)",
                symbol, len(closes), max(sma_long_days, rsi_period + 1) + freshness,
            )
            continue

        output_rows.append({
            "symbol": symbol,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            **computed,
        })

    try:
        await upsert_trend_regime(output_rows)
    except Exception as exc:
        logger.error("trend_filter: upsert failed: %s: %r", type(exc).__name__, exc)

    logger.info("trend_filter: refreshed %d symbol(s)", len(output_rows))
    return output_rows


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No trend regime rows computed.")
        return
    print(
        f"{'Symbol':<10} {'Label':<14} {'Strength':>9} {'RSI':>7} "
        f"{'RSIState':<11} {'DaysSince':>10} {'Dir':<8}"
    )
    print("-" * 75)
    for row in sorted(rows, key=lambda r: r["symbol"]):
        print(
            f"{row['symbol']:<10} {row['trend_label']:<14} "
            f"{row['trend_strength']:>9.4f} {row['rsi_14']:>7.2f} "
            f"{row['rsi_state']:<11} {row['days_since_crossover']:>10} "
            f"{row['last_crossover_direction']:<8}"
        )


async def main() -> None:
    """CLI entry: refresh the trend regime table and print a text report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = await refresh_trend_regime()
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
