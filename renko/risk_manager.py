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
    CLOSED_SL_GAP = "closed_sl_gap"     # Gap-through stop (brick opened past SL)
    CLOSED_TRAIL = "closed_trail"
    CLOSED_TIME = "closed_time"
    CLOSED_MANUAL = "closed_manual"
    CLOSED_OCO_SL = "closed_oco_sl"     # OCO: both SL+TP triggered, SL prioritised
    CLOSED_OCO_TP = "closed_oco_tp"     # OCO: both SL+TP triggered, TP prioritised


class OcoPriority(str, Enum):
    """OCO (One-Cancels-Other) resolution priority when SL and TP both trigger in the same brick."""
    SL_FIRST = "sl_first"     # Conservative: assume SL triggers first (worst case)
    TP_FIRST = "tp_first"     # Optimistic: assume TP triggers first (best case)
    WORST_CASE = "worst_case"  # Same as SL_FIRST (alias for clarity)


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
    # Execution realism parameters
    oco_priority: OcoPriority = OcoPriority.SL_FIRST  # How to resolve OCO conflicts
    # Phase 7: Advanced execution modeling parameters
    enable_market_impact: bool = False       # Enable Almgren-Chriss market impact model
    avg_daily_volume: float = 1e6           # Average daily volume (shares) for impact estimation
    impact_gamma: float = 0.314             # Almgren-Chriss permanent impact coefficient
    impact_eta: float = 0.142               # Almgren-Chriss temporary impact coefficient
    enable_fill_probability: bool = False    # Enable fill probability modeling
    enable_borrow_costs: bool = False        # Enable short borrow cost modeling
    borrow_rate_annual: float = 0.005        # Annualized borrow rate (0.5% for ETB stocks)
    hard_to_borrow: bool = False             # Mark as hard-to-borrow for higher borrow rates
    htb_premium_rate: float = 0.10           # Additional annual rate for HTB stocks (10%)
    enable_margin_costs: bool = False        # Enable margin financing cost modeling
    margin_rate_annual: float = 0.065        # Annualized margin rate (SOFR + 200bps)
    margin_requirement: float = 0.50         # Reg T initial margin requirement (50%)

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
    # Phase 7: Advanced execution modeling
    market_impact_bps: float = 0.0
    market_impact_cost: float = 0.0
    fill_probability: float = 1.0
    borrow_cost: float = 0.0
    margin_cost: float = 0.0
    dividend_cost: float = 0.0
    financing_cost_total: float = 0.0

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
            # Phase 7: Advanced execution modeling
            "market_impact_bps": self.market_impact_bps,
            "market_impact_cost": self.market_impact_cost,
            "fill_probability": self.fill_probability,
            "borrow_cost": self.borrow_cost,
            "margin_cost": self.margin_cost,
            "dividend_cost": self.dividend_cost,
            "financing_cost_total": self.financing_cost_total,
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

        Phase 3 enhancement: checks for gap-through stops (brick opens
        past the stop level) and OCO conflicts (both SL and TP triggered
        by the same brick). Gap-through fills use the brick's open price
        (worse than the stop level), which is more realistic for
        overnight gaps, earnings moves, and flash crashes.

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

        # ── Phase 3: Gap-through + OCO detection ────────────────────
        sl_triggered, sl_gap, sl_exit_price = self._is_sl_triggered(pos, brick)
        tp_triggered, tp_exit_price = self._is_tp_triggered(pos, brick)

        if sl_triggered and tp_triggered:
            # OCO conflict: both SL and TP triggered by this brick
            result = self._resolve_oco(pos, brick, sl_exit_price, tp_exit_price, sl_gap)
        elif sl_triggered:
            # SL only (possibly gap-through)
            if sl_gap:
                result = self._make_result(pos, sl_exit_price, PositionStatus.CLOSED_SL_GAP, brick)
            else:
                result = self._make_result(pos, sl_exit_price, PositionStatus.CLOSED_SL, brick)
        elif tp_triggered:
            # TP only
            result = self._make_result(pos, tp_exit_price, PositionStatus.CLOSED_TP, brick)

        # Check trailing stop (only if no SL/TP triggered)
        if result is None:
            self._update_trailing(pos, brick)
            trail_result = self._check_trailing_stop(pos, brick)
            if trail_result is not None:
                result = trail_result

        # Check time stop
        if result is None:
            result = self._check_time_stop(pos, brick)

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

    # ── Phase 3: Gap-through + OCO detection helpers ────────────

    def _is_sl_triggered(
        self, pos: ActivePosition, brick: Brick
    ) -> tuple[bool, bool, float]:
        """Check if stop-loss is triggered, accounting for gap-through.

        Returns (triggered, is_gap_through, exit_price).

        Gap-through occurs when the brick's open price has already
        passed the stop level — e.g. overnight gap, earnings move,
        flash crash. In reality, you'd get filled at the open price
        (worse than the stop), not at the stop price itself.
        """
        if pos.direction == SignalDirection.LONG:
            # Gap-through: brick opens at or below stop level
            if brick.open_price <= pos.current_sl_price:
                return True, True, brick.open_price
            # Normal: brick closes at or below stop level
            if brick.close_price <= pos.current_sl_price:
                return True, False, pos.current_sl_price
        else:
            # SHORT: gap-through: brick opens at or above stop level
            if brick.open_price >= pos.current_sl_price:
                return True, True, brick.open_price
            # Normal: brick closes at or above stop level
            if brick.close_price >= pos.current_sl_price:
                return True, False, pos.current_sl_price
        return False, False, 0.0

    def _is_tp_triggered(
        self, pos: ActivePosition, brick: Brick
    ) -> tuple[bool, float]:
        """Check if take-profit is triggered.

        Returns (triggered, exit_price).

        TP can also gap-through (brick opens past TP), but since TP
        is a limit order, gap-through is actually favorable — you get
        a better fill than the TP price. We use the open price for
        gap-through TP as well, which is more realistic.
        """
        if pos.direction == SignalDirection.LONG:
            # Gap-through TP: brick opens above TP (favorable)
            if brick.open_price >= pos.tp_price:
                return True, brick.open_price
            # Normal TP: brick closes above TP
            if brick.close_price >= pos.tp_price:
                return True, pos.tp_price
        else:
            # SHORT: gap-through TP: brick opens below TP (favorable)
            if brick.open_price <= pos.tp_price:
                return True, brick.open_price
            # Normal TP: brick closes below TP
            if brick.close_price <= pos.tp_price:
                return True, pos.tp_price
        return False, 0.0

    def _resolve_oco(
        self,
        pos: ActivePosition,
        brick: Brick,
        sl_exit_price: float,
        tp_exit_price: float,
        sl_gap: bool,
    ) -> TradeResult:
        """Resolve an OCO (One-Cancels-Other) conflict.

        When both SL and TP are triggered by the same brick, we need
        to decide which one "happened first." This is inherently
        ambiguous — we don't know intrabar price action — so we use
        a configurable priority:

          - SL_FIRST (default): Conservative — assume the worst case.
            The stop-loss triggered first. This is the standard
            approach for realistic backtesting because stops are
            market orders (immediate fill) while TPs are limit orders
            (may not fill if price gaps through).
          - TP_FIRST: Optimistic — assume TP triggered first. Only
            appropriate if you have reason to believe the price
            reached the TP level before the SL level intrabar.

        Gap-through SL always takes priority over TP regardless of
        oco_priority setting — if the bar opens past your stop, you're
        out at the open, period.
        """
        # If SL is a gap-through, SL always wins (you're stopped out
        # at the open before the bar even starts moving toward TP)
        if sl_gap:
            return self._make_result(pos, sl_exit_price, PositionStatus.CLOSED_SL_GAP, brick)

        # Resolve based on OCO priority setting
        if pos.params.oco_priority == OcoPriority.TP_FIRST:
            return self._make_result(pos, tp_exit_price, PositionStatus.CLOSED_OCO_TP, brick)
        else:
            # SL_FIRST or WORST_CASE (default) — conservative
            return self._make_result(pos, sl_exit_price, PositionStatus.CLOSED_OCO_SL, brick)

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
        if status in (PositionStatus.CLOSED_SL, PositionStatus.CLOSED_SL_GAP, PositionStatus.CLOSED_OCO_SL):
            # Stop-loss: full adverse (market order, worst execution)
            # Gap-through SL gets even worse fills but that's already
            # captured by the exit_price being at the open (worse than stop)
            slip_mult = 1.0
        elif status in (PositionStatus.CLOSED_TP, PositionStatus.CLOSED_OCO_TP):
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

        # ── Phase 7: Advanced Execution Modeling ────────────────────
        market_impact_bps = 0.0
        market_impact_cost = 0.0
        fill_probability = 1.0
        borrow_cost = 0.0
        margin_cost = 0.0
        dividend_cost = 0.0
        financing_cost_total = 0.0

        try:
            # 7A: Market Impact
            if getattr(self.params, 'enable_market_impact', False):
                from regime_platform.services.execution.market_impact import compute_market_impact
                adv = getattr(self.params, 'avg_daily_volume', 1e6)
                # Estimate daily volatility from brick size (heuristic: brick_size/price ~ daily move)
                daily_vol = pos.brick_size / pos.entry_price if pos.entry_price > 0 else 0.02
                impact = compute_market_impact(
                    shares=pos.remaining_qty,
                    price=pos.entry_price,
                    avg_daily_volume=adv,
                    daily_volatility=daily_vol,
                    execution_horizon_bars=1,
                    gamma=getattr(self.params, 'impact_gamma', 0.314),
                    eta=getattr(self.params, 'impact_eta', 0.142),
                )
                market_impact_bps = impact.total_impact_bps
                market_impact_cost = impact.impact_cost_dollars

            # 7B: Fill Probability
            if getattr(self.params, 'enable_fill_probability', False):
                from regime_platform.services.execution.fill_probability import compute_fill_probability_for_backtest
                fill_result = compute_fill_probability_for_backtest(
                    direction="LONG" if pos.direction.value == "LONG" or pos.direction.value == "long" else "SHORT",
                    entry_price=pos.entry_price,
                    market_price=pos.entry_price,
                    order_size_usd=abs(pos.entry_price * pos.remaining_qty),
                    avg_daily_volume=getattr(self.params, 'avg_daily_volume', 1e6),
                    is_limit_order=False,  # Renko uses market orders by default
                )
                fill_probability = fill_result.fill_probability

            # 7C: Borrow/Financing Costs
            direction_str = "SHORT" if pos.direction.value in ("SHORT", "short") else "LONG"
            if direction_str == "SHORT" and getattr(self.params, 'enable_borrow_costs', False):
                from regime_platform.services.execution.borrow_financing import compute_financing_for_trade
                short_value = abs(pos.entry_price * pos.remaining_qty)
                # Estimate days_held from bricks_held (assume ~3.5 bars/day for intraday)
                days_held = pos.bricks_open / 3.5 if pos.bricks_open > 0 else 1.0
                financing = compute_financing_for_trade(
                    direction=direction_str,
                    position_value=short_value,
                    days_held=days_held,
                    borrow_rate_annual=getattr(self.params, 'borrow_rate_annual', 0.005),
                    hard_to_borrow=getattr(self.params, 'hard_to_borrow', False),
                    htb_premium_rate=getattr(self.params, 'htb_premium_rate', 0.10),
                )
                borrow_cost = financing.borrow_cost.borrow_cost_dollars if financing.borrow_cost else 0.0
                dividend_cost = financing.dividend_cost_dollars
                financing_cost_total = financing.total_financing_cost_dollars

            if getattr(self.params, 'enable_margin_costs', False):
                from regime_platform.services.execution.borrow_financing import compute_margin_cost
                pos_value = abs(pos.entry_price * pos.remaining_qty)
                days_held = pos.bricks_open / 3.5 if pos.bricks_open > 0 else 1.0
                margin_result = compute_margin_cost(
                    position_value=pos_value,
                    margin_requirement=getattr(self.params, 'margin_requirement', 0.50),
                    margin_rate_annual=getattr(self.params, 'margin_rate_annual', 0.065),
                    days_held=days_held,
                )
                margin_cost = margin_result.margin_cost_dollars
                financing_cost_total += margin_cost

        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(f"Phase 7 execution modeling failed: {exc}")

        total_cost_extended = total_cost + market_impact_cost + financing_cost_total

        return TradeResult(
            entry_price=pos.entry_price,
            exit_price=filled_exit,
            direction=pos.direction,
            pnl_bricks=round(pnl_bricks, 4),
            pnl_dollars=round(pnl_dollars - total_cost - market_impact_cost - financing_cost_total, 4),
            status=status,
            entry_ts=pos.entry_ts,
            exit_ts=brick.timestamp,
            bricks_held=pos.bricks_open,
            peak_profit_bricks=pos.peak_profit_bricks,
            qty=pos.remaining_qty,
            commission=round(commission, 4),
            slippage_cost=round(total_slippage, 4),
            total_cost=round(total_cost_extended, 4),
            entry_slippage=round(entry_slip, 4),
            exit_slippage=round(exit_slip * pos.remaining_qty, 4),
            gross_pnl_dollars=round(gross_pnl_dollars, 4),
            # Phase 7: Advanced execution modeling
            market_impact_bps=round(market_impact_bps, 4),
            market_impact_cost=round(market_impact_cost, 4),
            fill_probability=round(fill_probability, 6),
            borrow_cost=round(borrow_cost, 4),
            margin_cost=round(margin_cost, 4),
            dividend_cost=round(dividend_cost, 4),
            financing_cost_total=round(financing_cost_total, 4),
        )

    def _close_position(self, result: TradeResult) -> None:
        """Record the trade and clear the active position."""
        self.closed_trades.append(result)
        self.active_position = None

    def reset(self) -> None:
        """Reset manager state."""
        self.active_position = None
        self.closed_trades.clear()
