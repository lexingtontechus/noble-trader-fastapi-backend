"""
Feature Buffer — in-memory rolling-window per-symbol feature store for the HMM.

Stores aligned multivariate feature vectors per symbol. The HMM fitter reads
from this buffer. On startup, the buffer is bootstrapped from /api/price/batch
(log_return + 5 TA indicators computed locally from OHLCV). Live /api/quote
and /api/ta polls append new vectors.

Feature vector (9 features, order matters — the HMM model depends on this):
  0. log_return           — ln(close / prev_close)
  1. effective_spread_bps — (ask - bid) / mid * 10000
  2. imbalance_I          — (bid_size - ask_size) / (bid_size + ask_size)
  3. volume_persistence   — volume / average_volume
  4. adx                  — Average Directional Index (TA)
  5. ao                   — Awesome Oscillator (TA)
  6. bb_power             — Bollinger Band Power (TA)
  7. macd                 — MACD line (TA)
  8. rsi                  — Relative Strength Index (TA)

Features 1-3 come from /api/quote (microstructure).
Features 4-8 come from /api/ta/indicators.
Feature 0 comes from /api/price/batch (already fetched by the sweep cron).

During bootstrap, features 1-3 are 0.0 (not yet available). The HMM fits on
whatever is in the buffer — as live microstructure data arrives, the HMM
refits and incorporates them.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np


# Feature indices — must match the order above.
FEATURE_NAMES: list[str] = [
    "log_return",
    "effective_spread_bps",
    "imbalance_I",
    "volume_persistence",
    "adx",
    "ao",
    "bb_power",
    "macd",
    "rsi",
]
N_FEATURES: int = len(FEATURE_NAMES)

# Default rolling window size. Matches the /api/price/batch return (~300 bars).
DEFAULT_WINDOW: int = 300


@dataclass(slots=True)
class _SymbolBuffer:
    """Per-symbol rolling buffer of feature vectors."""

    window: int
    _buf: deque = field(default_factory=deque)

    def push(self, vector: np.ndarray) -> None:
        """Append a feature vector. Automatically trims to window size."""
        self._buf.append(vector.copy())
        while len(self._buf) > self.window:
            self._buf.popleft()

    def matrix(self) -> np.ndarray | None:
        """Return the buffer as a (n_bars, N_FEATURES) matrix, or None if empty."""
        if len(self._buf) < 2:
            return None
        return np.array(list(self._buf), dtype=float)

    def latest(self) -> np.ndarray | None:
        """Return the most recent feature vector, or None if empty."""
        if not self._buf:
            return None
        return self._buf[-1]

    @property
    def n_bars(self) -> int:
        return len(self._buf)


class FeatureBuffer:
    """Thread-safe per-symbol rolling feature buffer.

    Usage:
        buf = FeatureBuffer(window=300)
        buf.push("BTCUSD", vector_9d)
        mat = buf.matrix("BTCUSD")  # (n_bars, 9) or None
    """

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self.window = window
        self._buffers: dict[str, _SymbolBuffer] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, symbol: str) -> _SymbolBuffer:
        buf = self._buffers.get(symbol)
        if buf is None:
            buf = _SymbolBuffer(window=self.window)
            self._buffers[symbol] = buf
        return buf

    def push(self, symbol: str, vector: np.ndarray) -> None:
        """Push a feature vector for a symbol.

        The vector must have shape (N_FEATURES,). Missing features should be
        0.0 (the HMM handles zeros naturally — they don't bias the Gaussian
        fit as long as they're a small fraction of the window).
        """
        if vector.shape != (N_FEATURES,):
            raise ValueError(
                f"Feature vector shape {vector.shape} != ({N_FEATURES},)"
            )
        with self._lock:
            self._get_or_create(symbol).push(vector)

    def matrix(self, symbol: str) -> np.ndarray | None:
        """Return the (n_bars, N_FEATURES) matrix for a symbol, or None."""
        with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None:
                return None
            return buf.matrix()

    def latest(self, symbol: str) -> np.ndarray | None:
        """Return the latest feature vector for a symbol, or None."""
        with self._lock:
            buf = self._buffers.get(symbol)
            if buf is None:
                return None
            return buf.latest()

    def n_bars(self, symbol: str) -> int:
        """Return the number of bars stored for a symbol."""
        with self._lock:
            buf = self._buffers.get(symbol)
            return buf.n_bars if buf is not None else 0

    def symbols(self) -> list[str]:
        """Return all symbols that have data in the buffer."""
        with self._lock:
            return list(self._buffers.keys())

    def clear(self, symbol: str | None = None) -> None:
        """Clear one symbol's buffer (or all if symbol is None)."""
        with self._lock:
            if symbol is None:
                self._buffers.clear()
            else:
                self._buffers.pop(symbol, None)
