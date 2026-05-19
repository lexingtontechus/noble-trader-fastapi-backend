"""
Almgren-Chriss Market Impact Model (Phase 7A).

Implements the Almgren-Chriss (2001) optimal execution model for estimating
market impact costs. The model decomposes impact into two components:

1. **Permanent impact** — the information effect that moves the equilibrium
   price. Proportional to total shares traded relative to daily volume,
   independent of execution speed.

    permanent_impact = γ × (X / V) × σ

2. **Temporary impact** — the transient price concession from consuming
   liquidity. Depends on the execution rate (participation rate per bar).

    temporary_impact = η × (X / (T × V))^(3/5) × σ

Where:
    - X = number of shares to trade
    - V = average daily volume (shares)
    - T = execution horizon in bars
    - σ = daily return volatility
    - γ = permanent impact coefficient (default 0.314, Almgren-Chiss calibrated)
    - η = temporary impact coefficient (default 0.142, Almgren-Chiss calibrated)

Total expected impact cost for optimal execution:
    total_impact = permanent_impact + temporary_impact

Impact is expressed in basis points (bps) and converted to dollar cost
using:  impact_cost_dollars = total_impact_bps / 10000 × X × price

The optimal execution horizon is determined by targeting a 10%
participation rate, which balances market impact against timing risk.

References:
    - Almgren, R. & Chriss, N. (2001). "Optimal Execution of Portfolio
      Transactions", Journal of Risk, 3(2), 5-40.
    - Kissell, R. (2013). "The Science of Algorithmic Trading and Portfolio
      Management", Academic Press.
    - Torre, N. (1997). "Market Impact Model Handbook", BARRA.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ── Result type ──────────────────────────────────────────────────────


@dataclass
class MarketImpactResult:
    """Result of an Almgren-Chriss market impact computation.

    Attributes
    ----------
    shares : float
        Number of shares to trade.
    avg_daily_volume : float
        Average daily volume (ADV) in shares used for the calculation.
    participation_rate : float
        Shares / ADV (as a fraction, e.g. 0.05 = 5% of daily volume).
    permanent_impact_bps : float
        Permanent impact component in basis points.
    temporary_impact_bps : float
        Temporary impact component in basis points.
    total_impact_bps : float
        Sum of permanent + temporary impact in basis points.
    impact_cost_dollars : float
        Total estimated impact cost in dollars.
    optimal_execution_time_bars : int
        Optimal number of bars to execute over (targeting 10% participation).
    gamma : float
        Permanent impact coefficient used.
    eta : float
        Temporary impact coefficient used.
    sigma : float
        Daily volatility used.
    """

    shares: float
    avg_daily_volume: float
    participation_rate: float
    permanent_impact_bps: float
    temporary_impact_bps: float
    total_impact_bps: float
    impact_cost_dollars: float
    optimal_execution_time_bars: int
    gamma: float
    eta: float
    sigma: float

    def to_dict(self) -> dict:
        """Convert result to a dictionary with rounded values."""
        return {
            "shares": round(self.shares, 2),
            "avg_daily_volume": round(self.avg_daily_volume, 2),
            "participation_rate": round(self.participation_rate, 6),
            "permanent_impact_bps": round(self.permanent_impact_bps, 4),
            "temporary_impact_bps": round(self.temporary_impact_bps, 4),
            "total_impact_bps": round(self.total_impact_bps, 4),
            "impact_cost_dollars": round(self.impact_cost_dollars, 2),
            "optimal_execution_time_bars": self.optimal_execution_time_bars,
            "gamma": self.gamma,
            "eta": self.eta,
            "sigma": round(self.sigma, 6),
        }

    def format_display(self) -> str:
        """Return a human-readable summary string.

        Example
        -------
        >>> "Impact: 3.2 bps ($45.60) [perm=1.8, temp=1.4]"
        """
        return (
            f"Impact: {self.total_impact_bps:.1f} bps "
            f"(${self.impact_cost_dollars:,.2f}) "
            f"[perm={self.permanent_impact_bps:.1f}, "
            f"temp={self.temporary_impact_bps:.1f}]"
        )


# ── Core computation ─────────────────────────────────────────────────


def compute_market_impact(
    shares: float,
    price: float,
    avg_daily_volume: float,
    daily_volatility: float,
    execution_horizon_bars: int = 1,
    gamma: float = 0.314,
    eta: float = 0.142,
) -> MarketImpactResult:
    """
    Compute market impact using the Almgren-Chriss model.

    Parameters
    ----------
    shares : float
        Number of shares to trade. Must be positive.
    price : float
        Current price per share. Must be positive.
    avg_daily_volume : float
        Average daily volume in shares. Must be positive.
    daily_volatility : float
        Daily return volatility (e.g., 0.02 for 2%). Must be non-negative.
    execution_horizon_bars : int
        Number of bars (periods) to execute the trade over.
        Default is 1 (instant execution, maximum temporary impact).
        Higher values spread execution and reduce temporary impact.
    gamma : float
        Permanent impact coefficient. Default 0.314 (Almgren-Chiss calibrated).
        Controls the information effect: higher gamma means permanent impact
        is larger for a given participation rate.
    eta : float
        Temporary impact coefficient. Default 0.142 (Almgren-Chiss calibrated).
        Controls the liquidity concession: higher eta means temporary impact
        is larger for a given execution rate.

    Returns
    -------
    MarketImpactResult with impact decomposition and dollar cost.

    Notes
    -----
    The model assumes:
    - Linear permanent impact: price moves proportionally to total volume traded.
    - Concave temporary impact: follows a 3/5 power law in execution rate,
      consistent with the square-root law observed empirically.
    - Independent components: permanent and temporary impacts are additive.

    The default coefficients (γ=0.314, η=0.142) are calibrated from
    Almgren et al. (2005) institutional equity data and are suitable for
    liquid US large-cap equities. For less liquid instruments, increase
    both coefficients.

    Examples
    --------
    >>> result = compute_market_impact(
    ...     shares=10_000,
    ...     price=150.0,
    ...     avg_daily_volume=5_000_000,
    ...     daily_volatility=0.02,
    ... )
    >>> print(result.format_display())
    """
    # ── Edge case handling ────────────────────────────────────────────
    if shares <= 0 or price <= 0 or avg_daily_volume <= 0:
        return _trivial_impact(shares, avg_daily_volume, gamma, eta, daily_volatility)

    if daily_volatility <= 0:
        logger.warning(
            "Zero daily volatility provided — impact will be zero. "
            "This may indicate missing or invalid volatility data."
        )
        return _trivial_impact(shares, avg_daily_volume, gamma, eta, daily_volatility)

    if execution_horizon_bars < 1:
        execution_horizon_bars = 1

    # ── Participation rate ────────────────────────────────────────────
    participation_rate = shares / avg_daily_volume

    if participation_rate > 0.3:
        logger.warning(
            f"High participation rate: {participation_rate:.1%}. "
            f"The Almgren-Chriss model is calibrated for participation "
            f"rates below 30%. Results may understate actual impact "
            f"for such large orders."
        )

    # ── Permanent impact (basis points) ──────────────────────────────
    # permanent_impact_bps = γ × (X / V) × σ × 10000
    permanent_impact_bps = gamma * participation_rate * daily_volatility * 10000

    # ── Temporary impact (basis points) ──────────────────────────────
    # temporary_impact_bps = η × (X / (T × V))^(3/5) × σ × 10000
    # Using the 3/5 power law (0.6 exponent) for concave temporary impact
    execution_rate = shares / (execution_horizon_bars * avg_daily_volume)
    temporary_impact_bps = eta * (execution_rate ** 0.6) * daily_volatility * 10000

    # ── Total impact ──────────────────────────────────────────────────
    total_impact_bps = permanent_impact_bps + temporary_impact_bps

    # ── Dollar cost ───────────────────────────────────────────────────
    # impact_cost_dollars = total_impact_bps / 10000 × shares × price
    impact_cost_dollars = (total_impact_bps / 10000) * shares * price

    # ── Optimal execution horizon ─────────────────────────────────────
    # Target 10% participation rate to balance impact vs. timing risk
    optimal_execution_time_bars = max(1, int(shares / (avg_daily_volume * 0.1)))

    return MarketImpactResult(
        shares=shares,
        avg_daily_volume=avg_daily_volume,
        participation_rate=participation_rate,
        permanent_impact_bps=permanent_impact_bps,
        temporary_impact_bps=temporary_impact_bps,
        total_impact_bps=total_impact_bps,
        impact_cost_dollars=impact_cost_dollars,
        optimal_execution_time_bars=optimal_execution_time_bars,
        gamma=gamma,
        eta=eta,
        sigma=daily_volatility,
    )


# ── Convenience: Impact from backtest context ─────────────────────────


def compute_impact_for_backtest(
    trade_value_dollars: float,
    price: float,
    avg_daily_volume: float,
    daily_volatility: float,
    execution_horizon_bars: int = 1,
    gamma: float = 0.314,
    eta: float = 0.142,
) -> MarketImpactResult:
    """
    Compute market impact for a backtest trade given dollar values.

    This is a convenience wrapper that derives the number of shares
    from the trade value and price, then delegates to compute_market_impact.

    Parameters
    ----------
    trade_value_dollars : float
        Total dollar value of the trade (shares × price).
    price : float
        Execution price per share. Must be positive.
    avg_daily_volume : float
        Average daily volume in shares.
    daily_volatility : float
        Daily return volatility (e.g., 0.02 for 2%).
    execution_horizon_bars : int
        Number of bars to execute over (default 1 = instant).
    gamma : float
        Permanent impact coefficient (default 0.314).
    eta : float
        Temporary impact coefficient (default 0.142).

    Returns
    -------
    MarketImpactResult with impact decomposition and dollar cost.

    Example
    -------
    >>> result = compute_impact_for_backtest(
    ...     trade_value_dollars=1_500_000,
    ...     price=150.0,
    ...     avg_daily_volume=5_000_000,
    ...     daily_volatility=0.02,
    ... )
    >>> print(result.format_display())
    """
    if price <= 0:
        logger.warning(
            "Invalid price (%s) in compute_impact_for_backtest — "
            "returning zero-impact result.", price
        )
        return _trivial_impact(0.0, avg_daily_volume, gamma, eta, daily_volatility)

    shares = trade_value_dollars / price

    return compute_market_impact(
        shares=shares,
        price=price,
        avg_daily_volume=avg_daily_volume,
        daily_volatility=daily_volatility,
        execution_horizon_bars=execution_horizon_bars,
        gamma=gamma,
        eta=eta,
    )


# ── Heuristic: Estimate ADV from price data ──────────────────────────


def estimate_adv_from_prices(
    prices: Sequence[float] | List[float],
    assumed_turnover_rate: float = 0.01,
    shares_outstanding: float = 1e9,
) -> float:
    """
    Estimate average daily volume (ADV) from a price series.

    This is a rough heuristic for backtest mode where actual volume
    data is unavailable. It assumes a fraction of market cap turns
    over daily.

    Formula:
        ADV ≈ assumed_turnover_rate × shares_outstanding

    The default assumptions (1% turnover, 1B shares) produce an
    ADV of 10M shares, which is reasonable for a large-cap ETF
    like SPY.

    Parameters
    ----------
    prices : array-like
        Price series (used only for length validation — the estimate
        itself is heuristic and does not depend on price levels).
    assumed_turnover_rate : float
        Fraction of shares outstanding assumed to trade daily.
        Default 0.01 (1%) is typical for liquid US large-caps.
        Use 0.005 for mid-cap, 0.002 for small-cap.
    shares_outstanding : float
        Total shares outstanding. Default 1e9 (1 billion) approximates
        a large ETF. For individual stocks, use actual shares outstanding.

    Returns
    -------
    Estimated ADV in shares (float).

    Notes
    -----
    This heuristic is intentionally simple. In production, you would
    use actual volume data from a market data provider. The estimate
    serves as a reasonable placeholder for backtesting when no volume
    data is available.

    The price series is accepted primarily for API consistency and
    future extensibility (e.g., estimating turnover from price
    volatility patterns). Currently, the length of the price series
    is validated to ensure it is non-empty.

    Examples
    --------
    >>> adv = estimate_adv_from_prices([100, 101, 102, 101, 103])
    >>> print(f"Estimated ADV: {adv:,.0f} shares")
    """
    prices_arr = np.asarray(prices, dtype=float)

    if len(prices_arr) == 0:
        logger.warning(
            "Empty price series provided to estimate_adv_from_prices — "
            "returning default estimate."
        )
        return assumed_turnover_rate * shares_outstanding

    if assumed_turnover_rate <= 0 or shares_outstanding <= 0:
        logger.warning(
            "Invalid parameters (turnover=%s, shares=%s) — "
            "returning zero ADV.",
            assumed_turnover_rate,
            shares_outstanding,
        )
        return 0.0

    estimated_adv = assumed_turnover_rate * shares_outstanding

    return float(estimated_adv)


# ── Internal helpers ─────────────────────────────────────────────────


def _trivial_impact(
    shares: float,
    avg_daily_volume: float,
    gamma: float,
    eta: float,
    sigma: float,
) -> MarketImpactResult:
    """Return a zero-impact result for edge cases.

    Used when inputs are invalid (zero volume, zero price, etc.)
    to avoid crashing the calling code.
    """
    participation_rate = 0.0
    if shares > 0 and avg_daily_volume > 0:
        participation_rate = shares / avg_daily_volume

    return MarketImpactResult(
        shares=shares,
        avg_daily_volume=avg_daily_volume,
        participation_rate=participation_rate,
        permanent_impact_bps=0.0,
        temporary_impact_bps=0.0,
        total_impact_bps=0.0,
        impact_cost_dollars=0.0,
        optimal_execution_time_bars=1,
        gamma=gamma,
        eta=eta,
        sigma=sigma,
    )
