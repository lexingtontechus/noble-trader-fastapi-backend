"""
Extract TA indicator values from the HMM FeatureBuffer for Supabase persistence.

The HMM FeatureBuffer stores 9-feature vectors per symbol per bar. Features
4-8 are the TA indicators (ADX, AO, BB Power, MACD, RSI) — see
feature_buffer.py for the index map.

This module provides a single function, ``latest_ta``, that returns the
latest TA values for a symbol as a dict suitable for direct injection into
a Supabase insert payload. Returns None for every indicator if the buffer
is empty or the latest vector is malformed — callers should treat the
result as best-effort.

Kept as a separate module (rather than a method on FeatureBuffer) so the
Supabase-facing code can import it without pulling the heavier HmmModel +
feature_buffer dependencies transitively.
"""

from __future__ import annotations

from typing import Any

from regime_platform.services.hmm.feature_buffer import FeatureBuffer

# TA indicator column names as they appear in Supabase (nt_sweep_result
# and nt_regime_log). Order MUST match feature_buffer.FEATURE_NAMES[4:9].
TA_COLUMNS: list[str] = ["ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi"]


def latest_ta(buffer: FeatureBuffer, symbol: str) -> dict[str, float | None]:
    """Return the latest TA values for ``symbol`` as a Supabase-shaped dict.

    Args:
        buffer: the FeatureBuffer instance the HMM is using.
        symbol: the symbol key (matches what was passed to buffer.push()).

    Returns:
        A dict with keys ``ta_adx, ta_ao, ta_bb_power, ta_macd, ta_rsi``.
        Each value is a float, or None if the buffer has no data for the
        symbol (or the latest vector is the wrong shape). The dict always
        contains all 5 keys — callers can spread it directly into a
        Supabase insert payload with ``**latest_ta(...)``.
    """
    empty: dict[str, float | None] = {k: None for k in TA_COLUMNS}

    latest = buffer.latest(symbol)
    if latest is None:
        return empty

    # FeatureBuffer.push() validates shape (N_FEATURES,) at push time, so
    # if latest is non-None the shape is guaranteed. Defensive check anyway
    # — a corrupted buffer shouldn't crash the Supabase write.
    if latest.shape[0] < 9:
        return empty

    return {
        "ta_adx": float(latest[4]),
        "ta_ao": float(latest[5]),
        "ta_bb_power": float(latest[6]),
        "ta_macd": float(latest[7]),
        "ta_rsi": float(latest[8]),
    }


def latest_ta_or_zero(buffer: FeatureBuffer, symbol: str) -> dict[str, float]:
    """Same as ``latest_ta`` but never returns None — missing values are 0.0.

    Use this variant when the destination column is NOT NULL or when the
    downstream consumer can't tolerate None (e.g. a strictly-typed
    numeric field). The Supabase ta_* columns ARE nullable, so the
    regular ``latest_ta`` is preferred for sweep writes — but this is
    kept for tests + future NOT NULL columns.
    """
    return {k: (v if v is not None else 0.0) for k, v in latest_ta(buffer, symbol).items()}
