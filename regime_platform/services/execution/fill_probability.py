"""
Fill Probability Model — estimates the probability of order fills.

In reality, limit orders don't always execute. This model estimates
fill probability based on:
  - Distance from current price (farther = less likely)
  - Volatility (higher vol = more likely to touch a limit)
  - Order type (market = 100%, limit < 100%, stop = high)

The logit model provides a smooth probability curve:
    P(fill) = 1 / (1 + exp(k * (distance/sigma - threshold)))

Reference:
  Cont & de Larrard (2013) "Price Dynamics in a Markovian Limit Order Market"
"""

import math
from typing import Dict, Any, Optional
from enum import Enum


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


def compute_fill_probability(
    order_type: str,
    distance_from_mid_pct: float = 0.0,
    volatility_daily: float = 0.02,
    time_horizon_hours: float = 6.5,
    volume_participation_rate: float = 0.01,
    sensitivity: float = 5.0,
    threshold: float = 1.0,
) -> Dict[str, Any]:
    """
    Compute the probability that an order will fill.
    
    Parameters
    ----------
    order_type : str
        "market", "limit", or "stop". Market orders always fill.
    distance_from_mid_pct : float
        Distance of limit/stop price from current mid-price, as a percentage.
        e.g., 0.01 means 1% away from current price.
        For TP limit orders: |tp_price - current_price| / current_price * 100
    volatility_daily : float
        Daily volatility as a decimal (e.g., 0.02 for 2%).
    time_horizon_hours : float
        Expected time the order will be active (default 6.5 = one trading day).
    volume_participation_rate : float
        Order size as fraction of average volume. Higher = harder to fill.
    sensitivity : float
        Logit sensitivity parameter (k). Higher = sharper transition.
    threshold : float
        Distance (in volatility units) where P(fill) = 0.5.
    
    Returns
    -------
    dict with:
        fill_probability: float — P(fill) between 0 and 1
        order_type: str
        distance_from_mid_pct: float
        distance_in_sigmas: float — distance normalized by volatility
        expected_fill_price_offset_bps: float — adverse selection offset
        interpretation: str
    """
    # Market orders always fill
    if order_type == OrderType.MARKET or order_type == "market":
        return {
            "fill_probability": 1.0,
            "order_type": "market",
            "distance_from_mid_pct": 0.0,
            "distance_in_sigmas": 0.0,
            "expected_fill_price_offset_bps": 0.0,
            "interpretation": "Market order — guaranteed fill at current price.",
        }
    
    # Convert distance to volatility units
    # intraday_vol ≈ daily_vol / sqrt(6.5) for 1-hour windows
    intraday_vol = volatility_daily / math.sqrt(time_horizon_hours)
    
    # Distance in standard deviations
    distance_in_sigmas = (distance_from_mid_pct / 100) / intraday_vol if intraday_vol > 0 else float('inf')
    
    # Logit fill probability
    # P(fill) = 1 / (1 + exp(k * (distance/sigma - threshold)))
    logit_arg = sensitivity * (distance_in_sigmas - threshold)
    
    # Adjust for time horizon: longer horizon = more chance to fill
    # Scale by sqrt(time) since price扩散 is ~ sqrt(t)
    time_factor = math.sqrt(time_horizon_hours / 6.5)  # Normalized to 1 day
    adjusted_distance = distance_in_sigmas / time_factor if time_factor > 0 else distance_in_sigmas
    
    logit_arg = sensitivity * (adjusted_distance - threshold)
    fill_probability = 1.0 / (1.0 + math.exp(min(logit_arg, 500)))  # Clamp to prevent overflow
    
    # Volume participation penalty: large orders fill less easily
    volume_penalty = 1.0 - min(volume_participation_rate * 10, 0.3)  # Up to 30% penalty
    fill_probability *= volume_penalty
    
    # Adverse selection: when limit orders fill, it's often because the market
    # moved against you. Expected fill offset captures this.
    if order_type == OrderType.STOP or order_type == "stop":
        # Stop orders are market orders when triggered, so they fill but with slippage
        fill_probability = max(fill_probability, 0.90)  # Stops almost always trigger
        expected_offset = volatility_daily * 10_000 * 0.5  # ~0.5 sigma adverse
        interpretation = f"Stop order — {fill_probability:.1%} fill probability (near-certain when triggered)."
    else:
        # Limit orders: adverse selection means the fill is "too early"
        expected_offset = volatility_daily * 10_000 * 0.2  # Small adverse
        if fill_probability > 0.8:
            interpretation = f"Limit order close to market — {fill_probability:.1%} fill probability (high confidence)."
        elif fill_probability > 0.5:
            interpretation = f"Limit order moderate distance — {fill_probability:.1%} fill probability (uncertain fill)."
        elif fill_probability > 0.2:
            interpretation = f"Limit order far from market — {fill_probability:.1%} fill probability (low confidence)."
        else:
            interpretation = f"Limit order very far from market — {fill_probability:.1%} fill probability (unlikely to fill)."
    
    return {
        "fill_probability": round(fill_probability, 4),
        "order_type": order_type,
        "distance_from_mid_pct": round(distance_from_mid_pct, 4),
        "distance_in_sigmas": round(distance_in_sigmas, 4),
        "expected_fill_price_offset_bps": round(expected_offset, 2),
        "interpretation": interpretation,
    }
