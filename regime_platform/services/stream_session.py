"""
StreamSession — per-symbol stateful streaming engine.

Responsibilities:
  • Maintains a rolling price deque (configurable window)
  • Holds a fitted RegimeHMM that is periodically re-fit in the background
  • On each new price tick: runs incremental predict() and returns a StreamTick
  • Tracks regime transitions and emits alerts on state changes
  • Thread-safe via asyncio.Lock for concurrent WebSocket clients
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Awaitable
import numpy as np

from ..core.regime_engine import RegimeHMM, RegimeSnapshot
from ..core.position_sizer import PositionSizer, PositionRequest
from ..core.risk_manager import RiskManager


# ─── Tick output ─────────────────────────────────────────────────────────────

@dataclass
class StreamTick:
    """Full regime+sizing+risk snapshot emitted on every price update."""
    symbol:             str
    ts:                 float          # unix timestamp
    price:             float
    n_bars:            int
    # Regime
    regime_label:       str
    vol_state:          str
    trend_state:        str
    vol_probs:          list[float]
    trend_probs:        list[float]
    confidence:         float
    risk_multiplier:    float
    # Sizing
    recommended_f:      float
    sharpe_ratio:       float
    # Risk
    var_95:             float
    cvar_95:            float
    suggested_stop:     float
    suggested_tp:       float
    # Meta
    regime_changed:     bool
    alert:              Optional[str]
    refit_count:        int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegimeAlert:
    symbol:   str
    ts:       float
    previous: str
    current:  str
    message:  str
    severity: str   # "info" | "warning" | "critical"


# ─── Session ─────────────────────────────────────────────────────────────────

class StreamSession:
    """
    Stateful streaming session for one symbol.

    Lifecycle:
      1. Seed with ≥51 historical prices  →  initial HMM fit
      2. tick(price)                       →  returns StreamTick in ~1ms
      3. Every `refit_every` bars          →  background HMM re-fit
      4. On regime change                  →  alert callbacks fired

    Configuration:
      All params (window, kelly_fraction, target_vol, base_risk_limit,
      refit_every, regime_debounce) are configurable via from_runtime_config()
      which resolves from the DB → env → hardcoded default chain.
    """

    # Hardcoded fallback — overridden by runtime_config in from_runtime_config()
    _DEFAULT_MIN_PRICES_FOR_FIT = 81
    _DEFAULT_SUBSCRIBER_QUEUE_SIZE = 500

    def __init__(
        self,
        symbol: str,
        window: int = 500,
        kelly_fraction: float = 0.5,
        target_vol: float = 0.15,
        base_risk_limit: float = 0.02,
        refit_every: int = 50,
        regime_debounce: int = 3,
        min_prices_for_fit: int = 81,
        subscriber_queue_size: int = 500,
    ):
        self.symbol          = symbol
        self.window          = window
        self.kelly_fraction  = kelly_fraction
        self.target_vol      = target_vol
        self.base_risk_limit = base_risk_limit
        self.refit_every     = refit_every
        self.regime_debounce = regime_debounce
        self.min_prices_for_fit = min_prices_for_fit
        self.subscriber_queue_size = subscriber_queue_size

        self._prices:  deque[float]  = deque(maxlen=window)
        self._model:   Optional[RegimeHMM] = None
        self._sizer    = PositionSizer()
        self._riskman  = RiskManager()
        self._lock     = asyncio.Lock()

        self._last_regime:   str   = ""
        self._refit_count:   int   = 0
        self._bars_since_refit: int = 0
        self._tick_count:    int   = 0
        self._alert_callbacks: list[Callable[[RegimeAlert], Awaitable[None]]] = []
        self._subscribers:     list[asyncio.Queue] = []

    @classmethod
    async def from_runtime_config(cls, symbol: str) -> "StreamSession":
        """
        Create a StreamSession with params from the runtime configuration system.

        Resolution: DB override → env var → hardcoded default.
        """
        from ..core.runtime_config import runtime_config

        window = await runtime_config.get_int("stream.window", default=500)
        kelly_fraction = await runtime_config.get_float("stream.kelly_fraction", default=0.5)
        target_vol = await runtime_config.get_float("stream.target_vol", default=0.15)
        base_risk_limit = await runtime_config.get_float("stream.base_risk_limit", default=0.02)
        refit_every = await runtime_config.get_int("stream.refit_every", default=50)
        regime_debounce = await runtime_config.get_int("stream.regime_debounce", default=3)
        min_prices_for_fit = await runtime_config.get_int("stream.min_prices_for_fit", default=cls._DEFAULT_MIN_PRICES_FOR_FIT)
        subscriber_queue_size = await runtime_config.get_int("stream.subscriber_queue_size", default=cls._DEFAULT_SUBSCRIBER_QUEUE_SIZE)

        return cls(
            symbol=symbol,
            window=window,
            kelly_fraction=kelly_fraction,
            target_vol=target_vol,
            base_risk_limit=base_risk_limit,
            refit_every=refit_every,
            regime_debounce=regime_debounce,
            min_prices_for_fit=min_prices_for_fit,
            subscriber_queue_size=subscriber_queue_size,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def seed(self, prices: list[float]) -> None:
        """Load historical prices and perform initial HMM fit."""
        async with self._lock:
            for p in prices[-self.window:]:
                self._prices.append(p)
            if len(self._prices) >= self.min_prices_for_fit:
                await asyncio.get_event_loop().run_in_executor(None, self._refit)

    async def tick(self, price: float, ts: float | None = None) -> StreamTick | None:
        """
        Ingest a new price tick. Returns a StreamTick if there are enough
        bars to produce a regime estimate, else None.
        """
        async with self._lock:
            self._prices.append(price)
            self._tick_count += 1
            self._bars_since_refit += 1

            n = len(self._prices)
            if n < self.min_prices_for_fit:
                return None

            # Periodic refit in executor (non-blocking)
            if self._model is None or self._bars_since_refit >= self.refit_every:
                await asyncio.get_event_loop().run_in_executor(None, self._refit)
                self._bars_since_refit = 0

            return self._compute_tick(price, ts or time.time())

    def subscribe(self) -> asyncio.Queue:
        """Return a Queue that receives every StreamTick for this symbol."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.subscriber_queue_size)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q) if hasattr(self._subscribers, 'discard') else None
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def add_alert_callback(self, cb: Callable[[RegimeAlert], Awaitable[None]]) -> None:
        self._alert_callbacks.append(cb)

    @property
    def price_buffer(self) -> list[float]:
        return list(self._prices)

    @property
    def is_ready(self) -> bool:
        return self._model is not None and self._model.fitted

    # ── Internal ──────────────────────────────────────────────────────────────

    def _refit(self) -> None:
        """Blocking HMM refit — always called via run_in_executor."""
        prices = np.array(list(self._prices), dtype=float)
        prices = prices[np.isfinite(prices) & (prices > 0)]
        if len(prices) < self.min_prices_for_fit:
            return
        model = RegimeHMM()
        model.fit(prices)
        self._model = model
        self._refit_count += 1

    def _compute_tick(self, price: float, ts: float) -> StreamTick:
        prices  = np.array(list(self._prices), dtype=float)
        prices  = prices[np.isfinite(prices) & (prices > 0)]
        if len(prices) < self.min_prices_for_fit:
            return None
        returns = np.diff(np.log(prices))
        returns = np.where(np.isfinite(returns), returns, 0.0)

        snap = self._model.predict(prices)

        size_result = self._sizer.size(PositionRequest(
            returns=returns.tolist(),
            kelly_fraction=self.kelly_fraction,
            target_vol=self.target_vol,
            regime=snap,
        ))

        risk = self._riskman.analyse(
            returns=returns,
            recommended_f=size_result.recommended_f,
            regime=snap,
            base_risk_limit=self.base_risk_limit,
        )

        # Debounce: track consecutive bars in new regime before firing alert
        if snap.regime_label != self._last_regime:
            self._regime_candidate = snap.regime_label
            self._candidate_count  = getattr(self, '_candidate_count', 0) + 1
        else:
            self._candidate_count = 0
            self._regime_candidate = snap.regime_label

        DEBOUNCE_BARS = self.regime_debounce
        regime_changed = (
            self._candidate_count == DEBOUNCE_BARS
            and self._last_regime != ""
            and self._regime_candidate != self._last_regime
        )
        alert_msg      = None

        if regime_changed:
            alert_msg = (
                f"⚡ REGIME CHANGE [{self.symbol}]: "
                f"{self._last_regime} → {snap.regime_label} "
                f"| risk_mult={snap.risk_multiplier:.2f}× "
                f"| recommended_f={size_result.recommended_f:.4f}"
            )
            # Fire alert callbacks (schedule as tasks — we're in a sync context)
            alert = RegimeAlert(
                symbol=self.symbol,
                ts=ts,
                previous=self._last_regime,
                current=snap.regime_label,
                message=alert_msg,
                severity=self._alert_severity(self._last_regime, snap.regime_label),
            )
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda a=alert: asyncio.ensure_future(self._fire_alerts(a))
            )

        if regime_changed:
            self._last_regime = self._regime_candidate
        elif self._candidate_count == 0:
            self._last_regime = snap.regime_label

        tick = StreamTick(
            symbol=self.symbol,
            ts=ts,
            price=price,
            n_bars=len(prices),
            regime_label=snap.regime_label,
            vol_state=snap.vol_state,
            trend_state=snap.trend_state,
            vol_probs=snap.vol_probs,
            trend_probs=snap.trend_probs,
            confidence=snap.confidence,
            risk_multiplier=snap.risk_multiplier,
            recommended_f=size_result.recommended_f,
            sharpe_ratio=size_result.sharpe_ratio,
            var_95=risk.var_95,
            cvar_95=risk.cvar_95,
            suggested_stop=risk.suggested_stop,
            suggested_tp=risk.suggested_tp,
            regime_changed=regime_changed,
            alert=alert_msg,
            refit_count=self._refit_count,
        )

        # Push to all subscriber queues (non-blocking)
        for q in self._subscribers:
            try:
                q.put_nowait(tick)
            except asyncio.QueueFull:
                pass  # slow consumer — drop tick

        return tick

    @staticmethod
    def _alert_severity(prev: str, curr: str) -> str:
        high_risk = {"high_vol_bear", "high_vol_neutral"}
        if curr in high_risk:
            return "critical"
        if "bear" in curr or "high" in curr:
            return "warning"
        return "info"

    async def _fire_alerts(self, alert: RegimeAlert) -> None:
        for cb in self._alert_callbacks:
            try:
                await cb(alert)
            except Exception:
                pass
