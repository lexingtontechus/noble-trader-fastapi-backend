"""
Risk Manager - brick-denominated risk management for Renko HFT.

Because every Renko brick is the same size, risk parameters are
expressed in brick units which translate to deterministic dollar
amounts. This eliminates the ambiguity of ATR-based or percentage-
based stops on time charts.

Core rules:
  - SL = N bricks below entry (for longs) / above entry (for shorts)
  - TP = M bricks above entry (for longs) / below entry (for shorts)
  - Trailing stop: activate after K bricks profit, trail by L bricks
  - Time stop: close if open for more than P bricks without TP/SL
  - Partial exit: optionally close X% at half-TP
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional

from collections import deque

from renko.brick_engine import Brick, BrickDirection
from renko.pattern_detector import SignalDirection


# ── Data types ─────────────────────────────────────────────────────


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED_TP = "closed_tp"
    CLOSED_SL = "closed_sl"
    CLOSED_SL_GAP = "closed_sl_gap"      # Gap-through stop (Phase 3)
    CLOSED_OCO_SL = "closed_oco_sl"      # OCO resolved as SL (Phase 3)
    CLOSED_OCO_TP = "closed_oco_tp"      # OCO resolved as TP (Phase 3)
    CLOSED_TRAIL = "closed_trail"
    CLOSED_TIME = "closed_time"
    CLOSED_MANUAL = "closed_manual"


@dataclass
class RiskParams:
    """Risk parameters for a single trade."""

    sl_bricks: int = 3
    tp_bricks: int = 5
    trailing_stop: bool = True
    trail_after_bricks: int = 3
    trail_distance_bricks: int = 2
    time_stop_bricks: int = 10
    partial_exit_pct: float = 0.0
    partial_exit_bricks: int = 3
    slippage_bps: float = 2.0            # Slippage in basis points (Phase 2)
    oco_priority: str = "sl_first"       # OCO resolution priority (Phase 3)

    @property
    def risk_reward_ratio(self) -> float:
        return self.tp_bricks / self.sl_bricks if self.sl_bricks > 0 else 0.0

    @property
    def breakeven_win_rate(self) -> float:
        total = self.sl_bricks + self.tp_bricks
        return self.sl_bricks / total if total > 0 else 0.0


@dataclass
class ActivePosition:
    """An open position being managed by the risk manager."""

    entry_price: float
    direction: SignalDirection
    brick_size: float
    entry_ts: float
    entry_brick_idx: int
    qty: float = 1.0
    sl_price: float = 0.0
    tp_price: float = 0.0
    params: RiskParams = field(default_factory=RiskParams)

    # Dynamic state
    peak_profit_bricks: float = 0.0
    current_sl_price: float = 0.0
    bricks_open: int = 0
    partial_closed: bool = False
    remaining_qty: float = 1.0

    def __post_init__(self):
        """Calculate initial SL/TP prices."""
        if self.direction == SignalDirection.LONG:
            self.sl_price = self.entry_price - self.params.sl_bricks * self.brick_size
            self.tp_price = self.entry_price + self.params.tp_bricks * self.brick_size
        else:
            self.sl_price = self.entry_price + self.params.sl_bricks * self.brick_size
            self.tp_price = self.entry_price - self.params.tp_bricks * self.brick_size
        self.current_sl_price = self.sl_price


@dataclass
class TradeResult:
    """Result of a closed trade."""

    entry_price: float
    exit_price: float
    direction: SignalDirection
    pnl_bricks: float
    pnl_dollars: float
    status: PositionStatus
    entry_ts: float
    exit_ts: float
    bricks_held: int
    peak_profit_bricks: float
    qty: float = 1.0

    @property
    def is_win(self) -> bool:
        return self.pnl_bricks > 0

    @property
    def hold_duration_seconds(self) -> float:
        return self.exit_ts - self.entry_ts

    def to_dict(self) -> dict:
        return {
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "direction": self.direction.value,
            "pnl_bricks": self.pnl_bricks,
            "pnl_dollars": self.pnl_dollars,
            "status": self.status.value,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "bricks_held": self.bricks_held,
            "peak_profit_bricks": self.peak_profit_bricks,
            "qty": self.qty,
        }


# ── Risk Manager ───────────────────────────────────────────────────


class RiskManager:
    """
    Manages open positions and enforces brick-denominated risk rules.

    Call ``evaluate()`` on every new brick to check SL/TP/trailing/
    time stops. The manager returns a TradeResult when a position
    should be closed.
    """

    def __init__(self, params: Optional[RiskParams] = None, slippage_bps: float = 2.0):
        self.params = params or RiskParams()
        self.slippage_bps = slippage_bps
        self.active_position: Optional[ActivePosition] = None
        self.closed_trades: List[TradeResult] = []

    # ── Public API ────────────────────────────────────────────────

    def open_position(
        self,
        entry_price: float,
        direction: SignalDirection,
        brick_size: float,
        entry_ts: float,
        entry_brick_idx: int,
        qty: float = 1.0,
        params: Optional[RiskParams] = None,
    ) -> ActivePosition:
        """
        Open a new position. Only one position at a time is supported.

        Returns the ActivePosition for reference.
        """
        if self.active_position is not None:
            raise RuntimeError("Cannot open position: existing position is open")

        effective_params = params or self.params
        self.active_position = ActivePosition(
            entry_price=entry_price,
            direction=direction,
            brick_size=brick_size,
            entry_ts=entry_ts,
            entry_brick_idx=entry_brick_idx,
            qty=qty,
            params=effective_params,
        )
        return self.active_position

    def evaluate(self, brick: Brick) -> Optional[TradeResult]:
        """
        Evaluate the current position against the latest brick.

        Checks OCO first (both SL+TP in same brick), then individual
        SL, TP, trailing, time stop, and partial exit.
        """
        pos = self.active_position
        if pos is None:
            return None

        pos.bricks_open += 1

        # Check OCO first (both SL and TP triggered in same brick)
        oco_result = self._check_oco(pos, brick)
        if oco_result is not None:
            self._close_position(oco_result)
            return oco_result

        # Check SL
        result = self._check_stop_loss(pos, brick)
        if result is not None:
            self._close_position(result)
            return result

        # Check TP
        result = self._check_take_profit(pos, brick)
        if result is not None:
            self._close_position(result)
            return result

        # Update trailing stop
        self._update_trailing(pos, brick)
        result = self._check_trailing_stop(pos, brick)
        if result is not None:
            self._close_position(result)
            return result

        # Check time stop
        result = self._check_time_stop(pos, brick)
        if result is not None:
            self._close_position(result)
            return result

        # Check partial exit (no close)
        self._check_partial_exit(pos, brick)

        return None

    def force_close(self, brick: Brick) -> TradeResult:
        """Force close the current position at the brick's price."""
        pos = self.active_position
        if pos is None:
            raise RuntimeError("No position to close")

        exit_price = brick.close_price
        pnl_bricks = self._calc_pnl_bricks(pos, exit_price)
        pnl_dollars = pnl_bricks * pos.brick_size * pos.remaining_qty

        result = TradeResult(
            entry_price=pos.entry_price,
            exit_price=exit_price,
            direction=pos.direction,
            pnl_bricks=pnl_bricks,
            pnl_dollars=pnl_dollars,
            status=PositionStatus.CLOSED_MANUAL,
            entry_ts=pos.entry_ts,
            exit_ts=brick.timestamp,
            bricks_held=pos.bricks_open,
            peak_profit_bricks=pos.peak_profit_bricks,
            qty=pos.remaining_qty,
        )
        self._close_position(result)
        return result

    @property
    def has_position(self) -> bool:
        return self.active_position is not None

    @property
    def current_pnl_bricks(self) -> float:
        """Unrealised P&L in bricks (requires latest price)."""
        if self.active_position is None:
            return 0.0
        # Use current SL as reference for unrealised - caller should
        # call evaluate() for accurate P&L
        return self.active_position.peak_profit_bricks

    # ── Internal checks ───────────────────────────────────────────

    def _check_stop_loss(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if stop-loss has been hit, with gap-through and slippage."""
        if pos.direction == SignalDirection.LONG:
            # Gap-through: brick opens below stop
            if brick.open_price <= pos.current_sl_price:
                exit_price = brick.open_price * (1 - self.slippage_bps / 10000)
                return self._make_result(pos, exit_price, PositionStatus.CLOSED_SL_GAP, brick)
            # Normal: brick closes below stop
            if brick.close_price <= pos.current_sl_price:
                exit_price = pos.current_sl_price * (1 - self.slippage_bps / 10000)
                return self._make_result(pos, exit_price, PositionStatus.CLOSED_SL, brick)
        else:
            # Gap-through: brick opens above stop (for short)
            if brick.open_price >= pos.current_sl_price:
                exit_price = brick.open_price * (1 + self.slippage_bps / 10000)
                return self._make_result(pos, exit_price, PositionStatus.CLOSED_SL_GAP, brick)
            # Normal: brick closes above stop
            if brick.close_price >= pos.current_sl_price:
                exit_price = pos.current_sl_price * (1 + self.slippage_bps / 10000)
                return self._make_result(pos, exit_price, PositionStatus.CLOSED_SL, brick)
        return None

    def _check_take_profit(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if take-profit has been reached, with slight adverse slippage on limit fills."""
        if pos.direction == SignalDirection.LONG:
            if brick.close_price >= pos.tp_price:
                # TP is a limit order - slight adverse fill but much less than SL
                exit_price = pos.tp_price * (1 - self.slippage_bps / 20000)
                return self._make_result(pos, exit_price, PositionStatus.CLOSED_TP, brick)
        else:
            if brick.close_price <= pos.tp_price:
                exit_price = pos.tp_price * (1 + self.slippage_bps / 20000)
                return self._make_result(pos, exit_price, PositionStatus.CLOSED_TP, brick)
        return None

    def _update_trailing(self, pos: ActivePosition, brick: Brick) -> None:
        """Update trailing stop level if conditions are met."""
        if not pos.params.trailing_stop:
            return

        current_profit = self._calc_pnl_bricks(pos, brick.close_price)

        if current_profit > pos.peak_profit_bricks:
            pos.peak_profit_bricks = current_profit

        # Activate trailing after threshold
        if pos.peak_profit_bricks >= pos.params.trail_after_bricks:
            if pos.direction == SignalDirection.LONG:
                new_sl = brick.close_price - pos.params.trail_distance_bricks * pos.brick_size
                # Only move SL up, never down
                if new_sl > pos.current_sl_price:
                    pos.current_sl_price = new_sl
            else:
                new_sl = brick.close_price + pos.params.trail_distance_bricks * pos.brick_size
                # Only move SL down (for shorts), never up
                if new_sl < pos.current_sl_price:
                    pos.current_sl_price = new_sl

    def _check_trailing_stop(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if trailing stop has been hit (delegated to SL check)."""
        if pos.peak_profit_bricks < pos.params.trail_after_bricks:
            return None  # Trailing not active yet

        # The trailing stop IS the current SL - if SL is hit and
        # trailing is active, we label it as a trailing exit
        if pos.direction == SignalDirection.LONG:
            if brick.close_price <= pos.current_sl_price and pos.current_sl_price > pos.sl_price:
                return self._make_result(pos, pos.current_sl_price, PositionStatus.CLOSED_TRAIL, brick)
        else:
            if brick.close_price >= pos.current_sl_price and pos.current_sl_price < pos.sl_price:
                return self._make_result(pos, pos.current_sl_price, PositionStatus.CLOSED_TRAIL, brick)
        return None

    def _check_oco(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if both SL and TP are triggered in the same brick (OCO handling)."""
        sl_triggered = False
        tp_triggered = False

        if pos.direction == SignalDirection.LONG:
            sl_triggered = brick.close_price <= pos.current_sl_price or brick.open_price <= pos.current_sl_price
            tp_triggered = brick.close_price >= pos.tp_price or brick.open_price >= pos.tp_price
        else:
            sl_triggered = brick.close_price >= pos.current_sl_price or brick.open_price >= pos.current_sl_price
            tp_triggered = brick.close_price <= pos.tp_price or brick.open_price <= pos.tp_price

        if not (sl_triggered and tp_triggered):
            return None

        # Both triggered - use priority
        if self.params.oco_priority == "sl_first":
            return self._check_stop_loss(pos, brick)
        else:
            return self._check_take_profit(pos, brick)

    def _check_time_stop(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if position has been open too long."""
        if pos.bricks_open >= pos.params.time_stop_bricks:
            return self._make_result(pos, brick.close_price, PositionStatus.CLOSED_TIME, brick)
        return None

    def _check_partial_exit(self, pos: ActivePosition, brick: Brick) -> None:
        """Check for partial exit (no close - just reduces position)."""
        if pos.partial_closed or pos.params.partial_exit_pct <= 0:
            return

        current_profit = self._calc_pnl_bricks(pos, brick.close_price)
        if current_profit >= pos.params.partial_exit_bricks:
            pos.partial_closed = True
            # Reduce position by the partial exit percentage
            pos.remaining_qty = 1.0 - pos.params.partial_exit_pct

    # ── Helpers ───────────────────────────────────────────────────

    def _calc_pnl_bricks(self, pos: ActivePosition, exit_price: float) -> float:
        """Calculate P&L in brick units."""
        if pos.direction == SignalDirection.LONG:
            return (exit_price - pos.entry_price) / pos.brick_size
        else:
            return (pos.entry_price - exit_price) / pos.brick_size

    def _make_result(
        self,
        pos: ActivePosition,
        exit_price: float,
        status: PositionStatus,
        brick: Brick,
    ) -> TradeResult:
        """Create a TradeResult from the current position."""
        pnl_bricks = self._calc_pnl_bricks(pos, exit_price)
        pnl_dollars = pnl_bricks * pos.brick_size * pos.remaining_qty

        return TradeResult(
            entry_price=pos.entry_price,
            exit_price=exit_price,
            direction=pos.direction,
            pnl_bricks=round(pnl_bricks, 4),
            pnl_dollars=round(pnl_dollars, 4),
            status=status,
            entry_ts=pos.entry_ts,
            exit_ts=brick.timestamp,
            bricks_held=pos.bricks_open,
            peak_profit_bricks=pos.peak_profit_bricks,
            qty=pos.remaining_qty,
        )

    def _close_position(self, result: TradeResult) -> None:
        """Record the trade and clear the active position."""
        self.closed_trades.append(result)
        self.active_position = None

    def reset(self) -> None:
        """Reset manager state."""
        self.active_position = None
        self.closed_trades.clear()
