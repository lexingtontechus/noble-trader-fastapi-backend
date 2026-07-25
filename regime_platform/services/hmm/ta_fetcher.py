"""
TVDA /api/ta/{symbol}/indicators concurrent fetcher for the HMM.

Fetches 5 TA indicators (ADX, AO, BB Power, MACD, RSI) for 20 symbols
concurrently via asyncio.gather. Called from the sweep cron tick — no
separate poller task.

This is a SEPARATE consumer from the proxy's /api/ta call (which fetches
macd, atr, bollinger for p_micro_TA). The backend calls TVDA directly
with its own indicator list.

Extracted indicators are pushed into the FeatureBuffer at indices 4-8
(see feature_buffer.py). Features 0-3 come from the quote fetcher + the
sweep's price fetch.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.services.hmm.feature_buffer import N_FEATURES, FeatureBuffer

logger = logging.getLogger("regime.hmm.ta_fetcher")

# The 5 TA indicators the HMM uses (confirmed by operator 2026-07-24).
HMM_TA_INDICATORS: list[str] = ["adx", "ao", "bb_power", "macd", "rsi"]


# ─── Concurrent fetch ───────────────────────────────────────────────────────


async def fetch_and_update(
    symbols: list[dict[str, Any]],
    buffer: FeatureBuffer,
) -> int:
    """Fetch /api/ta/{symbol}/indicators for all symbols concurrently.

    Called from the sweep cron tick. Uses asyncio.gather to fetch all 20
    symbols in parallel. Each symbol's 5 TA values are combined with the
    latest microstructure + log_return (carried forward from the buffer's
    previous bar) into a full 9-feature vector, then pushed.

    Args:
        symbols: list of symbol config dicts with "symbol" + "tradingview_symbol".
        buffer: the FeatureBuffer to push into.

    Returns:
        Number of symbols successfully fetched + pushed.
    """
    if not symbols:
        return 0

    api_key = settings.TRADINGVIEW_API_KEY
    if not api_key:
        logger.warning("hmm.ta: TRADINGVIEW_API_KEY not set — skipping")
        return 0

    # Fetch all symbols concurrently
    tasks = [_fetch_one(sym, api_key) for sym in symbols]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    pushed = 0
    for sym, result in zip(symbols, results):
        symbol = sym["symbol"]
        if isinstance(result, Exception):
            logger.warning(
                "hmm.ta: %s — fetch failed (%s: %s)",
                symbol, type(result).__name__, result,
            )
            continue
        if result is None:
            continue

        # Build the 9-feature vector
        vector = np.zeros(N_FEATURES, dtype=float)

        # Features 0-3: carry forward from the buffer's latest bar
        # (log_return + microstructure come from the quote fetcher + price
        # fetch; here we preserve the previous values so the new vector is
        # complete).
        latest = buffer.latest(symbol)
        if latest is not None:
            vector[0] = latest[0]  # log_return
            vector[1] = latest[1]  # effective_spread_bps
            vector[2] = latest[2]  # imbalance_I
            vector[3] = latest[3]  # volume_persistence

        # Features 4-8: TA indicators from TVDA
        vector[4] = _extract_numeric(result, "adx")
        vector[5] = _extract_numeric(result, "ao")
        vector[6] = _extract_numeric(result, "bb_power", "bbpower", "bb_power")
        vector[7] = _extract_numeric(result, "macd")
        vector[8] = _extract_numeric(result, "rsi")

        buffer.push(symbol, vector)
        pushed += 1

    logger.info("hmm.ta: fetched %d/%d symbols", pushed, len(symbols))
    return pushed


def _extract_numeric(
    raw: dict[str, Any],
    *keys: str,
) -> float:
    """Extract a numeric TA indicator value from the TVDA response.

    TVDA wraps TA in various shapes — try the provided keys (case-insensitive)
    in the common sub-dicts. Returns 0.0 if not found or not parseable.
    """
    if not isinstance(raw, dict):
        return 0.0

    # Try common wrapper locations
    inner = raw.get("data", raw)
    if not isinstance(inner, dict):
        inner = raw

    indicators_node = (
        inner.get("indicators")
        or inner.get("ta")
        or inner.get("technical")
        or inner
    )
    if not isinstance(indicators_node, dict):
        return 0.0

    for key in keys:
        # Try exact, upper, title case
        for variant in (key, key.upper(), key.title(), key.replace("_", "")):
            val = indicators_node.get(variant)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, dict):
                # e.g. {"macd": {"line": 0.5, "signal": 0.3, "hist": 0.2}}
                # Try common sub-keys
                for sub in ("line", "value", "macd", "hist", "histogram"):
                    sub_val = val.get(sub)
                    if isinstance(sub_val, (int, float)):
                        return float(sub_val)
            if isinstance(val, str):
                try:
                    return float(val)
                except ValueError:
                    pass

    return 0.0


async def _fetch_one(
    sym: dict[str, Any],
    api_key: str,
) -> dict[str, Any] | None:
    """Fetch /api/ta/{symbol}/indicators for one symbol.

    Returns the parsed JSON response dict, or None on failure.
    """
    tv_sym = sym.get("tradingview_symbol") or sym.get("symbol", "")
    if not tv_sym:
        return None

    bare_sym = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
    bare_sym = bare_sym.replace("/", "").upper()

    url = f"{settings.TRADINGVIEW_BASE_URL}/api/ta/{bare_sym}/indicators"
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
                    "hmm.ta: %s — 429 rate limited (retry after %ss)",
                    bare_sym, retry_after,
                )
                return None

            if resp.status_code >= 400:
                logger.warning(
                    "hmm.ta: %s — HTTP %d: %s",
                    bare_sym, resp.status_code, resp.text[:200],
                )
                return None

            return resp.json()

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "hmm.ta: %s — HTTP error %d: %s",
            bare_sym, exc.response.status_code, str(exc)[:200],
        )
        return None
    except Exception as exc:
        logger.warning(
            "hmm.ta: %s — error (%s: %s)",
            bare_sym, type(exc).__name__, str(exc)[:200],
        )
        return None
