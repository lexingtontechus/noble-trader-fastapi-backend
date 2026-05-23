"""
Execution Modeling Services.

Provides models for realistic execution simulation:
  - Market impact estimation (Almgren-Chriss)
  - Fill probability modeling (limit/stop orders)
  - Borrowing & financing costs (short/leveraged positions)
"""

from regime_platform.services.execution.market_impact import (
    ImpactParams,
    compute_market_impact,
)

from regime_platform.services.execution.fill_probability import (
    OrderType,
    compute_fill_probability,
)

from regime_platform.services.execution.borrow_financing import (
    FinancingParams,
    compute_borrow_cost,
    compute_margin_cost,
    compute_dividend_cost_short,
    compute_total_financing_costs,
)

__all__ = [
    # Market impact
    "ImpactParams",
    "compute_market_impact",
    # Fill probability
    "OrderType",
    "compute_fill_probability",
    # Borrow & financing
    "FinancingParams",
    "compute_borrow_cost",
    "compute_margin_cost",
    "compute_dividend_cost_short",
    "compute_total_financing_costs",
]
