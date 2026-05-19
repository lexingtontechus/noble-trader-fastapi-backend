"""
Execution Modeling module (Phase 7).

Provides institutional-grade execution cost modeling for backtest results:
  7A — Almgren-Chriss Market Impact Model — optimal execution trajectory & impact cost
  7B — Fill Probability Model — order fill likelihood based on logit model
  7C — Borrowing & Financing Cost Modeling — short borrow, margin interest, dividend costs
"""

from regime_platform.services.execution.market_impact import (
    compute_market_impact,
    compute_impact_for_backtest,
    estimate_adv_from_prices,
    MarketImpactResult,
)

from regime_platform.services.execution.fill_probability import (
    compute_fill_probability,
    compute_fill_probability_for_backtest,
    simulate_fill_outcome,
    FillProbabilityResult,
    OrderType,
    TimeOfDay,
)

from regime_platform.services.execution.borrow_financing import (
    compute_borrow_cost,
    compute_margin_cost,
    compute_financing_for_trade,
    get_default_borrow_rate,
    compute_financing_summary,
    BorrowCostResult,
    MarginCostResult,
    FinancingResult,
)

__all__ = [
    # 7A — Almgren-Chriss Market Impact
    "compute_market_impact",
    "compute_impact_for_backtest",
    "estimate_adv_from_prices",
    "MarketImpactResult",
    # 7B — Fill Probability Model
    "compute_fill_probability",
    "compute_fill_probability_for_backtest",
    "simulate_fill_outcome",
    "FillProbabilityResult",
    "OrderType",
    "TimeOfDay",
    # 7C — Borrowing & Financing Cost
    "compute_borrow_cost",
    "compute_margin_cost",
    "compute_financing_for_trade",
    "get_default_borrow_rate",
    "compute_financing_summary",
    "BorrowCostResult",
    "MarginCostResult",
    "FinancingResult",
]
