"""
L2 Order Book Imbalance Computer  [SOFT-DEPRECATED — P3.5 / P7 removal candidate]
═════════════════════════════════════════════════════════════════════════════════

╔════════════════════════════════════════════════════════════════════════════╗
║  ⚠️  P3.5 DEPRECATION NOTICE (2026-07-21)                                  ║
║  ────────────────────────────────────────────────────────────────────────  ║
║  This module computes the L2-derived `p_imbalance` field that fed the     ║
║  EV engine's external_signal parameter.                                   ║
║                                                                            ║
║  P3.5 REPLACES p_imbalance with `p_microstructure` (composite of L1-      ║
║  derived microstructure + TVDA TA), computed in the proxy:                ║
║    noble-trader-proxy/src/proxy/microstructure/                           ║
║                                                                            ║
║  Reasons for replacement:                                                  ║
║    1. L2 coverage gap — Alpaca L2 is crypto-only; HL L2 is crypto-only.   ║
║       No equity/forex/commodity L2 source exists. This created a          ║
║       structural bias: crypto got 4 EV components, non-crypto got 3.      ║
║    2. Single data plane — TVDA SSE L1 is the proxy's only data source.    ║
║       Keeping L2 meant a permanent second data plane (Alpaca WS + HL WS)  ║
║       with timestamp reconciliation overhead.                             ║
║    3. Backtest comparability — p_microstructure is computed identically   ║
║       for all 20 curated symbols, making EV threshold tuning comparable   ║
║       across the basket.                                                  ║
║                                                                            ║
║  Disposition:                                                              ║
║    - This module is PRESERVED (not deleted) for the P3.5 rollout window.  ║
║    - p_imbalance continues to return its existing value for any caller    ║
║      that still references it (the field is "soft-deprecated", not        ║
║      removed).                                                            ║
║    - STALE COMMENT FIX (audit §5 #20, 2026-07-22): the EV engine no       ║
║      longer reads p_imbalance at all. The `external_signal` parameter     ║
║      was REMOVED from compute_ev() in v5 (see ev_engine.py:22). The L2    ║
║      imbalance value this module computes is currently NOT consumed by    ║
║      any live path — it's preserved only for backward-compat replay of   ║
║      historical backtest runs. P3.5 deprecated L2 entirely; the live      ║
║      replacement input is `p_microstructure` from the proxy's             ║
║      /sse/alerts microstructure event stream (HIGH #9 wired the SSE       ║
║      consumer into the synthesizer's MetaRegimeClassifier as the          ║
║      replacement microstructure source).                                  ║
║    - P7 REMOVAL: scheduled for after 30 days of stable SSE operation      ║
║      (target: 2026-08-21). At P7, this file + l2_adapters.py + the L2     ║
║      subscriber paths in the agent (alpaca_adapter.stream_order_book +    ║
║      hyperliquid_adapter.stream_order_book) will be DELETED.              ║
║                                                                            ║
║  Migration path for callers:                                               ║
║    OLD:  signal = l2_imbalance.compute(snap)["imbalance_ratio"]           ║
║    NEW:  micro = await proxy_microstructure_client.get(symbol)            ║
║          p = micro["p_microstructure"]  # range [-1, +1]                  ║
║                                                                            ║
║  See:                                                                     ║
║    - proxy: noble-trader-proxy/src/proxy/microstructure/composite.py      ║
║    - design: P3.5 changelog in /home/z/my-project/worklog.md              ║
╚════════════════════════════════════════════════════════════════════════════╝

Original docstring (preserved for reference):

Converts raw ``L2Snapshot`` order book depth data into the
``{signal, confidence, persistence}`` dict consumed by the
EV Engine's ``external_signal`` parameter.

Two computation modes:
  1. **Multi-level weighted imbalance** (default) — weighted average
     across depth levels with configurable level weights.
  2. **Top-of-book only** — simple bid/ask volume ratio at level 1.

Architecture
────────────
Maintains per-symbol state (ring buffer of recent imbalance readings)
to compute persistence — how directionally stable the signal has been.

Usage
─────
    computer = L2ImbalanceComputer()

    async with AlpacaL2Adapter(["SPY"]) as feed:
        async for snap in feed.stream():
            signal = computer.compute(snap)
            # signal → {"signal": "buy", "confidence": 0.72,
            #            "persistence": 0.85, "imbalance_ratio": 0.31, ...}
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Dict, List, Optional

from regime_platform.adapters.l2_adapters import L2Snapshot
from regime_platform.core.runtime_config import runtime_config

log = logging.getLogger("regime.l2_imbalance")

# ─── Default level weights (top 5 levels) ───────────────────────────
# Level 1 (top of book) gets the most weight; deeper levels taper off.
# These are configurable via runtime_config key "l2.level_weights".
_DEFAULT_LEVEL_WEIGHTS: List[float] = [0.40, 0.25, 0.15, 0.10, 0.10]

# Default threshold: |imbalance| above this triggers buy/sell signal
_DEFAULT_IMBALANCE_THRESHOLD = 0.15

# Ring buffer size for persistence tracking
_PERSISTENCE_WINDOW = 20


class L2ImbalanceComputer:
    """Converts raw L2 order book snapshots → EV engine signal dict.

    Thread-safe when each pipeline has its own instance (recommended).
    For shared access, wrap with ``asyncio.Lock``.

    Parameters
    ----------
    level_weights : list of float, optional
        Weights for each depth level (must sum to 1.0).
        Default: ``[0.40, 0.25, 0.15, 0.10, 0.10]``
    imbalance_threshold : float, optional
        Minimum |imbalance_ratio| to trigger a directional signal.
        Default: 0.15
    persistence_window : int, optional
        Number of recent readings to track for persistence computation.
        Default: 20
    """

    def __init__(
        self,
        level_weights: Optional[List[float]] = None,
        imbalance_threshold: Optional[float] = None,
        persistence_window: int = _PERSISTENCE_WINDOW,
    ):
        self._level_weights = level_weights or list(_DEFAULT_LEVEL_WEIGHTS)
        self._threshold = (
            imbalance_threshold if imbalance_threshold is not None
            else _DEFAULT_IMBALANCE_THRESHOLD
        )
        self._window = persistence_window

        # Validate weights
        if abs(sum(self._level_weights) - 1.0) > 0.01:
            log.warning(
                "L2 level weights sum to %.3f, not 1.0. Normalizing.",
                sum(self._level_weights),
            )
            total = sum(self._level_weights)
            self._level_weights = [w / total for w in self._level_weights]

        # Per-symbol ring buffers of recent imbalance_ratio readings
        self._history: Dict[str, deque] = {}

    # ── Public API ─────────────────────────────────────────────────

    def compute(self, snapshot: L2Snapshot) -> dict:
        """Compute imbalance signal from an L2 order book snapshot.

        Parameters
        ----------
        snapshot : L2Snapshot
            Raw order book depth snapshot from any venue.

        Returns
        -------
        dict
            EV-engine-compatible signal dict with keys:
            ``signal``, ``confidence``, ``persistence``,
            ``imbalance_ratio``, ``bid_volume``, ``ask_volume``,
            ``source``, ``timestamp``.
        """
        # 1. Compute multi-level weighted imbalance
        imbalance = self._weighted_imbalance(snapshot)

        # 2. Determine signal direction
        if imbalance > self._threshold:
            signal = "buy"
        elif imbalance < -self._threshold:
            signal = "sell"
        else:
            signal = "flat"

        # 3. Confidence = sigmoid mapping of |imbalance| to [0, 1]
        #    steepness=5 means imbalance=0.5 → confidence≈0.92
        confidence = 1.0 / (1.0 + math.exp(-5.0 * abs(imbalance)))

        # 4. Persistence = fraction of recent readings with same sign
        persistence = self._compute_persistence(snapshot.symbol, imbalance)

        # 5. Build the signal dict
        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "persistence": round(persistence, 4),
            "imbalance_ratio": round(imbalance, 4),
            "bid_volume": round(snapshot.total_bid_volume, 4),
            "ask_volume": round(snapshot.total_ask_volume, 4),
            "source": snapshot.source,
            "timestamp": snapshot.timestamp,
        }

    def get_latest_signal(self, symbol: str) -> Optional[dict]:
        """Return the most recent signal for a symbol without a new snapshot.

        Returns ``None`` if no data has been computed for this symbol yet.
        """
        if symbol not in self._history or not self._history[symbol]:
            return None
        latest = self._history[symbol][-1]
        # Reconstruct signal from stored ratio
        signal = "buy" if latest > self._threshold else "sell" if latest < -self._threshold else "flat"
        confidence = 1.0 / (1.0 + math.exp(-5.0 * abs(latest)))
        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "imbalance_ratio": round(latest, 4),
            "persistence": 0.0,  # Would need full history; caller should pass snapshot
        }

    def reset(self, symbol: Optional[str] = None) -> None:
        """Clear history for one symbol or all symbols."""
        if symbol:
            self._history.pop(symbol, None)
        else:
            self._history.clear()

    # ── Internal computation ───────────────────────────────────────

    def _weighted_imbalance(self, snapshot: L2Snapshot) -> float:
        """Multi-level weighted imbalance ratio across depth levels.

        Computes: Σ(level_weight[k] * imbalance_at_level_k)
        where imbalance_at_level = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        """
        weighted_sum = 0.0
        levels = min(len(snapshot.bids), len(snapshot.asks), len(self._level_weights))

        for k in range(levels):
            bid_vol = snapshot.bids[k][1]
            ask_vol = snapshot.asks[k][1]
            total = bid_vol + ask_vol
            if total > 0:
                ratio = (bid_vol - ask_vol) / total
                weighted_sum += self._level_weights[k] * ratio

        # If we have fewer levels than weights, renormalize
        if levels < len(self._level_weights) and levels > 0:
            used_weight = sum(self._level_weights[:levels])
            if used_weight > 0:
                weighted_sum /= used_weight

        return weighted_sum

    def _compute_persistence(self, symbol: str, current_ratio: float) -> float:
        """How directionally consistent the last N readings are.

        Returns fraction of readings in the sliding window that share
        the same directional sign as the current reading.
        """
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self._window)

        self._history[symbol].append(current_ratio)
        history = list(self._history[symbol])

        if len(history) < max(5, self._window // 4):
            return 0.0  # Not enough data yet

        # Determine current sign
        sign = (
            1 if current_ratio > self._threshold / 2
            else -1 if current_ratio < -self._threshold / 2
            else 0
        )
        if sign == 0:
            return 0.0  # Neutral — no persistence

        # Count past readings with matching sign
        matching = sum(
            1 for h in history
            if (h > 0 and sign > 0) or (h < 0 and sign < 0)
        )
        return matching / len(history)

    @property
    def stats(self) -> dict:
        """Return per-symbol statistics."""
        return {
            "symbols": list(self._history.keys()),
            "n_observations": {sym: len(h) for sym, h in self._history.items()},
            "level_weights": self._level_weights,
            "threshold": self._threshold,
        }
