"""
TVDA /api/quote/batch concurrent fetcher for the HMM (Phase B — full
batch consolidation, 2026-08-05).

Fetches bid/ask/size/volume data for ALL symbols in ⌈N/10⌉ batched POST
calls (TVDA hard-limits /api/quote/batch to 10 symbols per request),
instead of the old per-symbol `GET /api/quote/{symbol}` fan-out that
tripped RapidAPI's 429 rate limit (11 concurrent single-symbol calls →
"fetched 0/11 symbols" every tick).

Request:
    POST /api/quote/batch
    {"symbols": ["COINBASE:BTCUSD", "OANDA:XAUUSD", ...]}   # max 10

Response:
    {"success": true, "data": {"total": N, "successful": N, "failed": 0,
      "data": [{"success": true, "symbol": "COINBASE:BTCUSD",
                "data": {"bid": ..., "ask": ..., "lp": ...,
                         "volume": ..., "average_volume": ...}}, ...]}}

Extracts 3 microstructure features:
  - effective_spread_bps = (ask - bid) / mid * 10000
  - imbalance_I = (bid_size - ask_size) / (bid_size + ask_size)
  - volume_persistence = volume / average_volume

These features are pushed into the FeatureBuffer at indices 1-3 (see
feature_buffer.py). Feature 0 (log_return) comes from the sweep's existing
price fetch; features 4-8 (TA) come from ta_fetcher.py (now computed
locally from the same batch OHLCV — see ta_fetcher.py).
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.services.hmm.feature_buffer import N_FEATURES, FeatureBuffer

logger = logging.getLogger("regime.hmm.quote_fetcher")

# TVDA /api/quote/batch hard-limits to 10 symbols per request (same limit
# as /api/price/batch). Larger lists are chunked into groups automatically.
BATCH_CHUNK_SIZE = 10

# Latest effective spread per internal symbol from the most recent
# fetch_and_update() (2026-09-24, Kairos v3 P0.5 — stamped as
# nt_signal_sim.spread_bps; reuses this call, no extra API request).
_LAST_SPREAD_BPS: dict[str, float] = {}


def spread_bps_by_symbol() -> dict[str, float]:
    """Latest (ask − bid)/mid × 1e4 per internal symbol (copy)."""
    return dict(_LAST_SPREAD_BPS)


# ─── Batched fetch ────────────────────────────────────────────────────────


async def fetch_and_update(
    symbols: list[dict[str, Any]],
    buffer: FeatureBuffer,
    prev_closes: dict[str, float] | None = None,
) -> int:
    """Fetch /api/quote/batch for all symbols and push to the buffer.

    Called from the sweep cron tick. Uses ⌈N/10⌉ batched POST calls instead
    of the old per-symbol fan-out (which 429'd on RapidAPI). Each symbol's
    3 microstructure features are combined with the latest log_return (from
    prev_closes) and the latest TA values (from the buffer's previous bar)
    into a full 9-feature vector, then pushed.

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

    # Map bare TV symbol → internal symbol for the batch response
    tv_to_internal = {
        _bare_tv_symbol(s): s["symbol"]
        for s in symbols
        if s.get("tradingview_symbol") or s.get("symbol")
    }
    if not tv_to_internal:
        return 0

    # Chunk into BATCH_CHUNK_SIZE groups (TVDA hard-limits per request)
    chunks = [
        symbols[i:i + BATCH_CHUNK_SIZE]
        for i in range(0, len(symbols), BATCH_CHUNK_SIZE)
    ]

    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": settings.TRADINGVIEW_API_HOST,
        "Content-Type": "application/json",
    }
    url = f"{settings.TRADINGVIEW_BASE_URL}/api/quote/batch"

    quotes: dict[str, dict[str, Any]] = {}
    for chunk_idx, chunk in enumerate(chunks):
        body = {
            "symbols": [
                _bare_tv_symbol(s)
                for s in chunk
                if s.get("tradingview_symbol") or s.get("symbol")
            ]
        }
        if not body["symbols"]:
            continue

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json=body)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "5")
                logger.warning(
                    "hmm.quote: batch 429 rate limited (chunk %d/%d) — "
                    "retry after %ss. Skipping chunk.",
                    chunk_idx + 1, len(chunks), retry_after,
                )
                continue

            if resp.status_code >= 400:
                logger.warning(
                    "hmm.quote: batch HTTP %d (chunk %d/%d): %s. Skipping chunk.",
                    resp.status_code, chunk_idx + 1, len(chunks),
                    resp.text[:200],
                )
                continue

            data = resp.json()
        except Exception as exc:
            logger.warning(
                "hmm.quote: batch failed (chunk %d/%d): %s. Skipping chunk.",
                chunk_idx + 1, len(chunks), exc,
            )
            continue

        # Parse batch response:
        # {"success": true, "data": {"total": N, "successful": N, "failed": 0,
        #   "data": [{"success": true, "symbol": "...",
        #             "data": {"bid": ..., "ask": ..., ...}}, ...]}}
        batch_data = data.get("data", {}) if isinstance(data, dict) else {}
        if isinstance(batch_data, dict):
            items = batch_data.get("data", [])
        elif isinstance(batch_data, list):
            items = batch_data
        else:
            items = []

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("success") is False:
                tv_sym = item.get("symbol", "?")
                logger.warning(
                    "hmm.quote: symbol %s returned success=false: %s",
                    tv_sym, str(item)[:200],
                )
                continue

            tv_sym = item.get("symbol", "")
            internal_sym = tv_to_internal.get(tv_sym) or tv_to_internal.get(
                _bare_tv_symbol({"tradingview_symbol": tv_sym}), tv_sym,
            )
            quote = item.get("data", item)
            if isinstance(quote, dict) and (
                "bid" in quote or "ask" in quote or "lp" in quote or "last" in quote
            ):
                quotes[internal_sym] = quote

    if not quotes:
        logger.warning("hmm.quote: fetched 0/%d symbols (no batch quotes)", len(symbols))
        return 0

    # Build the 9-feature vector per symbol
    pushed = 0
    for sym in symbols:
        symbol = sym["symbol"]
        result = quotes.get(symbol)
        if not result:
            continue

        vector = np.zeros(N_FEATURES, dtype=float)

        # Feature 0: log_return
        last = float(result.get("last") or result.get("lp") or 0.0)
        if prev_closes and symbol in prev_closes and prev_closes[symbol] > 0 and last > 0:
            vector[0] = math.log(last / prev_closes[symbol])

        # Features 1-3: microstructure
        bid = float(result.get("bid", 0.0) or 0.0)
        ask = float(result.get("ask", 0.0) or 0.0)
        bid_size = float(result.get("bid_size", 0.0) or 0.0)
        ask_size = float(result.get("ask_size", 0.0) or 0.0)
        volume = float(result.get("volume", 0.0) or 0.0)
        avg_volume = float(result.get("average_volume", 0.0) or 0.0)

        mid = (bid + ask) / 2.0
        if mid > 0:
            vector[1] = (ask - bid) / mid * 10000.0  # effective_spread_bps
            if ask > 0 and bid > 0 and ask >= bid:
                _LAST_SPREAD_BPS[symbol] = float(vector[1])

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


def _bare_tv_symbol(sym: dict[str, Any]) -> str:
    """Extract the bare TV ticker from a symbol config (same normalization
    as the proxy's _normalize_symbol())."""
    tv_sym = sym.get("tradingview_symbol") or sym.get("symbol", "")
    bare = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
    return bare.replace("/", "").upper()
