"""
Aggression Dispatcher - maps EV → execution strategy.

Maps ev_per_dollar to an execution strategy that determines:
  - Order type (limit vs market)
  - Limit offset ticks (how deep to place limit orders)
  - Patience bars (how long to wait for fill before escalating)
  - EV-scaled Kelly factor
  - TTM (Time-to-Market) decay settings

Volatility regime scales patience:
  Low vol  → be patient (10 bars)
  High vol → fill fast or bail (2 bars)
"""

from dataclasses import dataclass


@dataclass
class DispatchResult:
    """Result of the aggression dispatch computation."""
    aggression: str         # "passive" | "mid" | "aggressive"
    order_type: str         # "limit" | "market"
    limit_offset_ticks: int # Number of ticks from mid for limit orders
    patience_bars: int      # Max bars to wait for fill before escalating
    ev_per_dollar: float
    ev_scale: float         # Kelly scaling factor
    
    # TTM (Time-to-Market) decay
    ttm_enabled: bool = True
    ttm_decay_ticks: int = 1   # Tighten by this many ticks per step
    ttm_max_steps: int = 3     # Steps before converting to market


class AggressionDispatcher:
    """Maps EV → execution strategy (order type, limit offset, patience)."""
    
    # Regime-scaled patience (bars to wait for fill)
    PATIENCE_BY_VOL = {
        "low": 10,       # Low vol — be patient
        "med_low": 7,
        "med": 5,
        "med_high": 3,
        "high": 2,       # High vol — fill fast or bail
    }
    
    def dispatch(
        self,
        ev_per_dollar: float,
        vol_state: str = "med",
        base_kelly: float = 0.0,
    ) -> DispatchResult:
        """
        Map ev_per_dollar to an execution strategy.
        
        Parameters
        ----------
        ev_per_dollar : float
            Expected value per dollar of risk.
        vol_state : str
            Current volatility regime ("low", "med_low", "med", "med_high", "high").
        base_kelly : float
            Base Kelly fraction (before EV scaling).
        
        Returns
        -------
        DispatchResult
            Execution strategy with order type, offset, patience, and TTM settings.
        """
        # Step 1: Determine aggression level
        if ev_per_dollar > 0.40:
            aggression = "passive"
            order_type = "limit"
            limit_offset_ticks = 3   # Deep limit (1.5× typical spread)
            patience_key = vol_state
        elif ev_per_dollar > 0.15:
            aggression = "mid"
            order_type = "limit"
            limit_offset_ticks = 1   # Mid-point limit
            patience_key = vol_state
        else:
            aggression = "aggressive"
            order_type = "market"
            limit_offset_ticks = 0   # Market order
            patience_key = "high"    # No patience needed — immediate fill
        
        patience_bars = self.PATIENCE_BY_VOL.get(patience_key, 5)
        
        # Step 2: EV-scaled Kelly
        ev_scale = min(1.0, ev_per_dollar / 0.50) if ev_per_dollar > 0 else 0
        
        # Step 3: TTM decay settings
        ttm_enabled = aggression != "aggressive"  # No decay for market orders
        ttm_decay_ticks = 1
        ttm_max_steps = 3 if aggression == "passive" else 2
        
        return DispatchResult(
            aggression=aggression,
            order_type=order_type,
            limit_offset_ticks=limit_offset_ticks,
            patience_bars=patience_bars,
            ev_per_dollar=ev_per_dollar,
            ev_scale=ev_scale,
            ttm_enabled=ttm_enabled,
            ttm_decay_ticks=ttm_decay_ticks,
            ttm_max_steps=ttm_max_steps,
        )
