"""
Renko Brick Engine — the foundation of the HFT pipeline.

Converts a real-time tick stream into Renko brick events.
Uses the classic 2-brick reversal rule: a direction change
requires price to move 2× brick_size against the current trend
before a reversal brick is emitted.

State machine:
    - Tracks current direction (UP / DOWN / FLAT)
    - Accumulates price movement until a brick threshold is crossed
    - Emits Brick events which feed into the Swing Classifier

Thread-safe: designed for async tick ingestion via `process_tick()`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, List, Optional

from collections import deque


# ── Data types ─────────────────────────────────────────────────────


class BrickDirection(str, Enum):
    UP = "up"
    DOWN = "down"


@dataclass
class Brick:
    """A single Renko brick."""

    index: int                # Sequential brick number (0-based)
    direction: BrickDirection
    open_price: float
    close_price: float
    high: float
    low: float
    timestamp: float          # Unix epoch (seconds)
    brick_size: float

    @property
    def is_bull(self) -> bool:
        return self.direction == BrickDirection.UP

    @property
    def is_bear(self) -> bool:
        return self.direction == BrickDirection.DOWN

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "direction": self.direction.value,
            "open_price": self.open_price,
            "close_price": self.close_price,
            "high": self.high,
            "low": self.low,
            "timestamp": self.timestamp,
            "brick_size": self.brick_size,
        }


@dataclass
class _EngineState:
    """Internal mutable state of the brick engine."""

    direction: Optional[BrickDirection] = None
    last_brick_high: float = 0.0
    last_brick_low: float = 0.0
    brick_count: int = 0
    pending_high: float = 0.0
    pending_low: float = 0.0


# ── Engine ─────────────────────────────────────────────────────────


class RenkoBrickEngine:
    """
    Stateless-in, event-out Renko brick constructor.

    Usage::

        engine = RenkoBrickEngine(brick_size=0.50)
        new_bricks = engine.process_tick(price=150.25, ts=epoch)

    The engine may emit 0 or more bricks per tick (e.g. a large gap
    generates multiple bricks).  All emitted bricks are also appended
    to ``self.history`` for downstream consumers.
    """

    def __init__(
        self,
        brick_size: float = 0.50,
        reversal_bricks: int = 2,
        on_brick: Optional[Callable[[Brick], None]] = None,
    ):
        self.brick_size = brick_size
        self.reversal_bricks = reversal_bricks
        self.on_brick = on_brick  # optional callback per brick

        self._state = _EngineState()
        self.history: Deque[Brick] = deque(maxlen=10_000)
        self._initialized = False

    # ── Public API ────────────────────────────────────────────────

    def process_tick(self, price: float, ts: Optional[float] = None) -> List[Brick]:
        """
        Ingest one tick price and return any new bricks.

        Parameters
        ----------
        price : float
            Latest trade price.
        ts : float, optional
            Unix timestamp. Defaults to ``time.time()``.

        Returns
        -------
        list[Brick]
            Zero or more new bricks created by this tick.
        """
        if ts is None:
            ts = time.time()

        # First tick: seed state without emitting
        if not self._initialized:
            self._seed(price, ts)
            return []

        return self._evaluate(price, ts)

    def reset(self) -> None:
        """Reset engine state (e.g. on session start)."""
        self._state = _EngineState()
        self.history.clear()
        self._initialized = False

    @property
    def current_direction(self) -> Optional[BrickDirection]:
        return self._state.direction

    @property
    def brick_count(self) -> int:
        return self._state.brick_count

    @property
    def last_brick(self) -> Optional[Brick]:
        return self.history[-1] if self.history else None

    # ── Internal ──────────────────────────────────────────────────

    def _seed(self, price: float, ts: float) -> None:
        """Initialise engine on first tick — set reference levels."""
        self._state.direction = None
        bs = self.brick_size
        # Round reference levels to brick grid
        self._state.last_brick_high = self._align_up(price, bs)
        self._state.last_brick_low = self._align_down(price, bs)
        self._state.pending_high = self._state.last_brick_high
        self._state.pending_low = self._state.last_brick_low
        self._initialized = True

    def _evaluate(self, price: float, ts: float) -> List[Brick]:
        """Core brick construction logic — classic Renko with 2-brick reversal."""
        new_bricks: List[Brick] = []
        bs = self.brick_size
        st = self._state

        # Keep generating bricks while price exceeds thresholds
        max_bricks_per_tick = 50  # safety cap for extreme gaps
        iterations = 0

        while iterations < max_bricks_per_tick:
            iterations += 1

            if st.direction is None or st.direction == BrickDirection.UP:
                brick = self._try_up_bricks(price, ts)
                if brick is None:
                    # Check for reversal
                    if st.direction == BrickDirection.UP:
                        reversal = self._check_down_reversal(price, ts)
                        if reversal is not None:
                            new_bricks.append(reversal)
                            self._emit(reversal)
                            continue  # re-evaluate with new direction
                    break  # no more bricks
                new_bricks.append(brick)
                self._emit(brick)

            elif st.direction == BrickDirection.DOWN:
                brick = self._try_down_bricks(price, ts)
                if brick is None:
                    # Check for reversal
                    reversal = self._check_up_reversal(price, ts)
                    if reversal is not None:
                        new_bricks.append(reversal)
                        self._emit(reversal)
                        continue
                    break
                new_bricks.append(brick)
                self._emit(brick)

        return new_bricks

    def _try_up_bricks(self, price: float, ts: float) -> Optional[Brick]:
        """Try to create an UP brick from current price."""
        st = self._state
        bs = self.brick_size
        next_high = st.last_brick_high + bs

        if price >= next_high:
            brick = Brick(
                index=st.brick_count,
                direction=BrickDirection.UP,
                open_price=st.last_brick_high,
                close_price=next_high,
                high=next_high,
                low=st.last_brick_high,
                timestamp=ts,
                brick_size=bs,
            )
            st.direction = BrickDirection.UP
            st.last_brick_high = next_high
            st.last_brick_low = st.last_brick_high - bs
            st.brick_count += 1
            return brick
        return None

    def _try_down_bricks(self, price: float, ts: float) -> Optional[Brick]:
        """Try to create a DOWN brick from current price."""
        st = self._state
        bs = self.brick_size
        next_low = st.last_brick_low - bs

        if price <= next_low:
            brick = Brick(
                index=st.brick_count,
                direction=BrickDirection.DOWN,
                open_price=st.last_brick_low,
                close_price=next_low,
                high=st.last_brick_low,
                low=next_low,
                timestamp=ts,
                brick_size=bs,
            )
            st.direction = BrickDirection.DOWN
            st.last_brick_high = st.last_brick_low + bs
            st.last_brick_low = next_low
            st.brick_count += 1
            return brick
        return None

    def _check_up_reversal(self, price: float, ts: float) -> Optional[Brick]:
        """Check if price triggers an UP reversal (from DOWN trend).

        Classic Renko: reversal requires 2× brick_size move.
        """
        st = self._state
        bs = self.brick_size
        reversal_target = st.last_brick_high + bs  # 2 bricks from last_brick_low

        # Already 1 brick away (the high of last down brick is 1 brick up from low)
        # So we need price >= that high + 1 more brick = 2 bricks total
        if price >= st.last_brick_high + bs:
            # Emit the first reversal brick
            new_high = st.last_brick_high + bs
            brick = Brick(
                index=st.brick_count,
                direction=BrickDirection.UP,
                open_price=st.last_brick_high,
                close_price=new_high,
                high=new_high,
                low=st.last_brick_high,
                timestamp=ts,
                brick_size=bs,
            )
            st.direction = BrickDirection.UP
            st.last_brick_high = new_high
            st.last_brick_low = new_high - bs
            st.brick_count += 1
            return brick
        return None

    def _check_down_reversal(self, price: float, ts: float) -> Optional[Brick]:
        """Check if price triggers a DOWN reversal (from UP trend)."""
        st = self._state
        bs = self.brick_size
        # Need 2× brick_size drop from current high
        if price <= st.last_brick_low - bs:
            new_low = st.last_brick_low - bs
            brick = Brick(
                index=st.brick_count,
                direction=BrickDirection.DOWN,
                open_price=st.last_brick_low,
                close_price=new_low,
                high=st.last_brick_low,
                low=new_low,
                timestamp=ts,
                brick_size=bs,
            )
            st.direction = BrickDirection.DOWN
            st.last_brick_high = new_low + bs
            st.last_brick_low = new_low
            st.brick_count += 1
            return brick
        return None

    def _emit(self, brick: Brick) -> None:
        """Record brick in history and fire callback."""
        self.history.append(brick)
        if self.on_brick is not None:
            try:
                self.on_brick(brick)
            except Exception:
                pass  # Don't let callback errors break the engine

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _align_up(price: float, brick_size: float) -> float:
        """Round price UP to nearest brick grid line."""
        import math
        return math.ceil(price / brick_size) * brick_size

    @staticmethod
    def _align_down(price: float, brick_size: float) -> float:
        """Round price DOWN to nearest brick grid line."""
        import math
        return math.floor(price / brick_size) * brick_size

    # ── Batch / Backtest helpers ──────────────────────────────────

    def build_from_prices(
        self,
        prices: List[float],
        timestamps: Optional[List[float]] = None,
    ) -> List[Brick]:
        """
        Build bricks from a list of prices (backtest / warm-up).

        Parameters
        ----------
        prices : list[float]
            Sequential price observations.
        timestamps : list[float], optional
            Matching timestamps. Defaults to auto-incrementing.

        Returns
        -------
        list[Brick]
            All bricks generated from the price series.
        """
        self.reset()
        all_bricks: List[Brick] = []

        for i, price in enumerate(prices):
            ts = timestamps[i] if timestamps else float(i)
            new = self.process_tick(price, ts)
            all_bricks.extend(new)

        return all_bricks
