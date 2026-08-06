"""
TVDA TA fetcher for the HMM — Phase B full batch consolidation (2026-08-05).

The old per-symbol `GET /api/ta/{symbol}/indicators` fan-out (11 concurrent
calls) tripped RapidAPI's 429 rate limit — "hmm.ta: fetched 0/11 symbols"
every tick. TVDA exposes NO batch TA endpoint (only /api/price/batch and
/api/quote/batch exist), so the full consolidation computes the 5 HMM TA
indicators LOCALLY from the OHLCV closes the sweep already fetched via
/api/price/batch (valid_prices). This is the same code path the HMM uses
for bootstrap (model._compute_ta_indicators_local) — zero extra HTTP
calls, zero 429 exposure.

Extracted indicators are pushed into the FeatureBuffer at indices 4-8
(see feature_buffer.py). Features 0-3 come from the quote fetcher + the
sweep's price fetch.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from regime_platform.services.hmm.feature_buffer import N_FEATURES, FeatureBuffer
from regime_platform.services.hmm.model import _compute_ta_indicators_local

logger = logging.getLogger("regime.hmm.ta_fetcher")

# The 5 TA indicators the HMM uses (confirmed by operator 2026-07-24).
HMM_TA_INDICATORS: list[str] = ["adx", "ao", "bb_power", "macd", "rsi"]


# ─── Local computation from batch OHLCV (no per-symbol HTTP) ──────────────


async def fetch_and_update(
    symbols: list[dict[str, Any]],
    buffer: FeatureBuffer,
    closes_by_symbol: dict[str, list[float]] | None = None,
) -> int:
    """Compute the 5 TA indicators locally from the batch closes and push.

    Called from the sweep cron tick right after /api/price/batch has
    produced `valid_prices`. This is a pure in-process computation — no
    HTTP calls. Each symbol's 5 TA values are combined with the latest
    microstructure + log_return (carried forward from the buffer's previous
    bar) into a full 9-feature vector, then pushed.

    Args:
        symbols: list of symbol config dicts with "symbol".
        buffer: the FeatureBuffer to push into.
        closes_by_symbol: dict of {internal_symbol: closes} — the batch
            OHLCV close prices already fetched this tick. Symbols without
            closes are skipped (0 extra HTTP).

    Returns:
        Number of symbols successfully computed + pushed.
    """
    if not symbols:
        return 0
    if not closes_by_symbol:
        logger.warning("hmm.ta: no batch closes provided — nothing to compute")
        return 0

    pushed = 0
    for sym in symbols:
        symbol = sym["symbol"]
        closes = closes_by_symbol.get(symbol)
        if not closes or len(closes) < 2:
            logger.debug("hmm.ta: %s — no batch closes, skipping", symbol)
            continue

        ta = _compute_ta_indicators_local(np.asarray(closes, dtype=float))

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

        # Features 4-8: latest TA indicator values (computed locally)
        for idx, key in enumerate(HMM_TA_INDICATORS):
            arr = ta.get(key)
            if arr is not None and len(arr) > 0:
                vector[4 + idx] = float(arr[-1]) or 0.0

        buffer.push(symbol, vector)
        pushed += 1

    logger.info("hmm.ta: fetched %d/%d symbols", pushed, len(symbols))
    return pushed
