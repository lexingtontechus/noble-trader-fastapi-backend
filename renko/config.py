"""
Configuration dataclasses for the Renko HFT module.

All tunable parameters are centralised here so the pipeline
can be optimised via walk-forward analysis or the GA strategy
evolution module.

Configuration resolution order (highest priority wins):
  1. Explicit constructor arguments (per-pipeline overrides)
  2. Runtime config DB overrides (admin-adjustable via /config API)
  3. Environment variables (e.g. RENKO_BRICK_SIZE)
  4. Hardcoded defaults (the dataclass defaults below)
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
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

    # ── Execution Control ──────────────────────────────────────────
    dry_run: bool = True               # Default True — never execute real orders without explicit opt-in
    live_enabled: bool = False         # Master switch: must be True for any real order submission

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

    # ── Transaction Cost Modeling (Phase 2) ────────────────────────
    slippage_bps: float = 2.0            # Slippage in basis points
    commission_bps: float = 5.0          # Commission in basis points
    spread_bps: float = 1.0             # Bid-ask spread in basis points (half-spread each side)

    # ── Execution Realism (Phase 3) ────────────────────────────────
    oco_priority: str = "sl_first"       # When SL+TP both trigger: "sl_first" (conservative) or "tp_first"

    # ── Execution Modeling (Phase 7) ───────────────────────────────
    market_impact_mode: str = "none"     # "none" (backward-compatible) or "almgren_chriss"
    adv_shares: float = 10_000_000       # Average daily volume in shares
    fill_probability_mode: str = "always_fill"  # "always_fill" or "realistic"
    borrow_rate_bps: float = 50.0        # Annualized short borrow cost
    margin_rate_bps: float = 150.0       # Annualized margin rate for leveraged longs
    is_hard_to_borrow: bool = False      # Whether stock is hard-to-borrow
    dividend_yield_bps: float = 200.0    # Annualized dividend yield
    initial_capital: float = 100_000.0   # Starting capital for dollar P&L

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

    @classmethod
    async def from_runtime_config(cls, overrides: Optional[Dict[str, Any]] = None) -> "RenkoConfig":
        """
        Build a RenkoConfig with values from the runtime configuration system.

        Resolution: explicit overrides → DB/env runtime config → dataclass defaults.

        This is the recommended way to create a RenkoConfig in async contexts
        (FastAPI endpoints, pipeline creation). The sync dataclass constructor
        still works for backwards compatibility and non-async code.
        """
        from regime_platform.core.runtime_config import runtime_config

        # Map RenkoConfig field names → runtime_config keys
        key_map: Dict[str, str] = {
            "brick_size": "renko.brick_size",
            "brick_size_mode": "renko.brick_size_mode",
            "atr_period": "renko.atr_period",
            "atr_multiplier": "renko.atr_multiplier",
            "reversal_bricks": "renko.reversal_bricks",
            "swing_lookback": "renko.swing_lookback",
            "min_swing_distance_bricks": "renko.min_swing_distance",
            "bull_trigger_n": "renko.bull_trigger_n",
            "bear_trigger_n": "renko.bear_trigger_n",
            "double_top_bricks": "renko.double_top_bricks",
            "consolidation_max_mix": "renko.consolidation_max_mix",
            "session_start": "renko.session_start",
            "session_end": "renko.session_end",
            "skip_lunch": "renko.skip_lunch",
            "lunch_start": "renko.lunch_start",
            "lunch_end": "renko.lunch_end",
            "max_trades_per_session": "renko.max_trades_per_session",
            "max_daily_loss_bricks": "renko.max_daily_loss_bricks",
            "max_consecutive_losses": "renko.max_consecutive_losses",
            "cooldown_seconds": "renko.cooldown_seconds",
            "regime_gate": "renko.regime_gate",
            "sl_bricks": "renko.sl_bricks",
            "tp_bricks": "renko.tp_bricks",
            "trailing_stop": "renko.trailing_stop",
            "trail_after_bricks": "renko.trail_after_bricks",
            "trail_distance_bricks": "renko.trail_distance_bricks",
            "time_stop_bricks": "renko.time_stop_bricks",
            "partial_exit_pct": "renko.partial_exit_pct",
            "partial_exit_bricks": "renko.partial_exit_bricks",
            "kelly_fraction": "renko.kelly_fraction",
            "max_position_pct": "renko.max_position_pct",
            "min_position_usd": "renko.min_position_usd",
            "default_win_rate": "renko.default_win_rate",
            "slippage_bps": "renko.slippage_bps",
            "commission_bps": "renko.commission_bps",
            "spread_bps": "renko.spread_bps",
            "oco_priority": "renko.oco_priority",
            "market_impact_mode": "renko.market_impact_mode",
            "adv_shares": "renko.adv_shares",
            "fill_probability_mode": "renko.fill_probability_mode",
            "borrow_rate_bps": "renko.borrow_rate_bps",
            "margin_rate_bps": "renko.margin_rate_bps",
            "is_hard_to_borrow": "renko.is_hard_to_borrow",
            "dividend_yield_bps": "renko.dividend_yield_bps",
            "initial_capital": "renko.initial_capital",
            "symbol": "renko.default_symbol",
            "timezone": "renko.timezone",
        }

        # Type map: field name → getter method on runtime_config
        type_map: Dict[str, str] = {
            "brick_size": "get_float",
            "brick_size_mode": "get_str",
            "atr_period": "get_int",
            "atr_multiplier": "get_float",
            "reversal_bricks": "get_int",
            "swing_lookback": "get_int",
            "min_swing_distance_bricks": "get_int",
            "bull_trigger_n": "get_int",
            "bear_trigger_n": "get_int",
            "double_top_bricks": "get_int",
            "consolidation_max_mix": "get_float",
            "session_start": "get_str",
            "session_end": "get_str",
            "skip_lunch": "get_bool",
            "lunch_start": "get_str",
            "lunch_end": "get_str",
            "max_trades_per_session": "get_int",
            "max_daily_loss_bricks": "get_float",
            "max_consecutive_losses": "get_int",
            "cooldown_seconds": "get_float",
            "regime_gate": "get_bool",
            "sl_bricks": "get_int",
            "tp_bricks": "get_int",
            "trailing_stop": "get_bool",
            "trail_after_bricks": "get_int",
            "trail_distance_bricks": "get_int",
            "time_stop_bricks": "get_int",
            "partial_exit_pct": "get_float",
            "partial_exit_bricks": "get_int",
            "kelly_fraction": "get_float",
            "max_position_pct": "get_float",
            "min_position_usd": "get_float",
            "default_win_rate": "get_float",
            "slippage_bps": "get_float",
            "commission_bps": "get_float",
            "spread_bps": "get_float",
            "oco_priority": "get_str",
            "market_impact_mode": "get_str",
            "adv_shares": "get_float",
            "fill_probability_mode": "get_str",
            "borrow_rate_bps": "get_float",
            "margin_rate_bps": "get_float",
            "is_hard_to_borrow": "get_bool",
            "dividend_yield_bps": "get_float",
            "initial_capital": "get_float",
            "symbol": "get_str",
            "timezone": "get_str",
        }

        # Build a default instance to get fallback values
        defaults = cls()
        kwargs: Dict[str, Any] = {}

        for field_name, config_key in key_map.items():
            # If caller provided an explicit override, use it
            if overrides and field_name in overrides:
                kwargs[field_name] = overrides[field_name]
                continue

            # Otherwise, resolve from runtime config
            default_val = getattr(defaults, field_name)
            getter_name = type_map.get(field_name, "get")
            getter = getattr(runtime_config, getter_name)
            kwargs[field_name] = await getter(config_key, default=default_val)

        # Handle enum fields
        if isinstance(kwargs.get("brick_size_mode"), str):
            kwargs["brick_size_mode"] = BrickSizeMode(kwargs["brick_size_mode"])

        return cls(**kwargs)
