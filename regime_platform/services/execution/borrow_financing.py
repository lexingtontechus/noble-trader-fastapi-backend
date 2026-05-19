"""
Borrowing & Financing Cost Modeling (Phase 7C).

Models the real-world costs of maintaining leveraged and short positions
that are typically omitted in simple backtests:

1. **Short borrow costs** — When you short a stock, you must borrow shares
   from a lender. The borrow rate is an annualized fee that accrues daily.
   Easy-to-borrow (ETB) stocks like SPY cost ~0.25-0.50% annually.
   Hard-to-borrow (HTB) stocks can cost 5-50%+ annually.

       borrow_cost = short_value × borrow_rate_annual × (days_held / 252)

2. **Margin financing** — Leveraged positions require borrowing capital
   from the broker. The margin rate is typically SOFR + 1.5-3% annually
   and accrues daily while the position is open.

       margin_cost = borrowed_amount × margin_rate_annual × (days_held / 252)
       borrowed_amount = position_value × (1 - margin_requirement)

3. **Dividend payments on shorts** — When short a stock that pays a
   dividend on the ex-dividend date, the short seller owes the dividend
   to the share lender.

       dividend_cost = shares_short × dividend_per_share

These costs can materially erode strategy returns, especially for:
 - Long-duration short positions in HTB names
 - Highly leveraged positions held for extended periods
 - Short positions in high-dividend stocks

References:
    - FINRA Rule 4510 (Margin Requirements)
    - Regulation T (12 CFR 220) — Federal Reserve Board
    - IBKR Borrow Fee Structure:
      https://www.interactivebrokers.com/en/trading/shortSaleStocks.php
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Result types ─────────────────────────────────────────────────────


@dataclass
class BorrowCostResult:
    """Result of a short borrow cost computation.

    Attributes
    ----------
    short_value : float
        Dollar value of the short position.
    borrow_rate_annual : float
        Annualized borrow rate (e.g., 0.005 = 0.5% annually).
    days_held : float
        Number of calendar days the position was held.
    borrow_cost_dollars : float
        Total borrow cost in dollars accrued over the holding period.
    borrow_cost_bps : float
        Borrow cost expressed as basis points of the short value.
    """

    short_value: float
    borrow_rate_annual: float
    days_held: float
    borrow_cost_dollars: float
    borrow_cost_bps: float

    def to_dict(self) -> dict:
        """Convert result to a dictionary with rounded values."""
        return {
            "short_value": round(self.short_value, 4),
            "borrow_rate_annual": round(self.borrow_rate_annual, 6),
            "days_held": round(self.days_held, 4),
            "borrow_cost_dollars": round(self.borrow_cost_dollars, 4),
            "borrow_cost_bps": round(self.borrow_cost_bps, 4),
        }

    def format_display(self) -> str:
        """Return a human-readable summary string.

        Example
        -------
        >>> "Borrow: $12.50 (0.5% ann, 3 days, 8.3 bps)"
        """
        ann_pct = self.borrow_rate_annual * 100
        return (
            f"Borrow: ${self.borrow_cost_dollars:,.2f} "
            f"({ann_pct:.1f}% ann, "
            f"{self.days_held:.0f} days, "
            f"{self.borrow_cost_bps:.1f} bps)"
        )


@dataclass
class MarginCostResult:
    """Result of a margin financing cost computation.

    Attributes
    ----------
    position_value : float
        Total dollar value of the position.
    margin_requirement : float
        Reg T initial margin requirement (e.g., 0.50 = 50%).
    borrowed_amount : float
        Dollar amount financed through margin.
    margin_rate_annual : float
        Annualized margin interest rate (e.g., 0.065 = 6.5%).
    days_held : float
        Number of calendar days the position was held.
    margin_cost_dollars : float
        Total margin interest cost in dollars.
    margin_cost_bps : float
        Margin cost expressed as basis points of position value.
    """

    position_value: float
    margin_requirement: float
    borrowed_amount: float
    margin_rate_annual: float
    days_held: float
    margin_cost_dollars: float
    margin_cost_bps: float

    def to_dict(self) -> dict:
        """Convert result to a dictionary with rounded values."""
        return {
            "position_value": round(self.position_value, 4),
            "margin_requirement": round(self.margin_requirement, 4),
            "borrowed_amount": round(self.borrowed_amount, 4),
            "margin_rate_annual": round(self.margin_rate_annual, 6),
            "days_held": round(self.days_held, 4),
            "margin_cost_dollars": round(self.margin_cost_dollars, 4),
            "margin_cost_bps": round(self.margin_cost_bps, 4),
        }

    def format_display(self) -> str:
        """Return a human-readable summary string.

        Example
        -------
        >>> "Margin: $8.20 (6.5% ann, $5K borrowed, 5.5 bps)"
        """
        ann_pct = self.margin_rate_annual * 100
        borrowed_k = self.borrowed_amount / 1000
        if borrowed_k >= 1.0:
            borrowed_str = f"${borrowed_k:.1f}K"
        else:
            borrowed_str = f"${self.borrowed_amount:,.2f}"
        return (
            f"Margin: ${self.margin_cost_dollars:,.2f} "
            f"({ann_pct:.1f}% ann, "
            f"{borrowed_str} borrowed, "
            f"{self.margin_cost_bps:.1f} bps)"
        )


@dataclass
class FinancingResult:
    """Combined financing cost result for a single trade.

    Attributes
    ----------
    borrow_cost : Optional[BorrowCostResult]
        Short borrow cost. None for long-only positions.
    margin_cost : Optional[MarginCostResult]
        Margin financing cost. None if no leverage is used.
    dividend_cost_dollars : float
        Dividend cost owed by short sellers on ex-dividend dates.
        Zero for long positions.
    total_financing_cost_dollars : float
        Sum of all financing costs (borrow + margin + dividend).
    total_financing_cost_bps : float
        Total financing cost as basis points of position value.
    """

    borrow_cost: Optional[BorrowCostResult]
    margin_cost: Optional[MarginCostResult]
    dividend_cost_dollars: float
    total_financing_cost_dollars: float
    total_financing_cost_bps: float

    def to_dict(self) -> dict:
        """Convert result to a dictionary with rounded values."""
        return {
            "borrow_cost": self.borrow_cost.to_dict() if self.borrow_cost else None,
            "margin_cost": self.margin_cost.to_dict() if self.margin_cost else None,
            "dividend_cost_dollars": round(self.dividend_cost_dollars, 4),
            "total_financing_cost_dollars": round(self.total_financing_cost_dollars, 4),
            "total_financing_cost_bps": round(self.total_financing_cost_bps, 4),
        }

    def format_display(self) -> str:
        """Return a human-readable summary string.

        Example
        -------
        >>> "Financing: $20.70 [borrow=$12.50, margin=$8.20, div=$0.00] (13.8 bps)"
        """
        borrow_d = self.borrow_cost.borrow_cost_dollars if self.borrow_cost else 0.0
        margin_d = self.margin_cost.margin_cost_dollars if self.margin_cost else 0.0
        div_d = self.dividend_cost_dollars
        return (
            f"Financing: ${self.total_financing_cost_dollars:,.2f} "
            f"[borrow=${borrow_d:,.2f}, "
            f"margin=${margin_d:,.2f}, "
            f"div=${div_d:,.2f}] "
            f"({self.total_financing_cost_bps:.1f} bps)"
        )


# ── Core computations ───────────────────────────────────────────────


def compute_borrow_cost(
    short_value: float,
    borrow_rate_annual: float = 0.005,
    days_held: float = 1.0,
) -> BorrowCostResult:
    """
    Compute the borrow cost for a short position.

    The borrow cost accrues daily at the annualized borrow rate,
    prorated by the number of calendar days held relative to 252
    trading days per year.

    Parameters
    ----------
    short_value : float
        Dollar value of the short position (positive number).
        Must be non-negative; zero returns a zero-cost result.
    borrow_rate_annual : float
        Annualized borrow rate as a decimal (e.g., 0.005 = 0.5%).
        Default 0.005 (0.5%) is typical for easy-to-borrow (ETB) stocks.
        Hard-to-borrow (HTB) stocks can be 0.05-0.50+ (5-50%+).
    days_held : float
        Number of calendar days the short position was held.
        Default 1.0 (one day). Must be non-negative; negative values
        are clamped to zero.

    Returns
    -------
    BorrowCostResult with borrow cost in dollars and basis points.

    Notes
    -----
    Formula:
        borrow_cost = short_value × borrow_rate_annual × (days_held / 252)

    Basis points conversion:
        borrow_cost_bps = (borrow_cost / short_value) × 10000
        (Returns 0 if short_value is zero to avoid division by zero.)

    Examples
    --------
    >>> result = compute_borrow_cost(short_value=100_000, days_held=5)
    >>> print(result.format_display())
    Borrow: $9.92 (0.5% ann, 5 days, 1.0 bps)
    """
    # ── Edge case handling ────────────────────────────────────────────
    if short_value <= 0:
        return BorrowCostResult(
            short_value=0.0,
            borrow_rate_annual=borrow_rate_annual,
            days_held=0.0,
            borrow_cost_dollars=0.0,
            borrow_cost_bps=0.0,
        )

    if days_held < 0:
        logger.warning(
            f"Negative days_held ({days_held}) in compute_borrow_cost — "
            f"clamping to zero."
        )
        days_held = 0.0

    if borrow_rate_annual < 0:
        logger.warning(
            f"Negative borrow_rate_annual ({borrow_rate_annual}) — "
            f"treating as zero."
        )
        borrow_rate_annual = 0.0

    # ── Compute borrow cost ──────────────────────────────────────────
    borrow_cost_dollars = short_value * borrow_rate_annual * (days_held / 252.0)

    # ── Basis points ─────────────────────────────────────────────────
    borrow_cost_bps = (borrow_cost_dollars / short_value) * 10000.0

    return BorrowCostResult(
        short_value=round(short_value, 4),
        borrow_rate_annual=round(borrow_rate_annual, 6),
        days_held=round(days_held, 4),
        borrow_cost_dollars=round(borrow_cost_dollars, 4),
        borrow_cost_bps=round(borrow_cost_bps, 4),
    )


def compute_margin_cost(
    position_value: float,
    margin_requirement: float = 0.50,
    margin_rate_annual: float = 0.065,
    days_held: float = 1.0,
) -> MarginCostResult:
    """
    Compute the margin financing cost for a leveraged position.

    When an investor uses margin, they borrow a portion of the position
    value from their broker. The borrowed amount accrues interest at the
    margin rate, prorated by the holding period.

    Parameters
    ----------
    position_value : float
        Total dollar value of the position. Must be non-negative.
    margin_requirement : float
        Reg T initial margin requirement as a fraction of position value.
        Default 0.50 (50%) is the standard Reg T requirement for equities.
        If >= 1.0, no leverage is used and the result is zero cost.
    margin_rate_annual : float
        Annualized margin interest rate as a decimal.
        Default 0.065 (6.5%) approximates SOFR + 200bps.
        Typical range: 0.05-0.08 (5-8%) depending on broker.
    days_held : float
        Number of calendar days the position was held.
        Must be non-negative; negative values are clamped to zero.

    Returns
    -------
    MarginCostResult with margin cost in dollars and basis points.

    Notes
    -----
    Formulas:
        borrowed_amount = position_value × (1 - margin_requirement)
        margin_cost = borrowed_amount × margin_rate_annual × (days_held / 252)
        margin_cost_bps = (margin_cost / position_value) × 10000

    If margin_requirement >= 1.0, no money is borrowed and all costs
    are zero (no leverage used).

    Examples
    --------
    >>> result = compute_margin_cost(position_value=100_000, days_held=10)
    >>> print(result.format_display())
    Margin: $128.97 (6.5% ann, $50.0K borrowed, 12.9 bps)
    """
    # ── Edge case: no leverage ────────────────────────────────────────
    if margin_requirement >= 1.0:
        return MarginCostResult(
            position_value=round(position_value, 4) if position_value > 0 else 0.0,
            margin_requirement=round(margin_requirement, 4),
            borrowed_amount=0.0,
            margin_rate_annual=round(margin_rate_annual, 6),
            days_held=round(max(days_held, 0.0), 4),
            margin_cost_dollars=0.0,
            margin_cost_bps=0.0,
        )

    # ── Edge case: zero position ──────────────────────────────────────
    if position_value <= 0:
        return MarginCostResult(
            position_value=0.0,
            margin_requirement=round(margin_requirement, 4),
            borrowed_amount=0.0,
            margin_rate_annual=round(margin_rate_annual, 6),
            days_held=0.0,
            margin_cost_dollars=0.0,
            margin_cost_bps=0.0,
        )

    if days_held < 0:
        logger.warning(
            f"Negative days_held ({days_held}) in compute_margin_cost — "
            f"clamping to zero."
        )
        days_held = 0.0

    if margin_rate_annual < 0:
        logger.warning(
            f"Negative margin_rate_annual ({margin_rate_annual}) — "
            f"treating as zero."
        )
        margin_rate_annual = 0.0

    if margin_requirement < 0:
        logger.warning(
            f"Negative margin_requirement ({margin_requirement}) — "
            f"clamping to zero."
        )
        margin_requirement = 0.0

    # ── Compute borrowed amount ───────────────────────────────────────
    borrowed_amount = position_value * (1.0 - margin_requirement)

    # ── Compute margin cost ──────────────────────────────────────────
    margin_cost_dollars = borrowed_amount * margin_rate_annual * (days_held / 252.0)

    # ── Basis points ─────────────────────────────────────────────────
    margin_cost_bps = (margin_cost_dollars / position_value) * 10000.0

    # ── Log warning for high margin usage ─────────────────────────────
    if margin_requirement < 0.25:
        logger.warning(
            f"Very low margin requirement ({margin_requirement:.0%}) — "
            f"high leverage amplifies both returns and financing costs."
        )

    return MarginCostResult(
        position_value=round(position_value, 4),
        margin_requirement=round(margin_requirement, 4),
        borrowed_amount=round(borrowed_amount, 4),
        margin_rate_annual=round(margin_rate_annual, 6),
        days_held=round(days_held, 4),
        margin_cost_dollars=round(margin_cost_dollars, 4),
        margin_cost_bps=round(margin_cost_bps, 4),
    )


def compute_financing_for_trade(
    direction: str,
    position_value: float,
    days_held: float,
    use_leverage: bool = False,
    margin_requirement: float = 0.50,
    margin_rate_annual: float = 0.065,
    borrow_rate_annual: float = 0.005,
    hard_to_borrow: bool = False,
    htb_premium_rate: float = 0.10,
    dividend_per_share: float = 0.0,
    shares: float = 1.0,
) -> FinancingResult:
    """
    Compute the comprehensive financing cost for a single trade.

    This is the main entry point for trade-level financing cost analysis.
    It combines short borrow costs, margin financing, and dividend
    obligations into a single result.

    Parameters
    ----------
    direction : str
        Trade direction: "LONG" or "SHORT" (case-insensitive).
        LONG positions may incur margin costs if leverage is used.
        SHORT positions incur borrow costs, dividend costs, and
        potentially margin costs if leveraged.
    position_value : float
        Total dollar value of the position. Must be non-negative.
    days_held : float
        Number of calendar days the position was held open.
    use_leverage : bool
        Whether the position uses margin financing.
        If False, margin costs are zero regardless of other parameters.
    margin_requirement : float
        Reg T initial margin requirement (default 0.50 = 50%).
        Only relevant when use_leverage=True.
    margin_rate_annual : float
        Annualized margin interest rate (default 0.065 = 6.5%,
        approximating SOFR + 200bps).
    borrow_rate_annual : float
        Annualized borrow rate for short positions (default 0.005 = 0.5%).
        Only relevant for SHORT positions.
    hard_to_borrow : bool
        Whether the stock is hard-to-borrow (HTB).
        If True, the htb_premium_rate is added to the borrow rate.
    htb_premium_rate : float
        Additional annual borrow rate for HTB stocks (default 0.10 = 10%).
        Added on top of the base borrow_rate_annual when hard_to_borrow=True.
    dividend_per_share : float
        Dividend amount per share owed by short sellers on the ex-div
        date. Default 0.0 (no dividend). Only relevant for SHORT positions.
    shares : float
        Number of shares held. Used to compute total dividend cost.
        Default 1.0.

    Returns
    -------
    FinancingResult with all financing cost components.

    Notes
    -----
    Cost decomposition by direction:

    LONG:
        - margin_cost (if use_leverage=True)
        - borrow_cost = None
        - dividend_cost = 0

    SHORT:
        - borrow_cost (always, using effective borrow rate)
        - dividend_cost (if dividend_per_share > 0)
        - margin_cost (if use_leverage=True, since short selling
          also requires margin under Reg T)

    Effective borrow rate for HTB stocks:
        effective_borrow_rate = borrow_rate_annual + htb_premium_rate

    Examples
    --------
    >>> # Long position with leverage
    >>> result = compute_financing_for_trade(
    ...     direction="LONG",
    ...     position_value=100_000,
    ...     days_held=5,
    ...     use_leverage=True,
    ... )
    >>> print(result.format_display())

    >>> # Short position in a hard-to-borrow stock
    >>> result = compute_financing_for_trade(
    ...     direction="SHORT",
    ...     position_value=50_000,
    ...     days_held=10,
    ...     hard_to_borrow=True,
    ...     dividend_per_share=0.50,
    ...     shares=200,
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

    # ── Edge case: zero position value ────────────────────────────────
    if position_value <= 0:
        return FinancingResult(
            borrow_cost=None,
            margin_cost=None,
            dividend_cost_dollars=0.0,
            total_financing_cost_dollars=0.0,
            total_financing_cost_bps=0.0,
        )

    if days_held < 0:
        logger.warning(
            f"Negative days_held ({days_held}) — clamping to zero."
        )
        days_held = 0.0

    # ── Compute borrow cost (SHORT only) ─────────────────────────────
    borrow_cost: Optional[BorrowCostResult] = None
    dividend_cost_dollars: float = 0.0

    if direction_upper == "SHORT":
        # Effective borrow rate includes HTB premium
        effective_borrow_rate = borrow_rate_annual
        if hard_to_borrow:
            effective_borrow_rate += htb_premium_rate
            logger.warning(
                f"HTB stock: effective borrow rate = "
                f"{effective_borrow_rate:.2%} "
                f"(base={borrow_rate_annual:.2%} + "
                f"HTB premium={htb_premium_rate:.2%})"
            )

        borrow_cost = compute_borrow_cost(
            short_value=position_value,
            borrow_rate_annual=effective_borrow_rate,
            days_held=days_held,
        )

        # Dividend cost for shorts
        if dividend_per_share > 0 and shares > 0:
            dividend_cost_dollars = shares * dividend_per_share

    # ── Compute margin cost (if leverage used) ────────────────────────
    margin_cost: Optional[MarginCostResult] = None

    if use_leverage:
        margin_cost = compute_margin_cost(
            position_value=position_value,
            margin_requirement=margin_requirement,
            margin_rate_annual=margin_rate_annual,
            days_held=days_held,
        )

    # ── Aggregate total financing cost ────────────────────────────────
    total_dollars = dividend_cost_dollars
    if borrow_cost is not None:
        total_dollars += borrow_cost.borrow_cost_dollars
    if margin_cost is not None:
        total_dollars += margin_cost.margin_cost_dollars

    total_bps = (total_dollars / position_value) * 10000.0

    return FinancingResult(
        borrow_cost=borrow_cost,
        margin_cost=margin_cost,
        dividend_cost_dollars=round(dividend_cost_dollars, 4),
        total_financing_cost_dollars=round(total_dollars, 4),
        total_financing_cost_bps=round(total_bps, 4),
    )


# ── Heuristic: Default borrow rate by symbol ─────────────────────────


def get_default_borrow_rate(
    symbol: str,
    is_etf: bool = False,
    is_small_cap: bool = False,
) -> float:
    """
    Return a reasonable default borrow rate for a given symbol.

    This heuristic provides approximate borrow rates based on the
    instrument type and market cap classification. Actual borrow
    rates vary by broker, availability, and market conditions.

    Parameters
    ----------
    symbol : str
        Ticker symbol (e.g., "SPY", "AAPL", "GME").
        Used to match known ETF tickers with special rates.
    is_etf : bool
        Whether the symbol is an ETF. ETFs typically have much
        lower borrow rates due to high liquidity and share creation
        mechanisms. Default False.
    is_small_cap : bool
        Whether the stock is small-cap. Small caps tend to have
        higher borrow rates due to lower liquidity and fewer
        shares available for lending. Default False.

    Returns
    -------
    Annualized borrow rate as a float (e.g., 0.0025 = 0.25%).

    Notes
    -----
    Borrow rate tiers:
        - Ultra-liquid ETFs (SPY, IVV, VOO): 0.25% annually
        - Other ETFs: 0.50% annually
        - Large-cap stocks: 0.50% annually
        - Mid-cap stocks: 1.50% annually (inferred)
        - Small-cap stocks: 3.00% annually
        - Known HTB / meme stocks: 10.0%+ annually

    These are conservative estimates. In practice, borrow rates
    fluctuate daily based on supply and demand in the securities
    lending market.

    Examples
    --------
    >>> rate = get_default_borrow_rate("SPY")
    >>> print(f"SPY borrow rate: {rate:.2%}")
    SPY borrow rate: 0.25%

    >>> rate = get_default_borrow_rate("GME", is_small_cap=True)
    >>> print(f"GME borrow rate: {rate:.2%}")
    GME borrow rate: 10.00%
    """
    if not symbol:
        return 0.005

    symbol_upper = symbol.strip().upper()

    # ── Ultra-liquid ETFs: 0.25% annually ─────────────────────────────
    ultra_liquid_etfs = {"SPY", "IVV", "VOO", "VTI", "QQQ", "IWM", "DIA"}
    if symbol_upper in ultra_liquid_etfs:
        return 0.0025

    # ── Known HTB / meme stocks: 10%+ annually ───────────────────────
    htb_symbols = {"GME", "AMC", "BBBY", "SNDL", "CEI", "HYMC", "PROG", "DWAC"}
    if symbol_upper in htb_symbols:
        logger.warning(
            f"Symbol {symbol_upper} is a known HTB stock — "
            f"using 10% annual borrow rate."
        )
        return 0.10

    # ── ETFs (non ultra-liquid): 0.50% annually ──────────────────────
    if is_etf:
        return 0.005

    # ── Small-cap stocks: 3.0% annually ──────────────────────────────
    if is_small_cap:
        return 0.03

    # ── Large-cap stocks (default): 0.50% annually ───────────────────
    return 0.005


# ── Aggregate: Financing summary across all trades ───────────────────


def compute_financing_summary(
    trades: List[Dict],
    use_leverage: bool = False,
    margin_rate_annual: float = 0.065,
    default_borrow_rate_annual: float = 0.005,
) -> Dict:
    """
    Compute aggregate financing costs across all trades in a backtest.

    This function iterates over a list of trade dictionaries (as typically
    produced by the backtest engine) and accumulates the total financing
    costs broken down by component and direction.

    Parameters
    ----------
    trades : List[Dict]
        List of trade dictionaries. Each dict should contain:
          - "direction" (str): "LONG" or "SHORT"
          - "position_value" (float): dollar value of the position
          - "days_held" (float): number of days the position was open
        Optional keys:
          - "symbol" (str): ticker symbol for borrow rate lookup
          - "use_leverage" (bool): whether this specific trade used leverage
          - "margin_requirement" (float): override default margin requirement
          - "hard_to_borrow" (bool): whether the stock is HTB
          - "htb_premium_rate" (float): HTB premium rate override
          - "dividend_per_share" (float): dividend owed per share
          - "shares" (float): number of shares held
          - "pnl" (float): trade P&L (used for bps-of-PnL calculation)
    use_leverage : bool
        Global override for leverage usage. If True, all trades are
        treated as using margin unless the trade dict specifies
        "use_leverage" = False. Default False.
    margin_rate_annual : float
        Annualized margin rate for all trades (default 0.065 = 6.5%).
    default_borrow_rate_annual : float
        Default annualized borrow rate for short positions
        (default 0.005 = 0.5%).

    Returns
    -------
    Dict with keys:
        - total_borrow_cost: float — aggregate borrow cost in dollars
        - total_margin_cost: float — aggregate margin cost in dollars
        - total_dividend_cost: float — aggregate dividend cost in dollars
        - total_financing_cost: float — sum of all financing costs
        - financing_cost_bps_of_pnl: float — total financing as bps of
          total P&L (0 if total P&L is zero or negative)
        - trade_count: int — number of trades processed
        - by_direction: dict with "LONG" and "SHORT" breakdowns, each
          containing borrow_cost, margin_cost, dividend_cost, and
          financing_cost sub-totals

    Examples
    --------
    >>> trades = [
    ...     {"direction": "LONG", "position_value": 100000, "days_held": 5, "pnl": 500},
    ...     {"direction": "SHORT", "position_value": 50000, "days_held": 3, "pnl": -200},
    ... ]
    >>> summary = compute_financing_summary(trades, use_leverage=True)
    >>> print(f"Total financing: ${summary['total_financing_cost']:.2f}")
    """
    if not trades:
        return {
            "total_borrow_cost": 0.0,
            "total_margin_cost": 0.0,
            "total_dividend_cost": 0.0,
            "total_financing_cost": 0.0,
            "financing_cost_bps_of_pnl": 0.0,
            "trade_count": 0,
            "by_direction": {
                "LONG": {
                    "borrow_cost": 0.0,
                    "margin_cost": 0.0,
                    "dividend_cost": 0.0,
                    "financing_cost": 0.0,
                    "count": 0,
                },
                "SHORT": {
                    "borrow_cost": 0.0,
                    "margin_cost": 0.0,
                    "dividend_cost": 0.0,
                    "financing_cost": 0.0,
                    "count": 0,
                },
            },
        }

    # ── Accumulators ──────────────────────────────────────────────────
    total_borrow = 0.0
    total_margin = 0.0
    total_dividend = 0.0
    total_pnl = 0.0

    by_direction: Dict[str, Dict[str, float]] = {
        "LONG": {"borrow_cost": 0.0, "margin_cost": 0.0, "dividend_cost": 0.0, "financing_cost": 0.0, "count": 0},
        "SHORT": {"borrow_cost": 0.0, "margin_cost": 0.0, "dividend_cost": 0.0, "financing_cost": 0.0, "count": 0},
    }

    for trade in trades:
        direction = str(trade.get("direction", "LONG")).strip().upper()
        if direction not in ("LONG", "SHORT"):
            direction = "LONG"

        position_value = float(trade.get("position_value", 0.0))
        days_held = float(trade.get("days_held", 0.0))

        # Per-trade overrides with global fallback
        trade_leverage = bool(trade.get("use_leverage", use_leverage))
        margin_req = float(trade.get("margin_requirement", 0.50))
        htb = bool(trade.get("hard_to_borrow", False))
        htb_premium = float(trade.get("htb_premium_rate", 0.10))
        div_per_share = float(trade.get("dividend_per_share", 0.0))
        trade_shares = float(trade.get("shares", 1.0))
        trade_pnl = float(trade.get("pnl", 0.0))

        # Symbol-based borrow rate lookup
        symbol = trade.get("symbol", "")
        if symbol and not htb:
            borrow_rate = get_default_borrow_rate(str(symbol))
        else:
            borrow_rate = default_borrow_rate_annual

        # ── Compute financing for this trade ──────────────────────────
        result = compute_financing_for_trade(
            direction=direction,
            position_value=position_value,
            days_held=days_held,
            use_leverage=trade_leverage,
            margin_requirement=margin_req,
            margin_rate_annual=margin_rate_annual,
            borrow_rate_annual=borrow_rate,
            hard_to_borrow=htb,
            htb_premium_rate=htb_premium,
            dividend_per_share=div_per_share,
            shares=trade_shares,
        )

        # ── Accumulate ───────────────────────────────────────────────
        borrow_d = result.borrow_cost.borrow_cost_dollars if result.borrow_cost else 0.0
        margin_d = result.margin_cost.margin_cost_dollars if result.margin_cost else 0.0
        div_d = result.dividend_cost_dollars

        total_borrow += borrow_d
        total_margin += margin_d
        total_dividend += div_d
        total_pnl += trade_pnl

        by_direction[direction]["borrow_cost"] += borrow_d
        by_direction[direction]["margin_cost"] += margin_d
        by_direction[direction]["dividend_cost"] += div_d
        by_direction[direction]["financing_cost"] += result.total_financing_cost_dollars
        by_direction[direction]["count"] += 1

    total_financing = total_borrow + total_margin + total_dividend

    # ── Financing cost as bps of PnL ─────────────────────────────────
    financing_bps_of_pnl = 0.0
    if total_pnl > 0:
        financing_bps_of_pnl = (total_financing / total_pnl) * 10000.0

    return {
        "total_borrow_cost": round(total_borrow, 4),
        "total_margin_cost": round(total_margin, 4),
        "total_dividend_cost": round(total_dividend, 4),
        "total_financing_cost": round(total_financing, 4),
        "financing_cost_bps_of_pnl": round(financing_bps_of_pnl, 4),
        "trade_count": len(trades),
        "by_direction": {
            d: {
                "borrow_cost": round(v["borrow_cost"], 4),
                "margin_cost": round(v["margin_cost"], 4),
                "dividend_cost": round(v["dividend_cost"], 4),
                "financing_cost": round(v["financing_cost"], 4),
                "count": int(v["count"]),
            }
            for d, v in by_direction.items()
        },
    }
