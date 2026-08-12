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


# ─── TradingView Batch API (TDVA — sole data source) ─────────────


async def fetch_tradingview_batch(
    symbols: list[dict[str, Any]],
    hours: int = LIGHT_CANDLE_BARS,
    interval: str = "1h",
) -> dict[str, list[float]]:
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

    Returns
    -------
    dict[str, list[float]]
        Maps internal symbol name → list of close prices (ascending ts).
        Symbols that failed are omitted from the dict.
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

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json={"requests": requests_body})

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After", "5")
                    logger.warning(
                        "TradingView batch 429 rate limited (chunk %d/%d) — "
                        "retry after %ss. Skipping chunk.",
                        chunk_idx + 1, len(chunks), retry_after,
                    )
                    continue

                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "TradingView batch HTTP %d (chunk %d/%d): %s. Skipping chunk.",
                exc.response.status_code, chunk_idx + 1, len(chunks),
                exc.response.text[:200],
            )
            continue
        except Exception as exc:
            logger.warning(
                "TradingView batch failed (chunk %d/%d): %s. Skipping chunk.",
                chunk_idx + 1, len(chunks), exc,
            )
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
            if bars and isinstance(bars, list):
                if isinstance(bars[0], dict):
                    prices = [
                        float(b.get("close", b.get("c", 0)))
                        for b in bars
                        if b.get("close") is not None or b.get("c") is not None
                    ]
                elif isinstance(bars[0], (list, tuple)):
                    prices = [float(b[4]) if len(b) > 4 else 0.0 for b in bars]

            # Fallback to "current" if no history
            if not prices and isinstance(data_obj, dict):
                current = data_obj.get("current", {})
                if isinstance(current, dict) and current.get("close") is not None:
                    prices = [float(current["close"])]

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

            if prices:
                results[internal_sym] = prices
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
    return results


# ─── Convenience wrappers ────────────────────────────────────────


async def fetch_candles_for_symbols(
    symbols: list[dict[str, Any]],
    n_bars: int = LIGHT_CANDLE_BARS,
    interval: str = "1h",
) -> dict[str, list[float]]:
    """Thin wrapper around fetch_tradingview_batch for callers that don't
    need to know about the TV-specific implementation.

    Used by:
      • Light sweep (_sweep_group): n_bars=200, interval="1h"
      • Heavy sweep (_heavy_sweep_group): n_bars=2160, interval="1h"
    """
    return await fetch_tradingview_batch(symbols, hours=n_bars, interval=interval)
