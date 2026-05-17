"""
Pattern Detector — identifies tradeable patterns from classified bricks.

Core patterns:
    1. N-Bull Run  : N consecutive HH/HL bricks -> long entry
    2. N-Bear Run  : N consecutive LL/LH bricks -> short entry
    3. Reversal    : After N-bull/bear run, first opposite-class brick
    4. Double Top  : Two swing highs at same brick level
    5. Double Bot  : Two swing lows at same brick level
    6. Consolidation Break : Mixed labels then directional breakout

The detector also considers brick velocity (bricks/minute) as a
signal-strength enhancer for HFT contexts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional

from collections import deque

from renko.brick_engine import Brick, BrickDirection
from renko.swing_classifier import ClassifiedBrick, SwingLabel, SwingPoint, SwingType


# ── Data types ─────────────────────────────────────────────────────


class PatternType(str, Enum):
    BULL_RUN = "bull_run"
    BEAR_RUN = "bear_run"
    REVERSAL = "reversal"
    DOUBLE_TOP = "double_top"
    DOUBLE_BOTTOM = "double_bottom"
    CONSOLIDATION_BREAK = "consolidation_break"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    EXIT = "exit"


@dataclass
class PatternSignal:
    """A trading signal produced by pattern detection."""

    pattern_type: PatternType
    direction: SignalDirection
    strength: float           # 0.0 - 1.0
    brick_index: int
    price: float
    timestamp: float
    label_sequence: List[str]  # The labels that triggered this signal
    velocity: float = 0.0      # Bricks per minute at signal time
    metadata: dict = field(default_factory=dict)

    @property
    def is_long(self) -> bool:
        return self.direction == SignalDirection.LONG

    @property
    def is_short(self) -> bool:
        return self.direction == SignalDirection.SHORT

    def to_dict(self) -> dict:
        return {
            "pattern_type": self.pattern_type.value,
            "direction": self.direction.value,
            "strength": self.strength,
            "brick_index": self.brick_index,
            "price": self.price,
            "timestamp": self.timestamp,
            "label_sequence": self.label_sequence,
            "velocity": self.velocity,
            "metadata": self.metadata,
        }


# ── Detector ───────────────────────────────────────────────────────


class PatternDetector:
    """
    Detects tradeable patterns from a stream of classified bricks.

    Usage::

        detector = PatternDetector(bull_trigger_n=3)
        signal = detector.detect(classified_brick, velocity=2.5)
        if signal:
            # pass to SignalFilter
    """

    def __init__(
        self,
        bull_trigger_n: int = 3,
        bear_trigger_n: int = 3,
        velocity_threshold: float = 0.0,
        double_top_bricks: int = 6,
        consolidation_max_mix: float = 0.4,
    ):
        self.bull_trigger_n = bull_trigger_n
        self.bear_trigger_n = bear_trigger_n
        self.velocity_threshold = velocity_threshold
        self.double_top_bricks = double_top_bricks
        self.consolidation_max_mix = consolidation_max_mix

        self.signals: List[PatternSignal] = []
        self._last_signal_idx: int = -999

        # Internal run counters
        self._bull_run_count: int = 0
        self._bear_run_count: int = 0
        self._prev_label: Optional[SwingLabel] = None
        self._label_sequence: List[str] = []

        # Swing point tracking for double-top/bottom
        self._recent_swing_highs: List[SwingPoint] = []
        self._recent_swing_lows: List[SwingPoint] = []

    # ── Public API ────────────────────────────────────────────────

    def detect(
        self,
        classified: ClassifiedBrick,
        velocity: float = 0.0,
    ) -> Optional[PatternSignal]:
        """
        Evaluate the latest classified brick for pattern matches.

        Parameters
        ----------
        classified : ClassifiedBrick
            Latest classified brick from SwingClassifier.
        velocity : float
            Current brick velocity (bricks/minute).

        Returns
        -------
        PatternSignal or None
            Signal if a pattern is detected, else None.
        """
        # Update run counters
        self._update_run_count(classified)

        # Track swing points for double-top/bottom
        if classified.swing_point is not None:
            if classified.swing_point.swing_type == SwingType.HIGH:
                self._recent_swing_highs.append(classified.swing_point)
                if len(self._recent_swing_highs) > 20:
                    self._recent_swing_highs = self._recent_swing_highs[-20:]
            else:
                self._recent_swing_lows.append(classified.swing_point)
                if len(self._recent_swing_lows) > 20:
                    self._recent_swing_lows = self._recent_swing_lows[-20:]

        signal = None

        # Priority 1: N-bull/bear run (main pattern)
        signal = self._check_run_pattern(classified, velocity)

        # Priority 2: Reversal pattern
        if signal is None:
            signal = self._check_reversal(classified, velocity)

        # Priority 3: Double top / double bottom
        if signal is None:
            signal = self._check_double_pattern(classified, velocity)

        # Priority 4: Consolidation breakout
        if signal is None:
            signal = self._check_consolidation_break(classified, velocity)

        if signal is not None:
            # Avoid duplicate signals on the same brick
            if signal.brick_index == self._last_signal_idx:
                return None
            self._last_signal_idx = signal.brick_index
            self.signals.append(signal)

        return signal

    @property
    def last_signal(self) -> Optional[PatternSignal]:
        return self.signals[-1] if self.signals else None

    # ── Run counter update ────────────────────────────────────────

    def _update_run_count(self, classified: ClassifiedBrick) -> None:
        """Update bullish/bearish run counters based on current label."""
        label = classified.label

        if label in (SwingLabel.HH, SwingLabel.HL):
            if self._prev_label in (SwingLabel.HH, SwingLabel.HL):
                self._bull_run_count += 1
            else:
                self._bull_run_count = 1
            self._bear_run_count = 0
        elif label in (SwingLabel.LL, SwingLabel.LH):
            if self._prev_label in (SwingLabel.LL, SwingLabel.LH):
                self._bear_run_count += 1
            else:
                self._bear_run_count = 1
            self._bull_run_count = 0

        self._prev_label = label
        self._label_sequence.append(label.value)
        if len(self._label_sequence) > 20:
            self._label_sequence = self._label_sequence[-20:]

    # ── Pattern checks ────────────────────────────────────────────

    def _check_run_pattern(
        self,
        classified: ClassifiedBrick,
        velocity: float,
    ) -> Optional[PatternSignal]:
        """Check for N consecutive HH/HL (bull) or LL/LH (bear) bricks."""
        label = classified.label

        # Bull trigger
        if self._bull_run_count >= self.bull_trigger_n and label in (SwingLabel.HH, SwingLabel.HL):
            strength = self._compute_strength(self._bull_run_count, velocity)
            return PatternSignal(
                pattern_type=PatternType.BULL_RUN,
                direction=SignalDirection.LONG,
                strength=strength,
                brick_index=classified.brick.index,
                price=classified.brick.close_price,
                timestamp=classified.brick.timestamp,
                label_sequence=self._label_sequence[-self.bull_trigger_n:],
                velocity=velocity,
                metadata={"run_length": self._bull_run_count},
            )

        # Bear trigger
        if self._bear_run_count >= self.bear_trigger_n and label in (SwingLabel.LL, SwingLabel.LH):
            strength = self._compute_strength(self._bear_run_count, velocity)
            return PatternSignal(
                pattern_type=PatternType.BEAR_RUN,
                direction=SignalDirection.SHORT,
                strength=strength,
                brick_index=classified.brick.index,
                price=classified.brick.close_price,
                timestamp=classified.brick.timestamp,
                label_sequence=self._label_sequence[-self.bear_trigger_n:],
                velocity=velocity,
                metadata={"run_length": self._bear_run_count},
            )

        return None

    def _check_reversal(
        self,
        classified: ClassifiedBrick,
        velocity: float,
    ) -> Optional[PatternSignal]:
        """
        Check for a reversal: first opposite-class brick after a run.

        e.g., After 4 HH/HL bricks, the first LL brick signals
        a potential reversal -> exit long / enter short.
        """
        label = classified.label

        # Bullish reversal: after bearish run, first bullish brick
        if label in (SwingLabel.HH, SwingLabel.HL) and self._bear_run_count == 0 and self._prev_label in (SwingLabel.LL, SwingLabel.LH):
            # This is the first bullish brick after a bearish run
            prev_bear_run = self._bear_run_count  # Already reset to 0
            return PatternSignal(
                pattern_type=PatternType.REVERSAL,
                direction=SignalDirection.LONG,
                strength=0.6,
                brick_index=classified.brick.index,
                price=classified.brick.close_price,
                timestamp=classified.brick.timestamp,
                label_sequence=self._label_sequence[-3:],
                velocity=velocity,
                metadata={"reversal_after_bear": True},
            )

        # Bearish reversal: after bullish run, first bearish brick
        if label in (SwingLabel.LL, SwingLabel.LH) and self._bull_run_count == 0 and self._prev_label in (SwingLabel.HH, SwingLabel.HL):
            return PatternSignal(
                pattern_type=PatternType.REVERSAL,
                direction=SignalDirection.SHORT,
                strength=0.6,
                brick_index=classified.brick.index,
                price=classified.brick.close_price,
                timestamp=classified.brick.timestamp,
                label_sequence=self._label_sequence[-3:],
                velocity=velocity,
                metadata={"reversal_after_bull": True},
            )

        return None

    def _check_double_pattern(
        self,
        classified: ClassifiedBrick,
        velocity: float,
    ) -> Optional[PatternSignal]:
        """
        Check for double-top / double-bottom using swing points.

        Two swing highs at the same price level = double top (bearish).
        Two swing lows at the same price level = double bottom (bullish).
        """
        if classified.swing_point is None:
            return None

        sp = classified.swing_point
        brick_size = classified.brick.brick_size
        tolerance = brick_size * 1.5  # Allow 1.5 brick tolerance

        if sp.swing_type == SwingType.HIGH and len(self._recent_swing_highs) >= 2:
            # Compare with previous swing high
            prev_high = self._recent_swing_highs[-2]
            if abs(sp.price - prev_high.price) <= tolerance:
                # Double top detected
                return PatternSignal(
                    pattern_type=PatternType.DOUBLE_TOP,
                    direction=SignalDirection.SHORT,
                    strength=0.7,
                    brick_index=classified.brick.index,
                    price=classified.brick.close_price,
                    timestamp=classified.brick.timestamp,
                    label_sequence=self._label_sequence[-4:],
                    velocity=velocity,
                    metadata={
                        "price1": prev_high.price,
                        "price2": sp.price,
                        "brick_distance": classified.brick.index - prev_high.brick_index,
                    },
                )

        elif sp.swing_type == SwingType.LOW and len(self._recent_swing_lows) >= 2:
            prev_low = self._recent_swing_lows[-2]
            if abs(sp.price - prev_low.price) <= tolerance:
                # Double bottom detected
                return PatternSignal(
                    pattern_type=PatternType.DOUBLE_BOTTOM,
                    direction=SignalDirection.LONG,
                    strength=0.7,
                    brick_index=classified.brick.index,
                    price=classified.brick.close_price,
                    timestamp=classified.brick.timestamp,
                    label_sequence=self._label_sequence[-4:],
                    velocity=velocity,
                    metadata={
                        "price1": prev_low.price,
                        "price2": sp.price,
                        "brick_distance": classified.brick.index - prev_low.brick_index,
                    },
                )

        return None

    def _check_consolidation_break(
        self,
        classified: ClassifiedBrick,
        velocity: float,
    ) -> Optional[PatternSignal]:
        """
        Check for consolidation breakout.

        A consolidation is a sequence of mixed HH/HL/LH/LL labels.
        When a directional breakout occurs after consolidation, it
        signals trend resumption.
        """
        if len(self._label_sequence) < 6:
            return None

        recent = self._label_sequence[-6:]
        bullish_count = sum(1 for l in recent if l in ("HH", "HL"))
        bearish_count = sum(1 for l in recent if l in ("LL", "LH"))

        total = bullish_count + bearish_count
        if total == 0:
            return None

        mix_ratio = min(bullish_count, bearish_count) / total

        # Only trigger if we had consolidation AND a new directional brick
        if mix_ratio >= self.consolidation_max_mix:
            label = classified.label
            if label in (SwingLabel.HH, SwingLabel.HL) and bullish_count > bearish_count:
                return PatternSignal(
                    pattern_type=PatternType.CONSOLIDATION_BREAK,
                    direction=SignalDirection.LONG,
                    strength=0.5,
                    brick_index=classified.brick.index,
                    price=classified.brick.close_price,
                    timestamp=classified.brick.timestamp,
                    label_sequence=recent,
                    velocity=velocity,
                    metadata={"consolidation_ratio": mix_ratio},
                )
            elif label in (SwingLabel.LL, SwingLabel.LH) and bearish_count > bullish_count:
                return PatternSignal(
                    pattern_type=PatternType.CONSOLIDATION_BREAK,
                    direction=SignalDirection.SHORT,
                    strength=0.5,
                    brick_index=classified.brick.index,
                    price=classified.brick.close_price,
                    timestamp=classified.brick.timestamp,
                    label_sequence=recent,
                    velocity=velocity,
                    metadata={"consolidation_ratio": mix_ratio},
                )

        return None

    # ── Helpers ───────────────────────────────────────────────────

    def _compute_strength(
        self,
        run_length: int,
        velocity: float,
    ) -> float:
        """
        Compute signal strength (0.0 - 1.0).

        Factors:
          - Run length: longer runs = stronger (capped at 2x trigger N)
          - Velocity: fast brick formation = stronger
        """
        trigger_n = max(self.bull_trigger_n, self.bear_trigger_n)
        max_factor = 2.0
        run_ratio = min(run_length / (trigger_n * max_factor), 1.0)
        base_strength = 0.4 + 0.4 * run_ratio

        # Velocity boost (0.0 - 0.2)
        if self.velocity_threshold > 0 and velocity > self.velocity_threshold:
            vel_boost = min(
                (velocity - self.velocity_threshold) / self.velocity_threshold,
                1.0,
            ) * 0.2
        else:
            vel_boost = 0.0

        return min(base_strength + vel_boost, 1.0)

    def reset(self) -> None:
        """Reset detector state."""
        self.signals.clear()
        self._last_signal_idx = -999
        self._bull_run_count = 0
        self._bear_run_count = 0
        self._prev_label = None
        self._label_sequence = []
        self._recent_swing_highs = []
        self._recent_swing_lows = []
