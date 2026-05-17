"""
Signal Filter — the risk governance layer.

Filters raw pattern signals through multiple safety checks before
they reach the order executor:

  - Session window (market hours only)
  - Max trades per session (hard cap)
  - Max daily loss (brick-based drawdown limit)
  - Max consecutive losses (pause after N losses)
  - Cooldown between trades (prevent machine-gun entries)
  - Brick velocity filter (no trading in dead markets)
  - HMM regime gate (only trade with regime alignment)

This is the last line of defence before real money is at risk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Deque, List, Optional

from collections import deque

from renko.pattern_detector import PatternSignal, SignalDirection


# ── Data types ─────────────────────────────────────────────────────


@dataclass
class FilteredSignal:
    """A signal that has passed all filters and is ready for execution."""

    original_signal: PatternSignal
    passed_filters: List[str]     # Names of filters that passed
    regime_aligned: bool = False  # Whether HMM regime agrees
    kelly_fraction: float = 0.0  # Kelly-derived position size fraction
    position_size_usd: float = 0.0
    brick_size: float = 0.0

    def to_dict(self) -> dict:
        return {
            "signal": self.original_signal.to_dict(),
            "passed_filters": self.passed_filters,
            "regime_aligned": self.regime_aligned,
            "kelly_fraction": self.kelly_fraction,
            "position_size_usd": self.position_size_usd,
            "brick_size": self.brick_size,
        }


@dataclass
class _SessionStats:
    """Mutable per-session statistics used by the filter."""

    trades_taken: int = 0
    daily_pnl_bricks: float = 0.0
    consecutive_losses: int = 0
    last_trade_ts: float = 0.0
    total_wins: int = 0
    total_losses: int = 0


# ── Filter ─────────────────────────────────────────────────────────


class SignalFilter:
    """
    Gate-keeps pattern signals through risk governance rules.

    Usage::

        filt = SignalFilter(config_dict)
        result = filt.evaluate(signal, regime="Bullish Trend")
        if result:
            # Send to executor
    """

    def __init__(
        self,
        session_start: str = "09:35",
        session_end: str = "15:45",
        skip_lunch: bool = True,
        lunch_start: str = "11:30",
        lunch_end: str = "13:00",
        max_trades_per_session: int = 15,
        max_daily_loss_bricks: float = 10.0,
        max_consecutive_losses: int = 3,
        cooldown_seconds: float = 30.0,
        min_brick_velocity: float = 0.0,
        regime_gate: bool = True,
        timezone: str = "America/New_York",
    ):
        self.session_start = self._parse_time(session_start)
        self.session_end = self._parse_time(session_end)
        self.skip_lunch = skip_lunch
        self.lunch_start = self._parse_time(lunch_start)
        self.lunch_end = self._parse_time(lunch_end)
        self.max_trades_per_session = max_trades_per_session
        self.max_daily_loss_bricks = max_daily_loss_bricks
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_seconds = cooldown_seconds
        self.min_brick_velocity = min_brick_velocity
        self.regime_gate = regime_gate
        self.timezone = timezone

        self._stats = _SessionStats()
        self._rejected: List[dict] = []  # Log of rejected signals

    # ── Public API ────────────────────────────────────────────────

    def evaluate(
        self,
        signal: PatternSignal,
        regime: Optional[str] = None,
        brick_velocity: float = 0.0,
        equity: float = 100000.0,
        brick_size: float = 0.50,
        kelly_fraction: float = 0.0,
    ) -> Optional[FilteredSignal]:
        """
        Evaluate a signal through all filters.

        Parameters
        ----------
        signal : PatternSignal
            Raw signal from PatternDetector.
        regime : str, optional
            Current HMM regime name (e.g. "Bullish Trend").
        brick_velocity : float
            Current bricks/minute.
        equity : float
            Current account equity.
        brick_size : float
            Current brick size in dollars.
        kelly_fraction : float
            Kelly-derived position sizing fraction.

        Returns
        -------
        FilteredSignal or None
            Filtered signal if it passes, None if rejected.
        """
        now = signal.timestamp
        passed: List[str] = []
        rejection_reason: Optional[str] = None

        # Filter 1: Session window
        if not self._check_session_window(now):
            rejection_reason = "outside_session_window"
        else:
            passed.append("session_window")

        # Filter 2: Max trades per session
        if rejection_reason is None and self._stats.trades_taken >= self.max_trades_per_session:
            rejection_reason = "max_trades_reached"
        elif rejection_reason is None:
            passed.append("max_trades")

        # Filter 3: Max daily loss
        if rejection_reason is None and self._stats.daily_pnl_bricks <= -self.max_daily_loss_bricks:
            rejection_reason = "max_daily_loss"
        elif rejection_reason is None:
            passed.append("daily_loss_limit")

        # Filter 4: Max consecutive losses
        if rejection_reason is None and self._stats.consecutive_losses >= self.max_consecutive_losses:
            rejection_reason = "max_consecutive_losses"
        elif rejection_reason is None:
            passed.append("consecutive_loss_limit")

        # Filter 5: Cooldown
        if rejection_reason is None:
            elapsed = now - self._stats.last_trade_ts
            if elapsed < self.cooldown_seconds:
                rejection_reason = f"cooldown_{self.cooldown_seconds - elapsed:.1f}s_remaining"
            else:
                passed.append("cooldown")

        # Filter 6: Brick velocity
        if rejection_reason is None and self.min_brick_velocity > 0:
            if brick_velocity < self.min_brick_velocity:
                rejection_reason = "low_velocity"
            else:
                passed.append("velocity")

        # Filter 7: Regime gate
        regime_aligned = False
        if rejection_reason is None and self.regime_gate:
            regime_aligned = self._check_regime_alignment(signal, regime)
            if not regime_aligned:
                rejection_reason = "regime_misaligned"
            else:
                passed.append("regime_gate")
        elif rejection_reason is None:
            passed.append("regime_gate_skipped")
            regime_aligned = True  # No gate = always aligned

        # Rejected
        if rejection_reason is not None:
            self._rejected.append({
                "timestamp": now,
                "reason": rejection_reason,
                "signal": signal.to_dict(),
            })
            return None

        # Passed — compute position size
        effective_kelly = kelly_fraction if kelly_fraction > 0 else 0.05
        position_usd = equity * min(effective_kelly, 0.10)  # Cap at 10%

        return FilteredSignal(
            original_signal=signal,
            passed_filters=passed,
            regime_aligned=regime_aligned,
            kelly_fraction=effective_kelly,
            position_size_usd=position_usd,
            brick_size=brick_size,
        )

    def record_trade_result(self, pnl_bricks: float) -> None:
        """
        Record the result of a completed trade (for filter tracking).

        Parameters
        ----------
        pnl_bricks : float
            P&L in brick units (positive = win, negative = loss).
        """
        self._stats.trades_taken += 1
        self._stats.daily_pnl_bricks += pnl_bricks
        self._stats.last_trade_ts = time.time()

        if pnl_bricks > 0:
            self._stats.consecutive_losses = 0
            self._stats.total_wins += 1
        else:
            self._stats.consecutive_losses += 1
            self._stats.total_losses += 1

    def reset_session(self) -> None:
        """Reset per-session statistics (call at market open)."""
        self._stats = _SessionStats()

    @property
    def session_stats(self) -> dict:
        return {
            "trades_taken": self._stats.trades_taken,
            "daily_pnl_bricks": self._stats.daily_pnl_bricks,
            "consecutive_losses": self._stats.consecutive_losses,
            "total_wins": self._stats.total_wins,
            "total_losses": self._stats.total_losses,
            "win_rate": (
                self._stats.total_wins /
                (self._stats.total_wins + self._stats.total_losses)
                if (self._stats.total_wins + self._stats.total_losses) > 0
                else 0.0
            ),
        }

    @property
    def rejected_signals(self) -> List[dict]:
        return list(self._rejected)

    # ── Internal ──────────────────────────────────────────────────

    def _check_session_window(self, timestamp: float) -> bool:
        """Check if the current time is within the trading session."""
        try:
            from datetime import timezone as tz
            from zoneinfo import ZoneInfo
            dt = datetime.fromtimestamp(timestamp, tz=ZoneInfo(self.timezone))
        except (ImportError, Exception):
            dt = datetime.fromtimestamp(timestamp)

        current_time = dt.time()
        weekday = dt.weekday()

        # No weekend trading
        if weekday >= 5:
            return False

        # Must be within session window
        if not (self.session_start <= current_time <= self.session_end):
            return False

        # Lunch skip
        if self.skip_lunch and self.lunch_start <= current_time <= self.lunch_end:
            return False

        return True

    def _check_regime_alignment(
        self,
        signal: PatternSignal,
        regime: Optional[str],
    ) -> bool:
        """
        Check if signal direction aligns with the HMM regime.

        Long signals are allowed in bullish regimes.
        Short signals are allowed in bearish regimes.
        Any signal is allowed in range-bound regimes.
        """
        if regime is None:
            return True  # No regime info = allow

        regime_lower = regime.lower()

        if signal.direction == SignalDirection.LONG:
            return "bull" in regime_lower or "range" in regime_lower
        elif signal.direction == SignalDirection.SHORT:
            return "bear" in regime_lower or "range" in regime_lower

        return True

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        """Parse HH:MM string into datetime.time."""
        parts = time_str.split(":")
        return dt_time(int(parts[0]), int(parts[1]))
