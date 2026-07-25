"""
TVDA /api/quote/{symbol} concurrent fetcher for the HMM.

Fetches bid/ask/size/volume data for 20 symbols concurrently via asyncio.gather,
per TVDA's recommendation for multi-symbol batches. Called from the sweep cron
tick — no separate poller task.

Extracts 3 microstructure features:
  - effective_spread_bps = (ask - bid) / mid * 10000
  - imbalance_I = (bid_size - ask_size) / (bid_size + ask_size)
  - volume_persistence = volume / average_volume

These features are pushed into the FeatureBuffer at indices 1-3 (see
feature_buffer.py). Feature 0 (log_return) comes from the sweep's existing
price fetch; features 4-8 (TA) come from ta_fetcher.py.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.services.hmm.feature_buffer import N_FEATURES, FeatureBuffer

logger = logging.getLogger("regime.hmm.quote_fetcher")


# ─── Concurrent fetch ───────────────────────────────────────────────────────


async def fetch_and_update(
    symbols: list[dict[str, Any]],
    buffer: FeatureBuffer,
    prev_closes: dict[str, float] | None = None,
) -> int:
    """Fetch /api/quote for all symbols concurrently and push to the buffer.

    Called from the sweep cron tick. Uses asyncio.gather to fetch all 20
    symbols in parallel (~1-2s total). Each symbol's 3 microstructure
    features are combined with the latest log_return (from prev_closes) and
    the latest TA values (from the buffer's previous bar) into a full
    9-feature vector, then pushed.

    Args:
        symbols: list of symbol config dicts, each with "symbol" and
            "tradingview_symbol".
        buffer: the FeatureBuffer to push into.
        prev_closes: optional dict of {symbol: last_close} — used to compute
            log_return for the quote snapshot. If None, log_return is 0.0.

    Returns:
        Number of symbols successfully fetched + pushed.
    """
    if not symbols:
        return 0

    api_key = settings.TRADINGVIEW_API_KEY
    if not api_key:
        logger.warning("hmm.quote: TRADINGVIEW_API_KEY not set — skipping")
        return 0

    # Fetch all symbols concurrently
    tasks = [_fetch_one(sym, api_key) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    pushed = 0
    for sym, result in zip(symbols, results):
        symbol = sym["symbol"]
        if isinstance(result, Exception):
            logger.warning(
                "hmm.quote: %s — fetch failed (%s: %s)",
                symbol, type(result).__name__, result,
            )
            continue
        if result is None:
            continue

        # Build the 9-feature vector
        vector = np.zeros(N_FEATURES, dtype=float)

        # Feature 0: log_return
        last = result.get("last", 0.0)
        if prev_closes and symbol in prev_closes and prev_closes[symbol] > 0 and last > 0:
            vector[0] = math.log(last / prev_closes[symbol])

        # Features 1-3: microstructure
        bid = float(result.get("bid", 0.0))
        ask = float(result.get("ask", 0.0))
        bid_size = float(result.get("bid_size", 0.0))
        ask_size = float(result.get("ask_size", 0.0))
        volume = float(result.get("volume", 0.0))
        avg_volume = float(result.get("average_volume", 0.0))

        mid = (bid + ask) / 2.0
        if mid > 0:
            vector[1] = (ask - bid) / mid * 10000.0  # effective_spread_bps

        denom = bid_size + ask_size
        if denom > 0:
            vector[2] = (bid_size - ask_size) / denom  # imbalance_I

        if avg_volume > 0:
            vector[3] = volume / avg_volume  # volume_persistence

        # Features 4-8: TA — carry forward from the buffer's latest bar
        # (TA is fetched separately by ta_fetcher; here we just preserve
        # the previous TA values so the new vector is complete).
        latest = buffer.latest(symbol)
        if latest is not None:
            vector[4] = latest[4]  # adx
            vector[5] = latest[5]  # ao
            vector[6] = latest[6]  # bb_power
            vector[7] = latest[7]  # macd
            vector[8] = latest[8]  # rsi

        buffer.push(symbol, vector)
        pushed += 1

    logger.info("hmm.quote: fetched %d/%d symbols", pushed, len(symbols))
    return pushed


async def _fetch_one(
    sym: dict[str, Any],
    api_key: str,
) -> dict[str, Any] | None:
    """Fetch /api/quote/{symbol} for one symbol.

    Uses the tradingview_symbol (e.g., "COINBASE:BTCUSD") — the same format
    as the existing price_fetcher. The symbol is URL-encoded for the path.

    Returns the parsed JSON response dict, or None on failure.
    """
    tv_sym = sym.get("tradingview_symbol") or sym.get("symbol", "")
    if not tv_sym:
        return None

    # TVDA quote endpoint uses bare ticker (no exchange prefix)
    # Same normalization as the proxy's _normalize_symbol()
    bare_sym = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
    bare_sym = bare_sym.replace("/", "").upper()

    url = f"{settings.TRADINGVIEW_BASE_URL}/api/quote/{bare_sym}"
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": settings.TRADINGVIEW_API_HOST,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "5")
                logger.warning(
                    "hmm.quote: %s — 429 rate limited (retry after %ss)",
                    bare_sym, retry_after,
                )
                return None

            if resp.status_code >= 400:
                logger.warning(
                    "hmm.quote: %s — HTTP %d: %s",
                    bare_sym, resp.status_code, resp.text[:200],
                )
                return None

            data = resp.json()

        # TVDA response shapes vary — extract the quote fields
        # Common shapes:
        #   {"symbol": "...", "bid": ..., "ask": ..., "last": ..., ...}
        #   {"data": {"symbol": ..., "bid": ..., ...}}
        if isinstance(data, dict):
            if "bid" in data or "ask" in data or "last" in data:
                return data
            inner = data.get("data", {})
            if isinstance(inner, dict) and ("bid" in inner or "ask" in inner):
                return inner

        return None

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "hmm.quote: %s — HTTP error %d: %s",
            bare_sym, exc.response.status_code, str(exc)[:200],
        )
        return None
    except Exception as exc:
        logger.warning(
            "hmm.quote: %s — error (%s: %s)",
            bare_sym, type(exc).__name__, str(exc)[:200],
        )
        return None
