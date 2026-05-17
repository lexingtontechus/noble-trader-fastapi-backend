"""
Swing Classifier — labels each Renko brick with market-structure tags
(HH, HL, LH, LL) and detects swing points (local pivots).

On Renko charts, classification is unambiguous because every brick
has the same size — no wicks, no doji, no interpretation debates.

Swing Label Definitions:
    HH (Higher High) : brick.high  > prev.high  AND brick.low  > prev.low
    HL (Higher Low)  : brick.low   > prev.low   AND brick.high <= prev.high
    LH (Lower High)  : brick.high  < prev.high  AND brick.low  >= prev.low
    LL (Lower Low)   : brick.high  < prev.high  AND brick.low  < prev.low

Swing Point Detection:
    A swing high occurs when a run of HH/HL bricks is followed by
    at least `swing_lookback` LH/LL bricks.
    A swing low occurs when a run of LL/LH bricks is followed by
    at least `swing_lookback` HH/HL bricks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional

from collections import deque

from renko.brick_engine import Brick, BrickDirection


# ── Data types ─────────────────────────────────────────────────────


class SwingLabel(str, Enum):
    """Market-structure classification for a single brick."""
    HH = "HH"  # Higher High — bullish continuation
    HL = "HL"  # Higher Low — bullish consolidation / pullback
    LH = "LH"  # Lower High — bearish consolidation / pullback
    LL = "LL"  # Lower Low — bearish continuation


class SwingType(str, Enum):
    """Swing point type."""
    HIGH = "swing_high"
    LOW = "swing_low"


@dataclass
class SwingPoint:
    """A detected swing point (local pivot)."""

    brick_index: int
    swing_type: SwingType
    price: float          # The high (for swing high) or low (for swing low)
    timestamp: float
    label: SwingLabel     # Label of the brick at the swing

    def to_dict(self) -> dict:
        return {
            "brick_index": self.brick_index,
            "swing_type": self.swing_type.value,
            "price": self.price,
            "timestamp": self.timestamp,
            "label": self.label.value,
        }


@dataclass
class ClassifiedBrick:
    """A brick with its swing label and optional swing point."""

    brick: Brick
    label: SwingLabel
    swing_point: Optional[SwingPoint] = None

    @property
    def is_bullish_label(self) -> bool:
        return self.label in (SwingLabel.HH, SwingLabel.HL)

    @property
    def is_bearish_label(self) -> bool:
        return self.label in (SwingLabel.LH, SwingLabel.LL)

    def to_dict(self) -> dict:
        d = {
            "brick": self.brick.to_dict(),
            "label": self.label.value,
        }
        if self.swing_point:
            d["swing_point"] = self.swing_point.to_dict()
        return d


# ── Classifier ─────────────────────────────────────────────────────


class SwingClassifier:
    """
    Classifies Renko bricks into HH/HL/LH/LL and detects swing points.

    Usage::

        classifier = SwingClassifier(swing_lookback=3)
        classified = classifier.classify(brick)
        if classified.swing_point:
            print(f"Swing {classified.swing_point.swing_type} detected!")
    """

    def __init__(
        self,
        swing_lookback: int = 3,
        min_swing_distance_bricks: int = 2,
    ):
        self.swing_lookback = swing_lookback
        self.min_swing_distance_bricks = min_swing_distance_bricks

        self.classified_history: Deque[ClassifiedBrick] = deque(maxlen=10_000)
        self.swing_points: List[SwingPoint] = []
        self._last_swing_high_idx: int = -999
        self._last_swing_low_idx: int = -999

    # ── Public API ────────────────────────────────────────────────

    def classify(self, brick: Brick) -> ClassifiedBrick:
        """
        Classify a single brick and check for swing points.

        Parameters
        ----------
        brick : Brick
            The latest brick from the engine.

        Returns
        -------
        ClassifiedBrick
            Brick with label and optional swing point.
        """
        label = self._compute_label(brick)
        classified = ClassifiedBrick(brick=brick, label=label)

        # Check for swing points
        swing = self._detect_swing_point(classified)
        if swing is not None:
            classified.swing_point = swing
            self.swing_points.append(swing)

        self.classified_history.append(classified)
        return classified

    def classify_batch(self, bricks: List[Brick]) -> List[ClassifiedBrick]:
        """Classify a batch of bricks (useful for backtest warm-up)."""
        results = []
        for b in bricks:
            results.append(self.classify(b))
        return results

    @property
    def last_classified(self) -> Optional[ClassifiedBrick]:
        return self.classified_history[-1] if self.classified_history else None

    @property
    def last_label(self) -> Optional[SwingLabel]:
        c = self.last_classified
        return c.label if c else None

    @property
    def recent_labels(self, n: int = 10) -> List[SwingLabel]:
        """Get the last N swing labels."""
        history = list(self.classified_history)
        return [cb.label for cb in history[-n:]]

    def get_brick_velocity(self, window_seconds: float = 60.0) -> float:
        """
        Calculate brick velocity (bricks per minute) over recent window.

        Useful as a signal-strength enhancer — fast brick formation
        indicates strong momentum.
        """
        if len(self.classified_history) < 2:
            return 0.0

        now = self.classified_history[-1].brick.timestamp
        cutoff = now - window_seconds

        count = 0
        for cb in reversed(self.classified_history):
            if cb.brick.timestamp < cutoff:
                break
            count += 1

        minutes = window_seconds / 60.0
        return count / minutes if minutes > 0 else 0.0

    # ── Internal ──────────────────────────────────────────────────

    def _compute_label(self, brick: Brick) -> SwingLabel:
        """
        Compute HH/HL/LH/LL label for a brick relative to its predecessor.

        On Renko, we compare the current brick's high and low to the
        previous brick's high and low. Because all bricks are the same
        size, this gives us clean, unambiguous labels.
        """
        if not self.classified_history:
            # First brick: use direction as initial label
            if brick.direction == BrickDirection.UP:
                return SwingLabel.HH
            else:
                return SwingLabel.LL

        prev = self.classified_history[-1].brick

        cur_high = brick.close_price if brick.is_bull else brick.open_price
        cur_low = brick.open_price if brick.is_bull else brick.close_price
        prev_high = prev.close_price if prev.is_bull else prev.open_price
        prev_low = prev.open_price if prev.is_bull else prev.close_price

        # For Renko, use the directional close/open as high/low
        # (since each brick is exactly 1 brick_size tall)
        if brick.is_bull:
            cur_high = brick.close_price
            cur_low = brick.open_price
        else:
            cur_high = brick.open_price
            cur_low = brick.close_price

        if prev.is_bull:
            prev_high = prev.close_price
            prev_low = prev.open_price
        else:
            prev_high = prev.open_price
            prev_low = prev.close_price

        # HH: both high and low higher than previous
        if cur_high > prev_high and cur_low > prev_low:
            return SwingLabel.HH
        # LL: both high and low lower than previous
        elif cur_high < prev_high and cur_low < prev_low:
            return SwingLabel.LL
        # HL: low is higher, but high is not (pullback in uptrend)
        elif cur_low > prev_low and cur_high <= prev_high:
            return SwingLabel.HL
        # LH: high is lower, but low is not (pullback in downtrend)
        elif cur_high < prev_high and cur_low >= prev_low:
            return SwingLabel.LH
        else:
            # Equal bricks (rare on Renko) — default to same as direction
            return SwingLabel.HH if brick.is_bull else SwingLabel.LL

    def _detect_swing_point(self, classified: ClassifiedBrick) -> Optional[SwingPoint]:
        """
        Detect swing points by looking for label transitions.

        A swing HIGH is detected when:
          - We had a sequence of HH/HL bricks (bullish structure)
          - Followed by at least `swing_lookback` LH/LL bricks
          - The last HH/HL brick before the transition is the swing high

        A swing LOW is detected when:
          - We had a sequence of LL/LH bricks (bearish structure)
          - Followed by at least `swing_lookback` HH/HL bricks
          - The last LL/LH brick before the transition is the swing low
        """
        history = list(self.classified_history)
        if len(history) < self.swing_lookback + 1:
            return None

        # Look at the last few bricks to detect a transition
        current_label = classified.label
        prev_labels = [cb.label for cb in history[-(self.swing_lookback):]]

        # Check for swing HIGH: transition from bullish to bearish
        if current_label in (SwingLabel.LH, SwingLabel.LL):
            bullish_run = all(l in (SwingLabel.HH, SwingLabel.HL) for l in prev_labels)
            if bullish_run:
                # Find the highest point in the bullish run
                peak_brick = max(
                    history[-self.swing_lookback:],
                    key=lambda cb: cb.brick.close_price if cb.brick.is_bull else cb.brick.open_price,
                )
                # Enforce minimum distance between swing points
                if peak_brick.brick.index - self._last_swing_high_idx >= self.min_swing_distance_bricks:
                    self._last_swing_high_idx = peak_brick.brick.index
                    return SwingPoint(
                        brick_index=peak_brick.brick.index,
                        swing_type=SwingType.HIGH,
                        price=peak_brick.brick.close_price if peak_brick.brick.is_bull else peak_brick.brick.open_price,
                        timestamp=peak_brick.brick.timestamp,
                        label=peak_brick.label,
                    )

        # Check for swing LOW: transition from bearish to bullish
        if current_label in (SwingLabel.HH, SwingLabel.HL):
            bearish_run = all(l in (SwingLabel.LL, SwingLabel.LH) for l in prev_labels)
            if bearish_run:
                trough_brick = min(
                    history[-self.swing_lookback:],
                    key=lambda cb: cb.brick.close_price if cb.brick.is_bear else cb.brick.open_price,
                )
                if trough_brick.brick.index - self._last_swing_low_idx >= self.min_swing_distance_bricks:
                    self._last_swing_low_idx = trough_brick.brick.index
                    return SwingPoint(
                        brick_index=trough_brick.brick.index,
                        swing_type=SwingType.LOW,
                        price=trough_brick.brick.close_price if trough_brick.brick.is_bear else trough_brick.brick.open_price,
                        timestamp=trough_brick.brick.timestamp,
                        label=trough_brick.label,
                    )

        return None

    def reset(self) -> None:
        """Reset classifier state."""
        self.classified_history.clear()
        self.swing_points.clear()
        self._last_swing_high_idx = -999
        self._last_swing_low_idx = -999
