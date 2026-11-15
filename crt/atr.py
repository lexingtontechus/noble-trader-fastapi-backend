"""atr.py — Wilder ATR with MT4 iATR seeding (CRT-1).

The EA/research compute ATR via MQL4 ``iATR(sym, tf, 14, s)``. MT4's iATR
seeds the series with the SMA of the first ``period`` true ranges, then
applies Wilder's recursion — this module reproduces THAT convention (it
differs from ``regime_edge/features.py::atr_wilder`` only in the seed
transient, which decays as ((period-1)/period)^i and is dead well before any
evaluated setup: setups need >= period bars and the monitor stream sits
hundreds of bars into the fetched window).
"""
from __future__ import annotations

import numpy as np


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """TR[0] = high-low (no previous close); then max of the three ranges."""
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    if close.size == 0:
        return np.asarray([], float)
    prev = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr_mt4(high: np.ndarray, low: np.ndarray, close: np.ndarray,
            period: int = 14) -> np.ndarray:
    """ATR series, MT4 iATR parity: NaN before the SMA seed, Wilder after.

    atr[i] is NaN for i < period-1; atr[period-1] = mean(TR[0..period-1]);
    atr[i] = (atr[i-1]*(period-1) + TR[i]) / period afterwards.
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    n = close.size
    atr = np.full(n, np.nan)
    if n < period or period < 1:
        return atr
    tr = true_range(high, low, close)
    atr[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def atr_percentile_at(atr: np.ndarray, i: int, lookback: int = 500,
                      min_samples: int = 50) -> float:
    """ATRPctAt port: percentile of atr[i] among the previous ``lookback`` ATRs.

    Returns -1.0 (unknown) when fewer than ``min_samples`` comparable ATRs
    exist before index i — the engine maps unknown to the mid vol bit (2),
    exactly like the EA's ``VolBit`` and the optimizer's ``vol_bit``.
    """
    atr = np.asarray(atr, float)
    if i < 0 or i >= atr.size or not np.isfinite(atr[i]) or atr[i] <= 0:
        return -1.0
    lo = max(0, i - lookback)
    window = atr[lo:i]
    window = window[np.isfinite(window) & (window > 0)]
    if window.size < min_samples:
        return -1.0
    return float(np.count_nonzero(window < atr[i])) / float(window.size)
