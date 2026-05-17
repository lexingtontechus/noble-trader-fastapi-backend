"""
Configuration dataclasses for the Renko HFT module.

All tunable parameters are centralised here so the pipeline
can be optimised via walk-forward analysis or the GA strategy
evolution module.
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class BrickSizeMode(str, Enum):
    """How the brick size is determined."""
    FIXED = "fixed"
    ATR = "atr"
    DYNAMIC = "dynamic"


class OrderType(str, Enum):
    """Order type for execution."""
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class RenkoConfig:
    """Master configuration for the Renko HFT pipeline."""

    # ── Brick Engine ──────────────────────────────────────────────
    brick_size_mode: BrickSizeMode = BrickSizeMode.FIXED
    brick_size: float = 0.50           # Fixed dollar brick size
    atr_period: int = 14               # Lookback for ATR-based brick sizing
    atr_multiplier: float = 1.0        # ATR × multiplier = brick size
    reversal_bricks: int = 2           # Classic Renko: 2-brick reversal

    # ── Swing Classifier ──────────────────────────────────────────
    swing_lookback: int = 3            # Bricks to confirm a swing point
    min_swing_distance_bricks: int = 2  # Min bricks between swing highs/lows

    # ── Pattern Detector ──────────────────────────────────────────
    bull_trigger_n: int = 3            # N consecutive HH/HL for bull trigger
    bear_trigger_n: int = 3            # N consecutive LL/LH for bear trigger
    velocity_threshold: float = 0.0    # Min bricks/sec to boost signal (0 = off)
    double_top_bricks: int = 6         # Lookback for double-top/bottom patterns
    consolidation_max_mix: float = 0.4 # Max ratio of opposite labels in a run

    # ── Signal Filter ─────────────────────────────────────────────
    session_start: str = "09:35"       # ET, skip opening chaos
    session_end: str = "15:45"         # ET, skip closing auction
    skip_lunch: bool = True            # Skip 11:30–13:00 ET
    lunch_start: str = "11:30"
    lunch_end: str = "13:00"
    max_trades_per_session: int = 15   # Hard cap on daily trades
    max_daily_loss_bricks: float = 10  # Stop trading after N bricks lost
    max_consecutive_losses: int = 3    # Pause after N consecutive losses
    cooldown_seconds: float = 30.0     # Min time between trades
    min_brick_velocity: float = 0.0    # Min bricks/min to allow trading
    regime_gate: bool = True           # Only trade with HMM regime alignment

    # ── Risk Manager ──────────────────────────────────────────────
    sl_bricks: int = 3                 # Stop-loss in brick units
    tp_bricks: int = 5                 # Take-profit in brick units
    trailing_stop: bool = True         # Enable trailing stop
    trail_after_bricks: int = 3        # Start trailing after N bricks profit
    trail_distance_bricks: int = 2     # Trail by N bricks behind peak
    time_stop_bricks: int = 10         # Close if open for N+ bricks without TP/SL
    partial_exit_pct: float = 0.0      # 0 = no partial exit; 0.5 = close half at TP/2
    partial_exit_bricks: int = 3       # Bricks of profit for partial exit

    # ── Executor ──────────────────────────────────────────────────
    order_type: OrderType = OrderType.MARKET
    use_bracket_orders: bool = True    # Alpaca native bracket orders
    paper_trading: bool = True         # Default to paper
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    base_url: str = "https://paper-api.alpaca.markets"

    # ── Position Sizing ───────────────────────────────────────────
    kelly_fraction: float = 0.5        # Half-Kelly by default
    max_position_pct: float = 0.10     # Max 10 % of equity per trade
    min_position_usd: float = 50.0     # Minimum order value
    default_win_rate: float = 0.55     # Fallback WR for Kelly when no history

    # ── Pipeline ──────────────────────────────────────────────────
    symbol: str = "SPY"
    timezone: str = "America/New_York"

    def get_brick_size(self, atr_value: Optional[float] = None) -> float:
        """Compute the effective brick size based on mode."""
        if self.brick_size_mode == BrickSizeMode.FIXED:
            return self.brick_size
        elif self.brick_size_mode == BrickSizeMode.ATR:
            if atr_value is None:
                return self.brick_size  # fallback
            return round(atr_value * self.atr_multiplier, 4)
        else:  # DYNAMIC — caller must override
            return self.brick_size

    def to_dict(self) -> dict:
        """Serialise config to a plain dict (for API responses / logging)."""
        import dataclasses
        return dataclasses.asdict(self)
