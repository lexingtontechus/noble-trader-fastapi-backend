"""
Risk Manager — brick-denominated risk management for Renko HFT.

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
    # Transaction cost parameters
    slippage_bps: float = 2.0       # Slippage in basis points (adverse fill)
    commission_bps: float = 5.0     # Commission in basis points of position value
    spread_bps: float = 1.0         # Bid-ask spread in basis points (half-spread each side)

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
    entry_slippage: float = 0.0      # Slippage cost on entry fill

    def __post_init__(self):
        """Calculate initial SL/TP prices.

        SL/TP are computed from the entry_price (which already includes
        any entry slippage applied by the RiskManager at open time).
        """
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
    # Transaction cost tracking
    commission: float = 0.0          # Total commission for this trade (both entry + exit)
    slippage_cost: float = 0.0       # Total slippage cost (entry + exit)
    total_cost: float = 0.0          # commission + slippage_cost
    entry_slippage: float = 0.0      # Slippage on entry fill
    exit_slippage: float = 0.0       # Slippage on exit fill
    gross_pnl_dollars: float = 0.0   # P&L before transaction costs

    @property
    def is_win(self) -> bool:
        return self.pnl_bricks > 0

    @property
    def hold_duration_seconds(self) -> float:
        return self.exit_ts - self.entry_ts

    @property
    def net_pnl_dollars(self) -> float:
        """P&L after all transaction costs."""
        return self.pnl_dollars - self.total_cost

    def to_dict(self) -> dict:
        return {
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "direction": self.direction.value,
            "pnl_bricks": self.pnl_bricks,
            "pnl_dollars": self.pnl_dollars,
            "gross_pnl_dollars": self.gross_pnl_dollars,
            "status": self.status.value,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "bricks_held": self.bricks_held,
            "peak_profit_bricks": self.peak_profit_bricks,
            "qty": self.qty,
            "commission": self.commission,
            "slippage_cost": self.slippage_cost,
            "total_cost": self.total_cost,
            "entry_slippage": self.entry_slippage,
            "exit_slippage": self.exit_slippage,
            "net_pnl_dollars": self.net_pnl_dollars,
        }


# ── Risk Manager ───────────────────────────────────────────────────


class RiskManager:
    """
    Manages open positions and enforces brick-denominated risk rules.

    Call ``evaluate()`` on every new brick to check SL/TP/trailing/
    time stops. The manager returns a TradeResult when a position
    should be closed.
    """

    def __init__(self, params: Optional[RiskParams] = None):
        self.params = params or RiskParams()
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

        Applies entry slippage and half-spread cost to the fill price.
        The adjusted price becomes the position's entry_price, and
        SL/TP levels are computed from it (in __post_init__).

        Returns the ActivePosition for reference.
        """
        if self.active_position is not None:
            raise RuntimeError("Cannot open position: existing position is open")

        effective_params = params or self.params

        # ── Apply entry slippage + half-spread ──────────────────────
        filled_price = entry_price
        entry_slippage = 0.0

        if effective_params.slippage_bps > 0 or effective_params.spread_bps > 0:
            slip_bps = effective_params.slippage_bps / 10000
            spread_bps = effective_params.spread_bps / 20000  # half-spread on entry

            if direction == SignalDirection.LONG:
                # Long entry: buy at the ask → price goes UP
                filled_price = entry_price * (1 + slip_bps + spread_bps)
            else:
                # Short entry: sell at the bid → price goes DOWN
                filled_price = entry_price * (1 - slip_bps - spread_bps)

            entry_slippage = abs(filled_price - entry_price) * qty

        self.active_position = ActivePosition(
            entry_price=filled_price,
            direction=direction,
            brick_size=brick_size,
            entry_ts=entry_ts,
            entry_brick_idx=entry_brick_idx,
            qty=qty,
            params=effective_params,
        )
        self.active_position.entry_slippage = entry_slippage
        return self.active_position

    def evaluate(self, brick: Brick) -> Optional[TradeResult]:
        """
        Evaluate the current position against the latest brick.

        Call this on EVERY new brick. Returns a TradeResult if the
        position should be closed, None otherwise.

        Parameters
        ----------
        brick : Brick
            The latest Renko brick.

        Returns
        -------
        TradeResult or None
        """
        pos = self.active_position
        if pos is None:
            return None

        pos.bricks_open += 1
        result = None

        # Check SL
        result = result or self._check_stop_loss(pos, brick)

        # Check TP
        result = result or self._check_take_profit(pos, brick)

        # Check trailing stop (only if not already triggered)
        if result is None:
            self._update_trailing(pos, brick)
            result = self._check_trailing_stop(pos, brick)

        # Check time stop
        result = result or self._check_time_stop(pos, brick)

        # Check partial exit
        if result is None:
            self._check_partial_exit(pos, brick)

        if result is not None:
            self._close_position(result)

        return result

    def force_close(self, brick: Brick) -> TradeResult:
        """Force close the current position at the brick's price (with slippage)."""
        pos = self.active_position
        if pos is None:
            raise RuntimeError("No position to close")

        # Use _make_result to apply slippage and commission consistently
        return self._make_result(pos, brick.close_price, PositionStatus.CLOSED_MANUAL, brick)

    @property
    def has_position(self) -> bool:
        return self.active_position is not None

    @property
    def current_pnl_bricks(self) -> float:
        """Unrealised P&L in bricks (requires latest price)."""
        if self.active_position is None:
            return 0.0
        # Use current SL as reference for unrealised — caller should
        # call evaluate() for accurate P&L
        return self.active_position.peak_profit_bricks

    # ── Internal checks ───────────────────────────────────────────

    def _check_stop_loss(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if stop-loss has been hit."""
        if pos.direction == SignalDirection.LONG:
            if brick.close_price <= pos.current_sl_price:
                return self._make_result(pos, pos.current_sl_price, PositionStatus.CLOSED_SL, brick)
        else:
            if brick.close_price >= pos.current_sl_price:
                return self._make_result(pos, pos.current_sl_price, PositionStatus.CLOSED_SL, brick)
        return None

    def _check_take_profit(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if take-profit has been reached."""
        if pos.direction == SignalDirection.LONG:
            if brick.close_price >= pos.tp_price:
                return self._make_result(pos, pos.tp_price, PositionStatus.CLOSED_TP, brick)
        else:
            if brick.close_price <= pos.tp_price:
                return self._make_result(pos, pos.tp_price, PositionStatus.CLOSED_TP, brick)
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

        # The trailing stop IS the current SL — if SL is hit and
        # trailing is active, we label it as a trailing exit
        if pos.direction == SignalDirection.LONG:
            if brick.close_price <= pos.current_sl_price and pos.current_sl_price > pos.sl_price:
                return self._make_result(pos, pos.current_sl_price, PositionStatus.CLOSED_TRAIL, brick)
        else:
            if brick.close_price >= pos.current_sl_price and pos.current_sl_price < pos.sl_price:
                return self._make_result(pos, pos.current_sl_price, PositionStatus.CLOSED_TRAIL, brick)
        return None

    def _check_time_stop(self, pos: ActivePosition, brick: Brick) -> Optional[TradeResult]:
        """Check if position has been open too long."""
        if pos.bricks_open >= pos.params.time_stop_bricks:
            return self._make_result(pos, brick.close_price, PositionStatus.CLOSED_TIME, brick)
        return None

    def _check_partial_exit(self, pos: ActivePosition, brick: Brick) -> None:
        """Check for partial exit (no close — just reduces position)."""
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

    def _apply_exit_slippage(
        self,
        exit_price: float,
        direction: SignalDirection,
        status: PositionStatus,
    ) -> tuple:
        """Apply slippage + half-spread to the exit price based on direction and close reason.

        Rules (from Phase 2 roadmap):
          - SL exit: ADVERSE fill (full slippage — stop is a market order)
          - TP exit: slight adverse (half slippage — limit order usually fills at limit)
          - Trail/Time/Manual: moderate adverse (0.75× slippage)
          - Half-spread is always applied on exit (you sell at the bid, buy at the ask)

        Returns (filled_exit_price, exit_slippage_cost)
        """
        if self.params.slippage_bps <= 0 and self.params.spread_bps <= 0:
            return exit_price, 0.0

        slip_bps = self.params.slippage_bps / 10000 if self.params.slippage_bps > 0 else 0.0
        spread_bps = self.params.spread_bps / 20000 if self.params.spread_bps > 0 else 0.0  # half-spread on exit

        # Determine slippage multiplier based on exit type
        if status == PositionStatus.CLOSED_SL:
            # Stop-loss: full adverse (market order, worst execution)
            slip_mult = 1.0
        elif status == PositionStatus.CLOSED_TP:
            # Take-profit: half adverse (limit order, usually fills at or near limit)
            slip_mult = 0.5
        else:
            # Trailing stop, time stop, manual: moderate adverse
            slip_mult = 0.75

        if direction == SignalDirection.LONG:
            # Long exit: sell at the bid → price goes DOWN
            filled = exit_price * (1 - slip_bps * slip_mult - spread_bps)
        else:
            # Short exit: buy at the ask → price goes UP
            filled = exit_price * (1 + slip_bps * slip_mult + spread_bps)

        exit_slip_cost = abs(filled - exit_price)
        return filled, exit_slip_cost

    def _calc_commission(
        self,
        entry_price: float,
        exit_price: float,
        qty: float = 1.0,
    ) -> float:
        """Calculate total round-trip commission (entry + exit)."""
        if self.params.commission_bps <= 0:
            return 0.0
        entry_value = abs(entry_price * qty)
        exit_value = abs(exit_price * qty)
        return (entry_value + exit_value) * self.params.commission_bps / 10000

    def _make_result(
        self,
        pos: ActivePosition,
        exit_price: float,
        status: PositionStatus,
        brick: Brick,
    ) -> TradeResult:
        """Create a TradeResult from the current position.

        Applies slippage to the exit price based on the close reason
        (SL gets worst fill, TP gets slight adverse, trail/time moderate).
        Tracks all transaction costs separately for transparency.
        """
        # ── Gross P&L (before costs, without slippage) ──────────────
        gross_pnl_bricks = self._calc_pnl_bricks(pos, exit_price)
        gross_pnl_dollars = gross_pnl_bricks * pos.brick_size * pos.remaining_qty

        # ── Apply exit slippage ─────────────────────────────────────
        filled_exit, exit_slip = self._apply_exit_slippage(
            exit_price, pos.direction, status,
        )

        # ── Net P&L (after slippage, before commission) ─────────────
        pnl_bricks = self._calc_pnl_bricks(pos, filled_exit)
        pnl_dollars = pnl_bricks * pos.brick_size * pos.remaining_qty

        # ── Commission (on entry + exit notional) ───────────────────
        commission = self._calc_commission(
            pos.entry_price, filled_exit, pos.remaining_qty,
        )

        # ── Total costs ─────────────────────────────────────────────
        entry_slip = getattr(pos, 'entry_slippage', 0.0)
        total_slippage = entry_slip + (exit_slip * pos.remaining_qty)
        total_cost = commission + total_slippage

        return TradeResult(
            entry_price=pos.entry_price,
            exit_price=filled_exit,
            direction=pos.direction,
            pnl_bricks=round(pnl_bricks, 4),
            pnl_dollars=round(pnl_dollars - total_cost, 4),
            status=status,
            entry_ts=pos.entry_ts,
            exit_ts=brick.timestamp,
            bricks_held=pos.bricks_open,
            peak_profit_bricks=pos.peak_profit_bricks,
            qty=pos.remaining_qty,
            commission=round(commission, 4),
            slippage_cost=round(total_slippage, 4),
            total_cost=round(total_cost, 4),
            entry_slippage=round(entry_slip, 4),
            exit_slippage=round(exit_slip * pos.remaining_qty, 4),
            gross_pnl_dollars=round(gross_pnl_dollars, 4),
        )

    def _close_position(self, result: TradeResult) -> None:
        """Record the trade and clear the active position."""
        self.closed_trades.append(result)
        self.active_position = None

    def reset(self) -> None:
        """Reset manager state."""
        self.active_position = None
        self.closed_trades.clear()
