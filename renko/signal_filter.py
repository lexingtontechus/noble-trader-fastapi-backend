"""
Signal Filter - the risk governance layer.

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
from typing import Any, Deque, List, Optional

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
    # Masaniello sizing fields (populated when sizing_mode=masaniello)
    sizing_mode: str = "kelly"           # "kelly" or "masaniello"
    masaniello_factor: float = 0.0       # M_i: urgency
    quality_factor: float = 0.0          # Q_i: model quality
    drawdown_factor: float = 0.0         # DD_i: drawdown scaling
    volatility_factor: float = 0.0       # V_i: vol adjustment
    risk_fraction: float = 0.0           # Final risk fraction after all factors
    contracts: int = 0                   # Calculated contracts/shares

    def to_dict(self) -> dict:
        return {
            "signal": self.original_signal.to_dict(),
            "passed_filters": self.passed_filters,
            "regime_aligned": self.regime_aligned,
            "kelly_fraction": self.kelly_fraction,
            "position_size_usd": self.position_size_usd,
            "brick_size": self.brick_size,
            "sizing_mode": self.sizing_mode,
            "masaniello_factor": self.masaniello_factor,
            "quality_factor": self.quality_factor,
            "drawdown_factor": self.drawdown_factor,
            "volatility_factor": self.volatility_factor,
            "risk_fraction": self.risk_fraction,
            "contracts": self.contracts,
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
        sizing_mode: str = "kelly",
        masaniello_sizing_config: Optional[Any] = None,
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
        self.sizing_mode = sizing_mode

        # Masaniello sizer instance (lazy-created from config when mode=masaniello)
        self._masaniello_sizer = masaniello_sizing_config
        self._masaniello_batch_wins = 0
        self._masaniello_batch_losses = 0
        self._masaniello_trade_index = 0
        self._masaniello_peak_equity = 0.0
        self._masaniello_initial_equity = 0.0
        self._masaniello_pnl = 0.0

        self._stats = _SessionStats()
        self._rejected: List[dict] = []  # Log of rejected signals

        # Warmup mode: bypasses session window, lunch, and cooldown filters.
        # Enabled during batch warmup and backtests where timestamps are
        # synthetic or historical and don't represent real market hours.
        self._warmup_mode: bool = False

    # ── Public API ────────────────────────────────────────────────

    def evaluate(
        self,
        signal: PatternSignal,
        regime: Optional[str] = None,
        brick_velocity: float = 0.0,
        equity: float = 100000.0,
        brick_size: float = 0.50,
        kelly_fraction: float = 0.0,
        regime_quality: float = 1.0,
        regime_confidence: float = 0.75,
        current_drawdown: float = 0.0,
        atr_baseline: float = 0.0,
        atr_current: float = 0.0,
        default_win_rate: float = 0.55,
        sl_bricks: int = 3,
        tp_bricks: int = 5,
        trend_probs: Optional[dict] = None,
    ) -> Optional[FilteredSignal]:
        """
        Evaluate a signal through all filters.
        """
        now = signal.timestamp
        passed: List[str] = []
        rejection_reason: Optional[str] = None

        # In warmup mode, bypass session window, lunch, and cooldown filters.
        if self._warmup_mode:
            passed.append("session_window_warmup")
            passed.append("cooldown_warmup")
        else:
            # Filter 1: Session window
            if not self._check_session_window(now):
                rejection_reason = "outside_session_window"
            else:
                passed.append("session_window")

            # Filter 5: Cooldown (only in live mode)
            if rejection_reason is None:
                elapsed = now - self._stats.last_trade_ts
                if elapsed < self.cooldown_seconds:
                    rejection_reason = f"cooldown_{self.cooldown_seconds - elapsed:.1f}s_remaining"
                else:
                    passed.append("cooldown")

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
            regime_aligned = True

        # Filter 8: EV Expectancy Gate (now with real P_regime when available)
        if rejection_reason is None:
            try:
                from regime_platform.services.statistics.ev_engine import EVEngine
                ev_engine = EVEngine()
                
                # Determine direction from signal
                direction = "long" if signal.direction == SignalDirection.LONG else "short"
                
                # We use the sync wrapper to avoid blocking the sync pipeline
                # Pass trend_probs and direction for real P_regime when available
                ev_res = ev_engine.validate_signal_sync(
                    db_session=None, 
                    regime=regime or "unknown",
                    pattern=signal.label,
                    expected_profit=tp_bricks * brick_size,
                    expected_loss=sl_bricks * brick_size,
                    trend_probs=trend_probs,  # NEW: pass HMM trend probs
                    direction=direction,  # NEW: pass direction
                )
                
                if not ev_res.allowed:
                    rejection_reason = f"ev_insufficient: {ev_res.reason}"
                else:
                    passed.append("ev_filter")
            except Exception as ev_e:
                passed.append("ev_filter_skipped_error")

        # Rejected
        if rejection_reason is not None:
            self._rejected.append({
                "timestamp": now,
                "reason": rejection_reason,
                "signal": signal.to_dict(),
            })
            return None

        # Passed - compute position size based on sizing_mode
        if self.sizing_mode == "masaniello" and self._masaniello_sizer is not None:
            sizing_result = self._compute_masaniello_size(
                signal=signal,
                equity=equity,
                brick_size=brick_size,
                regime_quality=regime_quality,
                regime_confidence=regime_confidence,
                current_drawdown=current_drawdown,
                atr_baseline=atr_baseline,
                atr_current=atr_current,
                default_win_rate=default_win_rate,
                sl_bricks=sl_bricks,
                tp_bricks=tp_bricks,
            )
            if sizing_result is not None:
                return sizing_result
            # Masaniello blocked the trade - fall through to Kelly as fallback
            # (This happens when Masaniello gates reject, e.g. p_win too low)

        # Kelly sizing (default or Masaniello fallback)
        effective_kelly = kelly_fraction if kelly_fraction > 0 else 0.05
        position_usd = equity * min(effective_kelly, 0.10)  # Cap at 10%

        return FilteredSignal(
            original_signal=signal,
            passed_filters=passed,
            regime_aligned=regime_aligned,
            kelly_fraction=effective_kelly,
            position_size_usd=position_usd,
            brick_size=brick_size,
            sizing_mode="kelly",
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

    def set_warmup_mode(self, enabled: bool) -> None:
        """
        Enable or disable warmup mode.

        When enabled, session window, lunch skip, and cooldown filters
        are bypassed. This is used during batch warmup and backtests
        where timestamps are synthetic or historical and don't represent
        real market hours.

        Risk limits (max trades, max daily loss, consecutive losses)
        still apply even in warmup mode.
        """
        self._warmup_mode = enabled

    @property
    def warmup_mode(self) -> bool:
        """Whether warmup mode is currently active."""
        return self._warmup_mode

    def _compute_masaniello_size(
        self,
        signal: PatternSignal,
        equity: float,
        brick_size: float,
        regime_quality: float,
        regime_confidence: float,
        current_drawdown: float,
        atr_baseline: float,
        atr_current: float,
        default_win_rate: float,
        sl_bricks: int,
        tp_bricks: int,
    ) -> Optional[FilteredSignal]:
        """Compute position size using the Dynamic Masaniello formula."""
        from regime_platform.core.position_sizer import DynamicMasanielloSizer, TradeContext

        sizer = self._masaniello_sizer

        # Compute stop distance and reward/risk from brick params
        stop_distance_price = sl_bricks * brick_size
        reward_risk = tp_bricks / sl_bricks if sl_bricks > 0 else 2.0

        # Use trade journal win rate if available, else default
        p_win = default_win_rate
        wr = self.session_stats.get("win_rate", 0.0)
        if wr > 0:
            p_win = wr

        # Use default ATR values if not provided
        if atr_baseline <= 0:
            atr_baseline = brick_size * 20  # Approximate: 20-brick ATR
        if atr_current <= 0:
            atr_current = atr_baseline

        # Compute actual drawdown if we have equity tracking
        if self._masaniello_initial_equity > 0 and self._masaniello_peak_equity > 0:
            current_equity = self._masaniello_initial_equity + self._masaniello_pnl
            if current_equity < self._masaniello_peak_equity:
                current_drawdown = (self._masaniello_peak_equity - current_equity) / self._masaniello_peak_equity

        direction = "long" if signal.direction.value.upper() in ("LONG", "BUY") else "short"

        ctx = TradeContext(
            equity=equity,
            stop_distance_price=stop_distance_price,
            point_value=1.0,  # Stocks: 1 point = $1
            p_win=float(min(max(p_win, 0.0), 1.0)),
            reward_risk=float(max(reward_risk, 0.0)),
            regime_quality=float(regime_quality),
            state_confidence=float(regime_confidence),
            current_drawdown=float(current_drawdown),
            atr_baseline=float(atr_baseline),
            atr_current=float(atr_current),
            wins_so_far=self._masaniello_batch_wins,
            losses_so_far=self._masaniello_batch_losses,
            trade_index=self._masaniello_trade_index + 1,
            batch_size=sizer.config.batch_size,
            target_wins=sizer.config.target_wins,
            direction=direction,
            symbol="",
            regime_label="",
        )

        result = sizer.size_trade(ctx)

        if not result.allowed:
            # Masaniello blocked - log and return None to fall back to Kelly
            self._rejected.append({
                "timestamp": signal.timestamp,
                "reason": f"masaniello_blocked: {result.reason}",
                "signal": signal.to_dict(),
            })
            return None

        position_usd = result.risk_amount

        # Update Masaniello batch state
        self._masaniello_trade_index += 1
        if equity > self._masaniello_peak_equity:
            self._masaniello_peak_equity = equity
        if self._masaniello_initial_equity <= 0:
            self._masaniello_initial_equity = equity
            self._masaniello_peak_equity = equity

        return FilteredSignal(
            original_signal=signal,
            passed_filters=[],  # Already validated above
            regime_aligned=True,
            kelly_fraction=0.0,  # Not Kelly
            position_size_usd=position_usd,
            brick_size=brick_size,
            sizing_mode="masaniello",
            masaniello_factor=result.masaniello_factor,
            quality_factor=result.quality_factor,
            drawdown_factor=result.drawdown_factor,
            volatility_factor=result.volatility_factor,
            risk_fraction=result.risk_fraction,
            contracts=result.contracts,
        )

    def record_masaniello_trade(self, pnl_bricks: float, pnl_dollars: float = 0.0) -> None:
        """Record a trade result for Masaniello batch state tracking."""
        if pnl_bricks > 0:
            self._masaniello_batch_wins += 1
        else:
            self._masaniello_batch_losses += 1
        self._masaniello_pnl += pnl_dollars
        # Update peak equity
        current_equity = self._masaniello_initial_equity + self._masaniello_pnl
        self._masaniello_peak_equity = max(self._masaniello_peak_equity, current_equity)

    def reset_session(self) -> None:
        """Reset per-session statistics (call at market open)."""
        self._stats = _SessionStats()
        self._warmup_mode = False  # Disable warmup mode on session reset
        self._masaniello_batch_wins = 0
        self._masaniello_batch_losses = 0
        self._masaniello_trade_index = 0
        self._masaniello_peak_equity = 0.0
        self._masaniello_initial_equity = 0.0
        self._masaniello_pnl = 0.0

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
