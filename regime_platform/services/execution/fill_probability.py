"""
Fill Probability Model (Phase 7B).

Models the probability that an order will be filled in a backtest,
addressing the unrealistic assumption that every order executes
instantaneously at the desired price.

In real markets, order fill probability depends on:

1. **Order type** — Market orders fill nearly always (~99.9%),
   limit orders fill probabilistically based on how far the limit
   price is from the market, and stop orders trigger conditionally.

2. **Distance from market** — Limit orders far from the current
   market price are less likely to fill. Measured in basis points
   (bps), each additional bps of distance reduces fill probability.

       distance_bps = |P - M| / M × 10000

3. **Participation rate** — Orders that represent a large fraction
   of average daily volume are harder to fill because there may not
   be enough counterparty liquidity at the limit price.

       participation_rate = order_size_shares / avg_daily_volume

4. **Time of day** — Fill rates vary by session:
   - Market open and close have higher fill rates (more volume)
   - Midday has lower fill rates (thinner liquidity)
   - After-hours has the lowest fill rates

5. **Volume liquidity score** — Highly liquid stocks (high ADV)
   have better fill probabilities than illiquid ones.

The core model uses a logistic (logit) function:

    fill_logit = 2.5 + 0.3 × (-distance_bps) + (-5.0) × participation_rate
                   + 0.5 × time_factor + 1.0 × volume_liquidity_score

    fill_probability = 1 / (1 + exp(-fill_logit))

The logistic function maps the combined signal to a probability
between 0 and 1, with the intercept (2.5) giving a ~92% base
fill probability for at-the-money limit orders in liquid stocks
during the morning session.

The model also estimates the expected partial fill percentage —
even when an order fills, it may only partially execute:

    expected_partial_fill_pct = fill_probability × 0.85 + 0.15

This yields a range of [0.15, 1.0], reflecting that even unlikely
orders tend to partially fill if they fill at all.

References:
    - Cont, R. & de Larrard, A. (2013). "Price Dynamics in a Markovian
      Limit Order Book Market", SIAM Journal on Financial Mathematics.
    - Gould, M.D. et al. (2013). "Limit Order Books", Quantitative
      Finance, 13(11), 1709-1742.
    - Almgren, R. & Chriss, N. (2001). "Optimal Execution of Portfolio
      Transactions", Journal of Risk, 3(2), 5-40.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Enumerations ──────────────────────────────────────────────────────


class OrderType(str, Enum):
    """Order type classification for fill probability modeling.

    Each order type has a characteristic fill probability profile:
    - MARKET: Nearly certain fill (~99.9%), executes at prevailing price
    - LIMIT: Fill depends on distance from market, participation rate,
      time of day, and liquidity
    - STOP: Triggered order, ~95% fill once triggered (may slip)
    - STOP_LIMIT: Combines stop trigger with limit price constraint,
      modeled similarly to LIMIT orders
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeOfDay(str, Enum):
    """Intraday time period classification.

    Fill rates vary significantly across the trading day due to
    volume concentration patterns:

    - OPEN (9:30-10:00): High volume, high volatility, best fill rates
    - MORNING (10:00-11:30): Good volume, directional moves
    - MIDDAY (11:30-14:00): Low volume, doldrums, worst fill rates
    - CLOSE (14:00-16:00): Volume pickup, institutional rebalancing
    - AFTER_HOURS (16:00+): Very low volume, poor fills
    """

    OPEN = "OPEN"
    MORNING = "MORNING"
    MIDDAY = "MIDDAY"
    CLOSE = "CLOSE"
    AFTER_HOURS = "AFTER_HOURS"


# ── Time factor mapping ──────────────────────────────────────────────

_TIME_FACTOR_MAP: dict[TimeOfDay, float] = {
    TimeOfDay.OPEN: 1.0,
    TimeOfDay.MORNING: 0.8,
    TimeOfDay.MIDDAY: 0.3,
    TimeOfDay.CLOSE: 0.9,
    TimeOfDay.AFTER_HOURS: -0.5,
}


# ── String to TimeOfDay mapping ──────────────────────────────────────

_TIME_OF_DAY_STR_MAP: dict[str, TimeOfDay] = {
    "open": TimeOfDay.OPEN,
    "morning": TimeOfDay.MORNING,
    "midday": TimeOfDay.MIDDAY,
    "close": TimeOfDay.CLOSE,
    "after_hours": TimeOfDay.AFTER_HOURS,
    "afterhours": TimeOfDay.AFTER_HOURS,
    "ah": TimeOfDay.AFTER_HOURS,
    "premarket": TimeOfDay.AFTER_HOURS,
    "pm": TimeOfDay.AFTER_HOURS,
}


# ── Result type ──────────────────────────────────────────────────────


@dataclass
class FillProbabilityResult:
    """Result of a fill probability computation.

    Attributes
    ----------
    order_type : OrderType
        Type of order evaluated.
    fill_probability : float
        Probability that the order will be filled, in [0.01, 1.0].
    distance_bps : float
        Distance of limit price from market price in basis points.
        Zero for market and stop orders.
    participation_rate : float
        Order size as a fraction of average daily volume.
    time_factor : float
        Time-of-day adjustment factor applied to the logit model.
    volume_liquidity_score : float
        Liquidity score based on ADV, in [0, 1]. Higher = more liquid.
    expected_partial_fill_pct : float
        Expected percentage of the order that will fill, in [0.15, 1.0].
        Accounts for the possibility of partial fills even when the
        order is triggered.
    will_fill : bool
        Whether the order fills in this simulation, determined by
        a Bernoulli draw with probability = fill_probability.
    """

    order_type: OrderType
    fill_probability: float
    distance_bps: float
    participation_rate: float
    time_factor: float
    volume_liquidity_score: float
    expected_partial_fill_pct: float
    will_fill: bool

    def to_dict(self) -> dict:
        """Convert result to a dictionary with rounded values."""
        return {
            "order_type": self.order_type.value,
            "fill_probability": round(self.fill_probability, 6),
            "distance_bps": round(self.distance_bps, 4),
            "participation_rate": round(self.participation_rate, 6),
            "time_factor": round(self.time_factor, 4),
            "volume_liquidity_score": round(self.volume_liquidity_score, 4),
            "expected_partial_fill_pct": round(self.expected_partial_fill_pct, 6),
            "will_fill": self.will_fill,
        }

    def format_display(self) -> str:
        """Return a human-readable summary string.

        Example
        -------
        >>> "Fill Prob: 87.3% [LIMIT, 8.2 bps away, 3.1% participation]"
        """
        fill_pct = self.fill_probability * 100
        part_pct = self.participation_rate * 100
        return (
            f"Fill Prob: {fill_pct:.1f}% "
            f"[{self.order_type.value}, "
            f"{self.distance_bps:.1f} bps away, "
            f"{part_pct:.1f}% participation]"
        )


# ── Core computation ─────────────────────────────────────────────────


def compute_fill_probability(
    order_type: OrderType,
    limit_price: Optional[float] = None,
    market_price: float = 0.0,
    order_size_shares: float = 1.0,
    avg_daily_volume: float = 1e6,
    time_of_day: TimeOfDay = TimeOfDay.MORNING,
    daily_volatility: float = 0.02,
    seed: Optional[int] = None,
) -> FillProbabilityResult:
    """
    Compute the fill probability for an order using the logit model.

    For market orders, fill probability is ~99.9% (nearly certain).
    For stop orders, fill probability is ~95% (triggered but may slip).
    For limit and stop-limit orders, a logistic model estimates fill
    probability based on distance from market, participation rate,
    time of day, and volume liquidity.

    Parameters
    ----------
    order_type : OrderType
        Type of order: MARKET, LIMIT, STOP, or STOP_LIMIT.
    limit_price : Optional[float]
        Limit price for LIMIT and STOP_LIMIT orders. Must be provided
        and positive for LIMIT and STOP_LIMIT order types.
        Ignored for MARKET and STOP orders.
    market_price : float
        Current market price. Must be positive for LIMIT and STOP_LIMIT
        orders (used to compute distance in bps).
    order_size_shares : float
        Number of shares in the order. Used to compute participation
        rate relative to average daily volume. Default 1.0.
    avg_daily_volume : float
        Average daily volume in shares. Used for participation rate
        and liquidity scoring. Default 1,000,000.
    time_of_day : TimeOfDay
        Intraday time period affecting fill probability.
        Default TimeOfDay.MORNING.
    daily_volatility : float
        Daily return volatility (e.g., 0.02 for 2%). Currently
        informational but reserved for future model extensions.
        Default 0.02.
    seed : Optional[int]
        Random seed for the Bernoulli draw determining will_fill.
        If None, the draw is non-deterministic. Providing a seed
        ensures reproducible backtest results.

    Returns
    -------
    FillProbabilityResult with fill probability, model components,
    and a simulated fill/no-fill outcome.

    Raises
    ------
    ValueError
        If order_type is LIMIT or STOP_LIMIT and limit_price is None
        or not positive, or if market_price is not positive.

    Notes
    -----
    Logit model for limit/stop-limit orders:

        fill_logit = 2.5 + 0.3 × (-distance_bps)
                       + (-5.0) × participation_rate
                       + 0.5 × time_factor
                       + 1.0 × volume_liquidity_score

        fill_probability = 1 / (1 + exp(-fill_logit))

    Where:
        - distance_bps = |limit_price - market_price| / market_price × 10000
        - participation_rate = order_size_shares / avg_daily_volume
        - time_factor: OPEN=1.0, MORNING=0.8, MIDDAY=0.3, CLOSE=0.9,
          AFTER_HOURS=-0.5
        - volume_liquidity_score = min(1.0, avg_daily_volume / 5e6)

    The fill probability is clamped to [0.01, 1.0] to avoid
    degenerate cases (zero probability or certainty).

    The expected partial fill percentage accounts for the possibility
    that even triggered orders may only partially execute:

        expected_partial_fill_pct = fill_probability × 0.85 + 0.15

    This maps fill_probability ∈ [0.01, 1.0] to partial fill ∈ [0.1585, 1.0].

    Examples
    --------
    >>> result = compute_fill_probability(
    ...     order_type=OrderType.LIMIT,
    ...     limit_price=149.50,
    ...     market_price=150.00,
    ...     order_size_shares=500,
    ...     avg_daily_volume=2_000_000,
    ...     time_of_day=TimeOfDay.MORNING,
    ...     seed=42,
    ... )
    >>> print(result.format_display())
    """
    # ── Market order: nearly certain fill ─────────────────────────────
    if order_type == OrderType.MARKET:
        fill_prob = 0.999
        return FillProbabilityResult(
            order_type=order_type,
            fill_probability=fill_prob,
            distance_bps=0.0,
            participation_rate=0.0,
            time_factor=0.0,
            volume_liquidity_score=0.0,
            expected_partial_fill_pct=fill_prob * 0.85 + 0.15,
            will_fill=simulate_fill_outcome(fill_prob, seed=seed),
        )

    # ── Stop order: high probability once triggered ───────────────────
    if order_type == OrderType.STOP:
        fill_prob = 0.95
        return FillProbabilityResult(
            order_type=order_type,
            fill_probability=fill_prob,
            distance_bps=0.0,
            participation_rate=0.0,
            time_factor=0.0,
            volume_liquidity_score=0.0,
            expected_partial_fill_pct=fill_prob * 0.85 + 0.15,
            will_fill=simulate_fill_outcome(fill_prob, seed=seed),
        )

    # ── Limit / Stop-Limit: logit model ──────────────────────────────
    if order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
        # Validate required parameters
        if limit_price is None:
            raise ValueError(
                f"limit_price must be provided for {order_type.value} orders. "
                f"Received limit_price=None."
            )
        if limit_price <= 0:
            raise ValueError(
                f"limit_price must be positive for {order_type.value} orders. "
                f"Received limit_price={limit_price}."
            )
        if market_price <= 0:
            raise ValueError(
                f"market_price must be positive for {order_type.value} orders. "
                f"Received market_price={market_price}."
            )

        # ── Distance in basis points ──────────────────────────────────
        distance_bps = abs(limit_price - market_price) / market_price * 10000.0

        # ── Participation rate ────────────────────────────────────────
        if avg_daily_volume <= 0:
            logger.warning(
                f"Zero or negative avg_daily_volume ({avg_daily_volume}) — "
                f"cannot compute participation rate. Using fallback "
                f"avg_daily_volume=1e6."
            )
            avg_daily_volume = 1e6

        participation_rate = order_size_shares / avg_daily_volume

        if participation_rate > 0.3:
            logger.warning(
                f"High participation rate: {participation_rate:.1%}. "
                f"Orders consuming >30% of daily volume are very unlikely "
                f"to fill completely at the limit price."
            )

        # ── Time factor ──────────────────────────────────────────────
        time_factor = _TIME_FACTOR_MAP.get(time_of_day, 0.8)

        # ── Volume liquidity score ───────────────────────────────────
        volume_liquidity_score = min(1.0, avg_daily_volume / 5e6)

        # ── Logit model ──────────────────────────────────────────────
        fill_logit = (
            2.5
            + 0.3 * (-distance_bps)
            + (-5.0) * participation_rate
            + 0.5 * time_factor
            + 1.0 * volume_liquidity_score
        )

        # ── Sigmoid → probability ────────────────────────────────────
        fill_probability = 1.0 / (1.0 + math.exp(-fill_logit))

        # ── Clamp to [0.01, 1.0] ─────────────────────────────────────
        fill_probability = max(0.01, min(1.0, fill_probability))

        # ── Expected partial fill ─────────────────────────────────────
        expected_partial_fill_pct = fill_probability * 0.85 + 0.15

        # ── Bernoulli draw ───────────────────────────────────────────
        will_fill = simulate_fill_outcome(fill_probability, seed=seed)

        return FillProbabilityResult(
            order_type=order_type,
            fill_probability=fill_probability,
            distance_bps=round(distance_bps, 4),
            participation_rate=round(participation_rate, 6),
            time_factor=round(time_factor, 4),
            volume_liquidity_score=round(volume_liquidity_score, 4),
            expected_partial_fill_pct=round(expected_partial_fill_pct, 6),
            will_fill=will_fill,
        )

    # ── Fallback (should never reach with valid OrderType) ───────────
    raise ValueError(f"Unsupported order_type: {order_type}")


# ── Convenience: Fill probability from backtest context ───────────────


def compute_fill_probability_for_backtest(
    direction: str,
    entry_price: float,
    market_price: float,
    order_size_usd: float,
    avg_daily_volume: float = 1e6,
    is_limit_order: bool = False,
    time_of_day: str = "morning",
    daily_volatility: float = 0.02,
    seed: Optional[int] = None,
) -> FillProbabilityResult:
    """
    Compute fill probability for a backtest trade using simplified inputs.

    This convenience wrapper translates backtest-level parameters
    (direction, dollar size, limit flag) into the inputs required
    by the core logit model. It handles the conversion from dollar
    order size to share count and maps string-based time-of-day
    to the TimeOfDay enum.

    Parameters
    ----------
    direction : str
        Trade direction: "LONG" or "SHORT" (case-insensitive).
        Used to determine the effective limit price for limit orders:
        - LONG limit: limit_price = entry_price (buy below market)
        - SHORT limit: limit_price = entry_price (sell above market)
    entry_price : float
        Intended entry price for the trade. For limit orders, this
        becomes the limit price. For market orders, this is the
        expected execution price. Must be positive.
    market_price : float
        Current market price. Must be positive.
    order_size_usd : float
        Dollar value of the order. Used to derive share count
        and participation rate. Must be positive for meaningful
        results; zero returns a trivial result.
    avg_daily_volume : float
        Average daily volume in shares. Default 1,000,000.
    is_limit_order : bool
        Whether the trade uses a limit order. If False, the order
        is treated as a market order with ~99.9% fill probability.
        Default False.
    time_of_day : str
        Time of day as a string. Case-insensitive. Accepted values:
        "open", "morning", "midday", "close", "after_hours",
        "afterhours", "ah", "premarket", "pm". Default "morning".
    daily_volatility : float
        Daily return volatility (e.g., 0.02 for 2%). Default 0.02.
    seed : Optional[int]
        Random seed for reproducible Bernoulli draws.

    Returns
    -------
    FillProbabilityResult with fill probability and model components.

    Notes
    -----
    For market orders (is_limit_order=False), fill probability is
    fixed at 99.9%. For limit orders, the full logit model is applied.

    The share count is derived as: shares = order_size_usd / entry_price.

    Examples
    --------
    >>> result = compute_fill_probability_for_backtest(
    ...     direction="LONG",
    ...     entry_price=149.50,
    ...     market_price=150.00,
    ...     order_size_usd=50000,
    ...     is_limit_order=True,
    ...     time_of_day="morning",
    ...     seed=42,
    ... )
    >>> print(result.format_display())
    """
    direction_upper = direction.strip().upper()
    if direction_upper not in ("LONG", "SHORT"):
        logger.warning(
            f"Unrecognized direction '{direction}' — defaulting to LONG. "
            f"Expected 'LONG' or 'SHORT'."
        )
        direction_upper = "LONG"

    # ── Edge case: zero or negative inputs ────────────────────────────
    if entry_price <= 0 or market_price <= 0:
        logger.warning(
            f"Invalid prices (entry={entry_price}, market={market_price}) — "
            f"returning zero-fill result."
        )
        return FillProbabilityResult(
            order_type=OrderType.MARKET if not is_limit_order else OrderType.LIMIT,
            fill_probability=0.01,
            distance_bps=0.0,
            participation_rate=0.0,
            time_factor=0.0,
            volume_liquidity_score=0.0,
            expected_partial_fill_pct=0.1585,
            will_fill=False,
        )

    # ── Determine order type ─────────────────────────────────────────
    if is_limit_order:
        order_type = OrderType.LIMIT
    else:
        order_type = OrderType.MARKET

    # ── Parse time of day ─────────────────────────────────────────────
    tod_enum = _TIME_OF_DAY_STR_MAP.get(
        time_of_day.strip().lower(), TimeOfDay.MORNING
    )

    # ── Derive shares from dollar size ───────────────────────────────
    order_size_shares = order_size_usd / entry_price if entry_price > 0 else 0.0

    # ── Delegate to core computation ──────────────────────────────────
    return compute_fill_probability(
        order_type=order_type,
        limit_price=entry_price if is_limit_order else None,
        market_price=market_price,
        order_size_shares=order_size_shares,
        avg_daily_volume=avg_daily_volume,
        time_of_day=tod_enum,
        daily_volatility=daily_volatility,
        seed=seed,
    )


# ── Simulation helper ────────────────────────────────────────────────


def simulate_fill_outcome(
    fill_probability: float,
    seed: Optional[int] = None,
    rng: Optional[np.random.Generator] = None,
) -> bool:
    """
    Simulate a fill/no-fill outcome via Bernoulli draw.

    Given a fill probability p, draws a single Bernoulli random
    variable: True with probability p, False with probability 1-p.

    Parameters
    ----------
    fill_probability : float
        Probability of fill, in [0, 1]. Values outside this range
        are clamped.
    seed : Optional[int]
        Random seed for reproducibility. If provided, a new
        numpy Generator is created with this seed. If rng is also
        provided, rng takes precedence and seed is ignored.
    rng : Optional[np.random.Generator]
        Pre-existing numpy random Generator instance. If provided,
        it is used directly and seed is ignored. Useful for
        maintaining a single RNG stream across multiple calls.

    Returns
    -------
    bool — True if the order fills, False otherwise.

    Notes
    -----
    For reproducible backtests, either provide a consistent seed
    or pass a pre-initialized RNG. Using the same seed across
    calls will produce the same sequence of fill outcomes.

    Examples
    --------
    >>> # Reproducible outcome
    >>> simulate_fill_outcome(0.8, seed=42)
    True

    >>> # Using a shared RNG
    >>> rng = np.random.default_rng(42)
    >>> results = [simulate_fill_outcome(0.8, rng=rng) for _ in range(10)]
    """
    # ── Clamp probability ─────────────────────────────────────────────
    p = max(0.0, min(1.0, fill_probability))

    # ── Get RNG ───────────────────────────────────────────────────────
    if rng is not None:
        generator = rng
    elif seed is not None:
        generator = np.random.default_rng(seed)
    else:
        generator = np.random.default_rng()

    # ── Bernoulli draw ────────────────────────────────────────────────
    return bool(generator.random() < p)
