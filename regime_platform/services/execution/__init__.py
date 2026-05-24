"""
Execution Modeling Services.

Provides models for realistic execution simulation:
  - Market impact estimation (Almgren-Chriss)
  - Fill probability modeling (limit/stop orders)
  - Borrowing & financing costs (short/leveraged positions)

All default parameters are configurable via runtime_config (DB → env → hardcoded).
Use `get_execution_defaults()` to resolve them in async contexts.
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


async def get_execution_defaults() -> dict:
    """
    Resolve all execution modeling defaults from runtime_config.

    Returns a dict with keys matching the computation function parameter names,
    ready to be used as fallbacks when the caller doesn't provide explicit values.

    Resolution: DB override → env var → hardcoded default.
    """
    from regime_platform.core.runtime_config import runtime_config

    return {
        # Market impact
        "sigma_daily": await runtime_config.get_float("exec.sigma_daily", default=0.02),
        "permanent_impact_coeff": await runtime_config.get_float("exec.permanent_impact_coeff", default=0.1),
        "temporary_impact_coeff": await runtime_config.get_float("exec.temporary_impact_coeff", default=0.142),
        "default_adv_shares": await runtime_config.get_int("exec.default_adv_shares", default=10_000_000),
        "default_avg_price": await runtime_config.get_float("exec.default_avg_price", default=450.0),
        # Fill probability
        "fill_time_horizon_hours": await runtime_config.get_float("exec.fill_time_horizon_hours", default=6.5),
        "fill_sensitivity": await runtime_config.get_float("exec.fill_sensitivity", default=5.0),
        "fill_threshold": await runtime_config.get_float("exec.fill_threshold", default=1.0),
        "default_participation_rate": await runtime_config.get_float("exec.default_participation_rate", default=0.01),
        "volume_penalty_multiplier": await runtime_config.get_float("exec.volume_penalty_multiplier", default=10.0),
        "max_volume_penalty": await runtime_config.get_float("exec.max_volume_penalty", default=0.3),
        "stop_min_fill_probability": await runtime_config.get_float("exec.stop_min_fill_probability", default=0.90),
        "stop_adverse_sigma": await runtime_config.get_float("exec.stop_adverse_sigma", default=0.5),
        "limit_adverse_sigma": await runtime_config.get_float("exec.limit_adverse_sigma", default=0.2),
        "fill_interp_high": await runtime_config.get_float("exec.fill_interp_high", default=0.8),
        "fill_interp_mid": await runtime_config.get_float("exec.fill_interp_mid", default=0.5),
        "fill_interp_low": await runtime_config.get_float("exec.fill_interp_low", default=0.2),
        # Financing
        "htb_premium_bps": await runtime_config.get_float("exec.htb_premium_bps", default=500.0),
        "borrow_rate_bps": await runtime_config.get_float("exec.borrow_rate_bps", default=50.0),
        "margin_rate_bps": await runtime_config.get_float("exec.margin_rate_bps", default=150.0),
        "dividend_yield_bps": await runtime_config.get_float("exec.dividend_yield_bps", default=200.0),
        "trading_days_per_year": await runtime_config.get_int("exec.trading_days_per_year", default=252),
        "trading_days_per_quarter": await runtime_config.get_int("exec.trading_days_per_quarter", default=63),
    }


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
    # Runtime defaults
    "get_execution_defaults",
]
