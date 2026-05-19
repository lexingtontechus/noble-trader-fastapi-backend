"""
Borrowing & Financing Costs for short and leveraged positions.

Short selling is NOT free:
  - Borrow costs: fee paid to the lender for borrowing shares
  - Margin interest: cost of maintaining the margin requirement
  - Dividend costs: short seller must pay dividends to the lender

Leveraged longs also incur margin interest:
  - Broker call rate (typically 1-2% above Fed Funds)

These costs can be the difference between a profitable short strategy
and an unprofitable one, especially for strategies with long holding periods.
"""

import math
from typing import Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class FinancingParams:
    """Default financing parameters."""
    borrow_rate_bps: float = 50.0      # Annualized borrow cost (0.50% for easy-to-borrow)
    margin_rate_bps: float = 150.0     # Annualized margin rate (1.50%, approx broker call rate)
    hard_to_borrow_premium_bps: float = 500.0  # Additional cost for HTB stocks (5%)
    dividend_yield_bps: float = 200.0  # Average dividend yield (2.0%)
    trading_days_per_year: int = 252


def compute_borrow_cost(
    position_notional: float,
    holding_days: float,
    borrow_rate_bps: float = 50.0,
    is_hard_to_borrow: bool = False,
    hard_to_borrow_premium_bps: float = 500.0,
) -> Dict[str, Any]:
    """
    Compute the borrow cost for a short position.
    
    Parameters
    ----------
    position_notional : float
        Dollar value of the short position.
    holding_days : float
        Number of days the position is held (can be fractional).
    borrow_rate_bps : float
        Annualized borrow rate in basis points.
    is_hard_to_borrow : bool
        Whether the stock is hard-to-borrow (HTB).
    hard_to_borrow_premium_bps : float
        Additional annualized cost for HTB stocks.
    
    Returns
    -------
    dict with daily and total borrow costs.
    """
    effective_rate_bps = borrow_rate_bps
    if is_hard_to_borrow:
        effective_rate_bps += hard_to_borrow_premium_bps
    
    daily_rate = effective_rate_bps / 10_000 / 252
    daily_cost = position_notional * daily_rate
    total_cost = daily_cost * holding_days
    
    return {
        "daily_borrow_cost": round(daily_cost, 4),
        "total_borrow_cost": round(total_cost, 2),
        "effective_annual_rate_bps": round(effective_rate_bps, 2),
        "is_hard_to_borrow": is_hard_to_borrow,
        "holding_days": holding_days,
        "position_notional": position_notional,
    }


def compute_margin_cost(
    borrowed_amount: float,
    holding_days: float,
    margin_rate_bps: float = 150.0,
) -> Dict[str, Any]:
    """
    Compute the margin interest cost for a leveraged long position.
    
    Parameters
    ----------
    borrowed_amount : float
        Dollar amount borrowed on margin.
    holding_days : float
        Number of days the margin loan is outstanding.
    margin_rate_bps : float
        Annualized margin rate in basis points (default 150 = 1.50%).
    
    Returns
    -------
    dict with daily and total margin costs.
    """
    daily_rate = margin_rate_bps / 10_000 / 252
    daily_cost = borrowed_amount * daily_rate
    total_cost = daily_cost * holding_days
    
    return {
        "daily_margin_cost": round(daily_cost, 4),
        "total_margin_cost": round(total_cost, 2),
        "margin_rate_bps": margin_rate_bps,
        "holding_days": holding_days,
        "borrowed_amount": borrowed_amount,
    }


def compute_dividend_cost_short(
    position_notional: float,
    dividend_yield_bps: float = 200.0,
    holding_days: float = 0.0,
) -> Dict[str, Any]:
    """
    Compute the dividend cost for a short position.
    
    When short, you OWE the dividend to the lender. This is a real cost
    that must be tracked.
    
    Parameters
    ----------
    position_notional : float
        Dollar value of the short position.
    dividend_yield_bps : float
        Annualized dividend yield in basis points.
    holding_days : float
        Number of days held.
    
    Returns
    -------
    dict with dividend cost information.
    """
    daily_dividend_rate = dividend_yield_bps / 10_000 / 252
    daily_cost = position_notional * daily_dividend_rate
    total_cost = daily_cost * holding_days
    
    # Probability of an ex-dividend date during holding period
    # Approximate: quarterly dividends, so ~4 per year
    n_quarters = holding_days / 63  # ~63 trading days per quarter
    prob_dividend = 1 - math.exp(-n_quarters) if n_quarters > 0 else 0
    
    return {
        "daily_dividend_cost": round(daily_cost, 4),
        "total_dividend_cost": round(total_cost, 2),
        "dividend_yield_bps": dividend_yield_bps,
        "prob_ex_dividend_during_hold": round(min(prob_dividend, 1.0), 4),
    }


def compute_total_financing_costs(
    position_notional: float,
    holding_days: float,
    direction: str = "short",
    is_hard_to_borrow: bool = False,
    leverage_ratio: float = 1.0,
    borrow_rate_bps: float = 50.0,
    margin_rate_bps: float = 150.0,
    dividend_yield_bps: float = 200.0,
    hard_to_borrow_premium_bps: float = 500.0,
) -> Dict[str, Any]:
    """
    Compute the total financing costs for a position.
    
    Parameters
    ----------
    position_notional : float
        Dollar value of the position.
    holding_days : float
        Number of days held.
    direction : str
        "long" or "short".
    is_hard_to_borrow : bool
        Whether the stock is HTB (only applies to shorts).
    leverage_ratio : float
        Leverage ratio (1.0 = no leverage, 2.0 = 50% margin).
    borrow_rate_bps : float
        Annualized borrow rate for shorts.
    margin_rate_bps : float
        Annualized margin rate for leveraged longs.
    dividend_yield_bps : float
        Annualized dividend yield.
    hard_to_borrow_premium_bps : float
        Additional cost for HTB stocks.
    
    Returns
    -------
    Combined dict with all financing cost components.
    """
    total_cost = 0.0
    components = {}
    
    if direction == "short":
        # Borrow cost
        borrow = compute_borrow_cost(
            position_notional, holding_days, borrow_rate_bps,
            is_hard_to_borrow, hard_to_borrow_premium_bps,
        )
        components["borrow"] = borrow
        total_cost += borrow["total_borrow_cost"]
        
        # Dividend cost (short seller owes dividends)
        dividend = compute_dividend_cost_short(
            position_notional, dividend_yield_bps, holding_days,
        )
        components["dividend"] = dividend
        total_cost += dividend["total_dividend_cost"]
    
    if direction == "long" and leverage_ratio > 1.0:
        # Margin interest on the borrowed portion
        borrowed_amount = position_notional * (1 - 1.0 / leverage_ratio)
        margin = compute_margin_cost(
            borrowed_amount, holding_days, margin_rate_bps,
        )
        components["margin"] = margin
        total_cost += margin["total_margin_cost"]
    
    return {
        "direction": direction,
        "position_notional": position_notional,
        "holding_days": holding_days,
        "total_financing_cost": round(total_cost, 2),
        "daily_financing_cost": round(total_cost / holding_days, 4) if holding_days > 0 else 0.0,
        "financing_cost_bps_daily": round(total_cost / position_notional / holding_days * 10_000, 2) if position_notional > 0 and holding_days > 0 else 0.0,
        "components": components,
    }
