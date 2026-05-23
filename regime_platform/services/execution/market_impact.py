"""
Almgren-Chriss Market Impact Model.

Estimates the price impact of trading a fraction of average daily volume (ADV).
The model separates impact into:
  - Permanent impact: information-based price movement that persists
  - Temporary impact: liquidity-based price movement that dissipates

The square-root model is standard in institutional execution:
    Impact = sigma * sqrt(participation_rate) * sign * volume_factor

Reference:
  Almgren & Chriss (2000) "Optimal Execution of Portfolio Transactions"
"""

import math
from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass
class ImpactParams:
    """Parameters for the market impact model."""
    sigma_daily: float = 0.02        # Daily volatility (2% default for S&P 500)
    permanent_impact_coeff: float = 0.1   # Permanent impact coefficient (eta)
    temporary_impact_coeff: float = 0.142  # Temporary impact coefficient (sigma in AC model)
    adv_shares: float = 10_000_000    # Average daily volume in shares (default for SPY)
    avg_price: float = 450.0          # Average price (for notional ADV computation)


def compute_market_impact(
    order_shares: float,
    adv_shares: float,
    sigma_daily: float = 0.02,
    avg_price: float = 450.0,
    permanent_impact_coeff: float = 0.1,
    temporary_impact_coeff: float = 0.142,
) -> Dict[str, Any]:
    """
    Compute market impact cost using the Almgren-Chriss square-root model.
    
    Parameters
    ----------
    order_shares : float
        Number of shares in the order.
    adv_shares : float
        Average daily volume in shares.
    sigma_daily : float
        Daily volatility (as a decimal, e.g. 0.02 for 2%).
    avg_price : float
        Average price per share.
    permanent_impact_coeff : float
        Permanent impact coefficient (eta). Default 0.1 from institutional calibrations.
    temporary_impact_coeff : float
        Temporary impact coefficient. Default 0.142 from Almgren-Thum (2005) calibration.
    
    Returns
    -------
    dict with keys:
        participation_rate: float — order_size / ADV
        permanent_impact_bps: float — permanent impact in basis points
        temporary_impact_bps: float — temporary impact in basis points
        total_impact_bps: float — combined impact in basis points
        impact_cost_dollars: float — estimated dollar cost of impact
        impact_cost_pct: float — impact as percentage of order value
    """
    if adv_shares <= 0 or order_shares <= 0:
        return {
            "participation_rate": 0.0,
            "permanent_impact_bps": 0.0,
            "temporary_impact_bps": 0.0,
            "total_impact_bps": 0.0,
            "impact_cost_dollars": 0.0,
            "impact_cost_pct": 0.0,
        }
    
    participation_rate = order_shares / adv_shares
    
    # Almgren-Chriss square-root model:
    # Permanent impact: eta * sigma * sqrt(participation_rate)
    # Temporary impact: epsilon * sigma * sqrt(participation_rate)
    # Where epsilon > eta because temporary impact captures the liquidity concession
    
    sqrt_participation = math.sqrt(participation_rate)
    
    permanent_impact = permanent_impact_coeff * sigma_daily * sqrt_participation
    temporary_impact = temporary_impact_coeff * sigma_daily * sqrt_participation
    total_impact = permanent_impact + temporary_impact
    
    # Convert to basis points
    permanent_impact_bps = permanent_impact * 10_000
    temporary_impact_bps = temporary_impact * 10_000
    total_impact_bps = total_impact * 10_000
    
    # Dollar cost
    order_value = order_shares * avg_price
    impact_cost_dollars = total_impact * order_value
    impact_cost_pct = total_impact * 100
    
    return {
        "participation_rate": round(participation_rate, 6),
        "permanent_impact_bps": round(permanent_impact_bps, 2),
        "temporary_impact_bps": round(temporary_impact_bps, 2),
        "total_impact_bps": round(total_impact_bps, 2),
        "impact_cost_dollars": round(impact_cost_dollars, 2),
        "impact_cost_pct": round(impact_cost_pct, 4),
    }
