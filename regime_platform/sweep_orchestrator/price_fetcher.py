"""
Price Candle Fetcher (Phase B — TDVA-only)
══════════════════════════════════════════

All candle data is fetched from TradingView Data API (TDVA) via RapidAPI.
HyperLiquid, Alpaca, and LightningAI/Yahoo proxy paths were removed in
Phase B per design decision: TDVA is the sole data source for both light
and heavy sweeps.

Public API:
  • ``fetch_tradingview_batch(symbols, hours, interval)`` — batch fetch
    multiple symbols in one HTTP call. Used by both light sweep (200 bars)
    and heavy sweep (2160 bars = 90d hourly).

TheTradingView batch endpoint accepts up to 10 symbols per request
(BATCH_CHUNK_SIZE); larger lists are auto-chunked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("regime.sweep.prices")

# ─── Constants ────────────────────────────────────────────────────

SWEEP_CANDLE_HOURS = 2160          # 90 days × 24h (heavy sweep)
SWEEP_CANDLE_INTERVAL = "1h"
LIGHT_CANDLE_BARS = 200            # light sweep default

# TradingView batch endpoint hard-limits to 10 symbols per request.
# Larger lists are chunked into BATCH_CHUNK_SIZE groups automatically.
BATCH_CHUNK_SIZE = 10

# 429 retry (2026-08-24): one bounded retry per chunk, honoring the
# Retry-After header the API already sends — previously a 429 was just
# logged and the chunk skipped outright, discarding data that a few
# seconds' wait would have recovered (confirmed live: eod_resolver lost an
# entire day's resolution attempt for 4 symbols to a 429 burst that a
# single retry would very likely have survived). Capped so a
# misbehaving/huge Retry-After value can't stall a sweep tick.
MAX_429_RETRIES = 1
RETRY_AFTER_CAP_SECONDS = 10.0

# Epoch-unit heuristic for the raw candle "time"/"t" field (2026-09-09,
# worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7): TDVA's
# documented candle shape is {"time": ..., "open": ..., ..., "close": ...}
# (confirmed via git history, commit c9484d8's docstring) but the unit
# (seconds vs. milliseconds) has never been consumed anywhere downstream
# until now, so it isn't verified against a live response. 1e12 is a
# standard seconds-vs-milliseconds discriminator (any real-world date is
# below 1e12 as Unix seconds — that threshold is year ~33658 — but above
# 1e12 as Unix milliseconds for every date since 2001), not a fitted or
# guessed constant, so a raw value in either unit normalises correctly.
_EPOCH_MS_THRESHOLD = 1e12


def _opt_float(raw: Any) -> float | None:
    """float(raw) or None for a missing/non-numeric OHLCV field."""
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


def _normalize_epoch_seconds(raw: Any) -> float | None:
    """Best-effort: coerce a raw candle timestamp field to Unix seconds.

    Returns ``None`` when ``raw`` is missing or not numeric — callers must
    treat that as "timestamp unavailable for this bar", not as epoch 0.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > _EPOCH_MS_THRESHOLD:
        value /= 1000.0
    return value


# ─── TradingView Batch API (TDVA — sole data source) ─────────────


async def fetch_tradingview_batch(
    symbols: list[dict[str, Any]],
    hours: int = LIGHT_CANDLE_BARS,
    interval: str = "1h",
    include_timestamps: bool = False,
    ohlcv_out: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[float]] | tuple[dict[str, list[float]], dict[str, list[float | None]]]:
    """Fetch candles for multiple TradingView symbols in batched API calls.

    Uses POST /api/price/batch — sends up to BATCH_CHUNK_SIZE (10) symbol
    requests per HTTP call. For N symbols, makes ⌈N/10⌉ HTTP calls.

    Parameters
    ----------
    symbols : list[dict]
        List of symbol config dicts. Each must have:
          - ``symbol``: internal name (e.g., "BTCUSD") — used as result key
          - ``tradingview_symbol``: TV symbol (e.g., "COINBASE:BTCUSD")
    hours : int
        Number of bars to fetch (default: 200 for light sweep).
        Heavy sweep passes 2160 (90 days × 24h).
    interval : str
        Candle interval — "1h" or "1D".
    include_timestamps : bool
        Default False — preserves the original return shape for every
        existing caller (eod_resolver, heavy_sweep, macro_filter,
        bracket_reachability, the backfill scripts, ...). When True,
        returns a ``(prices, timestamps)`` tuple instead; ``timestamps``
        is index-aligned with ``prices`` per symbol, with ``None`` for any
        bar whose raw payload had no parseable time field (2026-09-09,
        worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7
        — added so the Renko brick capture can use real per-bar timestamps
        instead of a fabricated reconstruction; see orchestrator.py's
        brick-capture block and supabase_client.log_renko_bricks).
    ohlcv_out : dict | None
        Optional out-parameter (2026-09-24, Kairos v3 P0.5). When a dict is
        passed, it is filled with ``{symbol: [{"t","o","h","l","c","v"}, ...]}``
        ascending and index-aligned with the returned closes (``None`` for a
        field the bar lacked). The return value/shape is unchanged either
        way, so no existing caller is affected.

    Returns
    -------
    dict[str, list[float]]
        Maps internal symbol name → list of close prices (ascending ts).
        Symbols that failed are omitted from the dict. (default shape)
    tuple[dict[str, list[float]], dict[str, list[float | None]]]
        ``(prices, timestamps)`` when ``include_timestamps=True``.
    """
    from regime_platform.core.settings import settings

    api_key = settings.TRADINGVIEW_API_KEY
    if not api_key:
        logger.warning("TradingView credentials not configured — cannot batch fetch")
        return {}

    # Filter to only symbols that have a tradingview_symbol.
    # After Phase B consolidation, ALL active symbols should have one —
    # this filter is defensive against incomplete nt_symbol rows.
    tv_symbols = [
        s for s in symbols
        if s.get("tradingview_symbol")
    ]
    if not tv_symbols:
        return {}

    # Map interval to TradingView timeframe
    interval_map = {"1h": "60", "1H": "60", "15m": "15", "5m": "5",
                    "4h": "240", "4H": "240", "1D": "D", "1d": "D"}
    tv_timeframe = interval_map.get(interval, "60")
    range_bars = min(hours, 5000)

    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": settings.TRADINGVIEW_API_HOST,
        "Content-Type": "application/json",
    }

    url = f"{settings.TRADINGVIEW_BASE_URL}/api/price/batch"

    # Build reverse map: TV symbol → internal symbol (used to key results)
    tv_to_internal = {s["tradingview_symbol"]: s["symbol"] for s in tv_symbols}

    results: dict[str, list[float]] = {}
    # Index-aligned with `results[sym]`; only populated/consulted when
    # include_timestamps=True. Kept separate from `results` rather than
    # folded into a richer per-symbol structure so the default return
    # shape (dict[str, list[float]]) is byte-for-byte unchanged for every
    # caller that doesn't ask for timestamps.
    results_ts: dict[str, list[float | None]] = {}

    # Chunk into BATCH_CHUNK_SIZE groups (TV hard-limits per request)
    chunks = [
        tv_symbols[i:i + BATCH_CHUNK_SIZE]
        for i in range(0, len(tv_symbols), BATCH_CHUNK_SIZE)
    ]

    for chunk_idx, chunk in enumerate(chunks):
        requests_body = [
            {
                "symbol": s["tradingview_symbol"],
                "timeframe": tv_timeframe,
                "range": range_bars,  # integer, NOT string
            }
            for s in chunk
        ]

        data: dict[str, Any] | None = None
        for attempt in range(MAX_429_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, headers=headers, json={"requests": requests_body})

                    if resp.status_code == 429:
                        try:
                            retry_after = min(
                                float(resp.headers.get("Retry-After", "5")),
                                RETRY_AFTER_CAP_SECONDS,
                            )
                        except (TypeError, ValueError):
                            retry_after = RETRY_AFTER_CAP_SECONDS
                        if attempt < MAX_429_RETRIES:
                            logger.warning(
                                "TradingView batch 429 rate limited (chunk %d/%d, "
                                "attempt %d/%d) — retrying after %.1fs.",
                                chunk_idx + 1, len(chunks), attempt + 1,
                                MAX_429_RETRIES + 1, retry_after,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        logger.warning(
                            "TradingView batch 429 rate limited (chunk %d/%d) — "
                            "retries exhausted. Skipping chunk.",
                            chunk_idx + 1, len(chunks),
                        )
                        break

                    resp.raise_for_status()
                    data = resp.json()
                    break
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "TradingView batch HTTP %d (chunk %d/%d): %s. Skipping chunk.",
                    exc.response.status_code, chunk_idx + 1, len(chunks),
                    exc.response.text[:200],
                )
                break
            except Exception as exc:
                logger.warning(
                    "TradingView batch failed (chunk %d/%d): %s. Skipping chunk.",
                    chunk_idx + 1, len(chunks), exc,
                )
                break

        if data is None:
            continue

        # Parse batch response:
        # {
        #   "success": true,
        #   "data": {
        #     "total": 4, "successful": 4, "failed": 0,
        #     "data": [
        #       {"success": true, "symbol": "COINBASE:BTCUSD", "current": {...}, "history": [...]},
        #       ...
        #     ]
        #   }
        # }
        batch_data = data.get("data", {})
        if isinstance(batch_data, dict):
            items = batch_data.get("data", [])
        elif isinstance(batch_data, list):
            items = batch_data
        else:
            items = []

        for item in items:
            if not isinstance(item, dict):
                continue
            if not item.get("success", True):
                tv_sym = item.get("symbol", "?")
                logger.warning(
                    "TradingView batch: symbol %s returned success=false: %s",
                    tv_sym, str(item)[:200],
                )
                continue

            tv_sym = item.get("symbol", "")
            internal_sym = tv_to_internal.get(tv_sym, tv_sym)

            # Extract close prices from history
            data_obj = item.get("data", item)  # may be nested
            bars = (
                data_obj.get("history", [])
                if isinstance(data_obj, dict)
                else data_obj.get("candles", [])
                if isinstance(data_obj, dict)
                else []
            )

            prices: list[float] = []
            # bar_timestamps stays index-aligned with `prices` (appended
            # together, one entry per accepted bar — including `None` when
            # a bar has no parseable time field) so a caller can zip() them
            # safely rather than trusting positional luck.
            bar_timestamps: list[float | None] = []
            ohlcv_rows: list[dict[str, Any]] = []
            if bars and isinstance(bars, list):
                if isinstance(bars[0], dict):
                    for b in bars:
                        close = b.get("close", b.get("c"))
                        if close is None:
                            continue
                        prices.append(float(close))
                        bar_timestamps.append(
                            _normalize_epoch_seconds(b.get("time", b.get("t")))
                        )
                        if ohlcv_out is not None:
                            ohlcv_rows.append({
                                "t": bar_timestamps[-1],
                                "o": _opt_float(b.get("open", b.get("o"))),
                                "h": _opt_float(b.get("max", b.get("high", b.get("h")))),
                                "l": _opt_float(b.get("min", b.get("low", b.get("l")))),
                                "c": float(close),
                                "v": _opt_float(b.get("volume", b.get("v"))),
                            })
                elif isinstance(bars[0], (list, tuple)):
                    for b in bars:
                        prices.append(float(b[4]) if len(b) > 4 else 0.0)
                        bar_timestamps.append(
                            _normalize_epoch_seconds(b[0]) if len(b) > 0 else None
                        )
                        if ohlcv_out is not None:
                            ohlcv_rows.append({
                                "t": bar_timestamps[-1],
                                "o": _opt_float(b[1]) if len(b) > 1 else None,
                                "h": _opt_float(b[2]) if len(b) > 2 else None,
                                "l": _opt_float(b[3]) if len(b) > 3 else None,
                                "c": prices[-1],
                                "v": _opt_float(b[5]) if len(b) > 5 else None,
                            })

            # Fallback to "current" if no history
            if not prices and isinstance(data_obj, dict):
                current = data_obj.get("current", {})
                if isinstance(current, dict) and current.get("close") is not None:
                    prices = [float(current["close"])]
                    bar_timestamps = [
                        _normalize_epoch_seconds(current.get("time", current.get("t")))
                    ]

            # ── ORDER NORMALISATION (2026-08-11) ──────────────────────────
            # TDVA /api/price/batch returns `history` in DESCENDING time
            # order (newest FIRST — timestamps decrease down the array).
            # Every downstream consumer (orchestrator, heavy sweep, HMM,
            # TimesFM, EOD resolver) assumes ASCENDING (oldest → newest)
            # so that prices[-1] is the current/latest close. Without this
            # reverse, current_price = prices[-1] was the ~8-12 day old
            # close → wrong entry/SL/TP levels (see worklog [9.2.0]).
            # Reverse here ONCE so all consumers stay correct.
            prices.reverse()
            bar_timestamps.reverse()
            ohlcv_rows.reverse()

            if prices:
                results[internal_sym] = prices
                results_ts[internal_sym] = bar_timestamps
                if ohlcv_out is not None and len(ohlcv_rows) == len(prices):
                    ohlcv_out[internal_sym] = ohlcv_rows
                logger.info(
                    "TradingView batch %s (%s): %d candles",
                    internal_sym, tv_sym, len(prices),
                )
            else:
                logger.warning(
                    "TradingView batch: no candles for %s (%s): %s",
                    internal_sym, tv_sym, str(item)[:200],
                )

    logger.info(
        "TradingView batch complete: %d/%d symbols successful "
        "(%d chunk(s), timeframe=%s, range=%s)",
        len(results), len(tv_symbols), len(chunks), tv_timeframe, range_bars,
    )
    if include_timestamps:
        return results, results_ts
    return results


# ─── Convenience wrappers ────────────────────────────────────────


async def fetch_candles_for_symbols(
    symbols: list[dict[str, Any]],
    n_bars: int = LIGHT_CANDLE_BARS,
    interval: str = "1h",
    include_timestamps: bool = False,
) -> dict[str, list[float]] | tuple[dict[str, list[float]], dict[str, list[float | None]]]:
    """Thin wrapper around fetch_tradingview_batch for callers that don't
    need to know about the TV-specific implementation.

    Used by:
      • Light sweep (_sweep_group): n_bars=200, interval="1h"
      • Heavy sweep (_heavy_sweep_group): n_bars=2160, interval="1h"

    ``include_timestamps`` (default False): forwarded as-is to
    ``fetch_tradingview_batch`` — see its docstring for the return-shape
    change when True. Unset by every pre-existing caller, so this wrapper's
    default return shape is unchanged.
    """
    return await fetch_tradingview_batch(
        symbols, hours=n_bars, interval=interval, include_timestamps=include_timestamps,
    )
