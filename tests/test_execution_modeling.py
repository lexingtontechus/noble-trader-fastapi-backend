"""
Comprehensive pytest test suite for Phase 7 (Execution Modeling) of the
noble-trader backtest platform.

Covers:
  7A - Almgren-Chriss Market Impact Model (10 tests)
  7B - Fill Probability Model (12 tests)
  7C - Borrowing & Financing Cost Modeling (12 tests)
  Integration with RiskManager, TradeRecord, RenkoConfig, RenkoBacktestRequest (8+ tests)

All tests are self-contained and deterministic.
"""

import math

import pytest

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
from renko.risk_manager import RiskParams, RiskManager, TradeResult, PositionStatus
from renko.trade_journal import TradeRecord
from renko.config import RenkoConfig
from renko.brick_engine import Brick, BrickDirection
from renko.pattern_detector import SignalDirection, PatternType


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def typical_market_impact():
    """Return a MarketImpactResult for typical large-cap trading."""
    return compute_market_impact(
        shares=10_000,
        price=150.0,
        avg_daily_volume=5_000_000,
        daily_volatility=0.02,
    )


@pytest.fixture
def risk_params_phase7_enabled():
    """RiskParams with all Phase 7 features enabled."""
    return RiskParams(
        sl_bricks=3,
        tp_bricks=5,
        enable_market_impact=True,
        avg_daily_volume=1e6,
        impact_gamma=0.314,
        impact_eta=0.142,
        enable_fill_probability=True,
        enable_borrow_costs=True,
        borrow_rate_annual=0.005,
        hard_to_borrow=False,
        htb_premium_rate=0.10,
        enable_margin_costs=True,
        margin_rate_annual=0.065,
        margin_requirement=0.50,
    )


@pytest.fixture
def risk_params_phase7_disabled():
    """RiskParams with Phase 7 features disabled (default)."""
    return RiskParams()


@pytest.fixture
def sample_brick():
    """A sample Renko brick for use in RiskManager tests."""
    return Brick(
        index=0,
        direction=BrickDirection.UP,
        open_price=100.0,
        close_price=100.5,
        high=100.6,
        low=99.9,
        timestamp=1700000000.0,
        brick_size=0.5,
    )


@pytest.fixture
def sample_trades_for_summary():
    """Sample trade dicts for compute_financing_summary."""
    return [
        {
            "direction": "LONG",
            "position_value": 100_000,
            "days_held": 5,
            "pnl": 500,
            "symbol": "SPY",
        },
        {
            "direction": "SHORT",
            "position_value": 50_000,
            "days_held": 3,
            "pnl": -200,
            "symbol": "AAPL",
        },
        {
            "direction": "SHORT",
            "position_value": 30_000,
            "days_held": 10,
            "pnl": -100,
            "symbol": "GME",
            "hard_to_borrow": True,
        },
    ]


# ══════════════════════════════════════════════════════════════════════
# 7A - Market Impact (Almgren-Chriss)
# ══════════════════════════════════════════════════════════════════════


class TestMarketImpact:
    """Tests for the Almgren-Chriss market impact model (Phase 7A)."""

    def test_market_impact_basic(self, typical_market_impact):
        """Verify compute_market_impact with typical values populates all fields.

        A standard large-cap trade (10K shares at $150, 5M ADV, 2% vol)
        should produce a valid MarketImpactResult with non-zero impact.
        """
        result = typical_market_impact
        assert isinstance(result, MarketImpactResult)
        assert result.shares == 10_000
        assert result.avg_daily_volume == 5_000_000
        assert result.participation_rate == pytest.approx(10_000 / 5_000_000)
        assert result.permanent_impact_bps > 0
        assert result.temporary_impact_bps > 0
        assert result.total_impact_bps == pytest.approx(
            result.permanent_impact_bps + result.temporary_impact_bps
        )
        assert result.impact_cost_dollars > 0
        assert result.optimal_execution_time_bars >= 1
        assert result.gamma == 0.314
        assert result.eta == 0.142
        assert result.sigma == 0.02

    def test_market_impact_zero_volume(self):
        """Zero ADV should return zero impact gracefully (trivial result).

        When avg_daily_volume <= 0, the model cannot compute meaningful
        participation rates, so it should return a zero-impact result
        rather than raising an exception.
        """
        result = compute_market_impact(
            shares=10_000, price=150.0, avg_daily_volume=0, daily_volatility=0.02
        )
        assert result.total_impact_bps == 0.0
        assert result.impact_cost_dollars == 0.0
        assert result.permanent_impact_bps == 0.0
        assert result.temporary_impact_bps == 0.0

    def test_market_impact_zero_volatility(self):
        """Zero volatility should return zero impact (trivial result).

        With zero daily volatility, there is no price movement to impact,
        so both permanent and temporary impacts should be zero.
        """
        result = compute_market_impact(
            shares=10_000, price=150.0, avg_daily_volume=5_000_000, daily_volatility=0.0
        )
        assert result.total_impact_bps == 0.0
        assert result.impact_cost_dollars == 0.0

    def test_market_impact_large_order(self, caplog):
        """Large participation rate (>30%) should log warning and still compute.

        Orders consuming more than 30% of daily volume are outside the
        model's calibration range but should still produce a result.
        """
        with caplog.at_level("WARNING"):
            result = compute_market_impact(
                shares=2_000_000,
                price=150.0,
                avg_daily_volume=5_000_000,
                daily_volatility=0.02,
            )
        assert result.participation_rate > 0.3
        assert result.total_impact_bps > 0
        assert any("High participation rate" in r.message for r in caplog.records)

    def test_market_impact_small_order(self):
        """Small order relative to ADV should have minimal impact.

        An order representing 0.01% of daily volume should produce
        negligible market impact (well under 1 bps total).
        """
        result = compute_market_impact(
            shares=500,
            price=150.0,
            avg_daily_volume=5_000_000,
            daily_volatility=0.02,
        )
        assert result.participation_rate < 0.001
        assert result.total_impact_bps < 1.0
        assert result.impact_cost_dollars < 1.0

    def test_market_impact_instant_vs_split(self):
        """Splitting execution over multiple bars should reduce temporary impact.

        Execution over more bars reduces the per-bar participation rate,
        which reduces temporary (liquidity) impact. Permanent impact
        should remain the same regardless of execution horizon.
        """
        instant = compute_market_impact(
            shares=10_000,
            price=150.0,
            avg_daily_volume=5_000_000,
            daily_volatility=0.02,
            execution_horizon_bars=1,
        )
        split = compute_market_impact(
            shares=10_000,
            price=150.0,
            avg_daily_volume=5_000_000,
            daily_volatility=0.02,
            execution_horizon_bars=5,
        )
        # Permanent impact is the same regardless of horizon
        assert instant.permanent_impact_bps == pytest.approx(
            split.permanent_impact_bps, rel=1e-6
        )
        # Split execution should reduce temporary impact
        assert split.temporary_impact_bps < instant.temporary_impact_bps
        # Total impact should also be lower
        assert split.total_impact_bps < instant.total_impact_bps

    def test_impact_for_backtest(self):
        """compute_impact_for_backtest convenience function should derive shares from dollars.

        The convenience wrapper converts trade_value_dollars / price to
        share count and delegates to compute_market_impact.
        """
        result = compute_impact_for_backtest(
            trade_value_dollars=1_500_000,
            price=150.0,
            avg_daily_volume=5_000_000,
            daily_volatility=0.02,
        )
        assert isinstance(result, MarketImpactResult)
        assert result.shares == pytest.approx(1_500_000 / 150.0)
        assert result.total_impact_bps > 0
        assert result.impact_cost_dollars > 0

    def test_estimate_adv_from_prices(self):
        """estimate_adv_from_prices heuristic should return turnover × shares_outstanding.

        With default parameters (1% turnover, 1B shares), the ADV
        estimate should be 10M shares.
        """
        adv = estimate_adv_from_prices([100, 101, 102, 101, 103])
        assert adv == pytest.approx(0.01 * 1e9)

        # Custom parameters
        adv_custom = estimate_adv_from_prices(
            [100, 101], assumed_turnover_rate=0.005, shares_outstanding=500_000_000
        )
        assert adv_custom == pytest.approx(0.005 * 500_000_000)

    def test_market_impact_result_to_dict(self, typical_market_impact):
        """Verify to_dict() returns all fields with rounded values."""
        d = typical_market_impact.to_dict()
        assert isinstance(d, dict)
        expected_keys = {
            "shares",
            "avg_daily_volume",
            "participation_rate",
            "permanent_impact_bps",
            "temporary_impact_bps",
            "total_impact_bps",
            "impact_cost_dollars",
            "optimal_execution_time_bars",
            "gamma",
            "eta",
            "sigma",
        }
        assert set(d.keys()) == expected_keys
        # Verify values are rounded (integers pass through round as int)
        assert isinstance(d["optimal_execution_time_bars"], int)
        assert isinstance(d["shares"], (int, float))
        assert isinstance(d["impact_cost_dollars"], float)

    def test_market_impact_result_format_display(self, typical_market_impact):
        """Verify format_display() returns a human-readable string.

        The output should contain the total impact in bps, dollar cost,
        and the permanent/temporary decomposition.
        """
        s = typical_market_impact.format_display()
        assert isinstance(s, str)
        assert "Impact:" in s
        assert "bps" in s
        assert "$" in s
        assert "perm=" in s
        assert "temp=" in s


# ══════════════════════════════════════════════════════════════════════
# 7B - Fill Probability
# ══════════════════════════════════════════════════════════════════════


class TestFillProbability:
    """Tests for the fill probability model (Phase 7B)."""

    def test_fill_prob_market_order(self):
        """Market order should have ~99.9% fill probability."""
        result = compute_fill_probability(
            order_type=OrderType.MARKET, seed=42
        )
        assert result.fill_probability == pytest.approx(0.999)
        assert result.order_type == OrderType.MARKET
        assert result.distance_bps == 0.0
        assert result.participation_rate == 0.0

    def test_fill_prob_stop_order(self):
        """Stop order should have ~95% fill probability."""
        result = compute_fill_probability(
            order_type=OrderType.STOP, seed=42
        )
        assert result.fill_probability == pytest.approx(0.95)
        assert result.order_type == OrderType.STOP
        assert result.distance_bps == 0.0

    def test_fill_prob_limit_order_at_market(self):
        """Limit order at market price should have high fill probability.

        When limit_price == market_price, distance_bps is 0, which
        maximizes the fill probability for the logit model.
        """
        result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=150.0,
            market_price=150.0,
            order_size_shares=500,
            avg_daily_volume=2_000_000,
            time_of_day=TimeOfDay.MORNING,
            seed=42,
        )
        assert result.fill_probability > 0.8
        assert result.distance_bps == pytest.approx(0.0, abs=0.01)

    def test_fill_prob_limit_order_far_away(self):
        """Limit order far from market should have low fill probability.

        A limit price 5% away from market (500 bps) should have
        significantly reduced fill probability.
        """
        result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=142.5,  # 5% below $150
            market_price=150.0,
            order_size_shares=500,
            avg_daily_volume=2_000_000,
            time_of_day=TimeOfDay.MORNING,
            seed=42,
        )
        assert result.fill_probability < 0.5
        assert result.distance_bps > 400  # roughly 500 bps

    def test_fill_prob_high_participation(self):
        """Large order relative to ADV reduces fill probability.

        An order consuming 20% of daily volume should have a lower
        fill probability than a small order at the same distance.
        We use a limit price close to market to avoid hitting the
        0.01 floor clamp on both probabilities.
        """
        small = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.95,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=5_000_000,
            time_of_day=TimeOfDay.OPEN,
            seed=42,
        )
        large = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.95,
            market_price=150.00,
            order_size_shares=1_000_000,  # 20% of ADV
            avg_daily_volume=5_000_000,
            time_of_day=TimeOfDay.OPEN,
            seed=42,
        )
        assert large.fill_probability < small.fill_probability
        assert large.participation_rate > small.participation_rate

    def test_fill_prob_time_of_day(self):
        """OPEN/CLOSE should have higher fill prob than MIDDAY.

        Market open and close have higher volume and better fill rates.
        Midday has lower volume and worse fill rates.
        We use a limit price very close to market to ensure probabilities
        are above the 0.01 floor clamp.
        """
        open_result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.99,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=5_000_000,
            time_of_day=TimeOfDay.OPEN,
            seed=42,
        )
        midday_result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.99,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=5_000_000,
            time_of_day=TimeOfDay.MIDDAY,
            seed=42,
        )
        close_result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.99,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=5_000_000,
            time_of_day=TimeOfDay.CLOSE,
            seed=42,
        )
        assert open_result.fill_probability > midday_result.fill_probability
        assert close_result.fill_probability > midday_result.fill_probability

    def test_fill_prob_limit_without_price(self):
        """LIMIT order without limit_price should raise ValueError."""
        with pytest.raises(ValueError, match="limit_price must be provided"):
            compute_fill_probability(order_type=OrderType.LIMIT, seed=42)

    def test_fill_prob_for_backtest(self):
        """compute_fill_probability_for_backtest convenience function should work.

        The convenience wrapper translates backtest-level parameters
        into the core logit model inputs.
        """
        # Market order (is_limit_order=False)
        result = compute_fill_probability_for_backtest(
            direction="LONG",
            entry_price=150.0,
            market_price=150.0,
            order_size_usd=50_000,
            is_limit_order=False,
            seed=42,
        )
        assert isinstance(result, FillProbabilityResult)
        assert result.fill_probability == pytest.approx(0.999)

        # Limit order (is_limit_order=True)
        result_limit = compute_fill_probability_for_backtest(
            direction="LONG",
            entry_price=149.50,
            market_price=150.00,
            order_size_usd=50_000,
            is_limit_order=True,
            time_of_day="morning",
            seed=42,
        )
        assert result_limit.order_type == OrderType.LIMIT
        assert result_limit.fill_probability > 0.0
        assert result_limit.fill_probability <= 1.0

    def test_simulate_fill_outcome(self):
        """simulate_fill_outcome should return deterministic bool with seed.

        With a fixed seed and high probability, the outcome should be
        True. With probability 0, the outcome should be False.
        """
        # High probability should usually fill
        assert isinstance(simulate_fill_outcome(0.999, seed=42), bool)

        # Probability = 1.0 should always fill
        assert simulate_fill_outcome(1.0, seed=42) is True

        # Probability = 0.0 should never fill
        assert simulate_fill_outcome(0.0, seed=42) is False

        # Same seed should give same result
        r1 = simulate_fill_outcome(0.8, seed=123)
        r2 = simulate_fill_outcome(0.8, seed=123)
        assert r1 == r2

    def test_fill_prob_zero_volume(self, caplog):
        """Zero ADV edge case should fall back gracefully to 1e6.

        When avg_daily_volume is zero, the model should log a warning
        and use a fallback volume rather than crashing.
        """
        with caplog.at_level("WARNING"):
            result = compute_fill_probability(
                order_type=OrderType.LIMIT,
                limit_price=149.50,
                market_price=150.00,
                order_size_shares=500,
                avg_daily_volume=0,
                time_of_day=TimeOfDay.MORNING,
                seed=42,
            )
        assert result.fill_probability > 0.0
        assert result.fill_probability <= 1.0
        assert any("Zero or negative avg_daily_volume" in r.message for r in caplog.records)

    def test_fill_prob_result_to_dict(self):
        """Verify to_dict() returns all fields with rounded values."""
        result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.50,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=2_000_000,
            time_of_day=TimeOfDay.MORNING,
            seed=42,
        )
        d = result.to_dict()
        expected_keys = {
            "order_type",
            "fill_probability",
            "distance_bps",
            "participation_rate",
            "time_factor",
            "volume_liquidity_score",
            "expected_partial_fill_pct",
            "will_fill",
        }
        assert set(d.keys()) == expected_keys
        assert d["order_type"] == "LIMIT"

    def test_fill_prob_result_format_display(self):
        """Verify format_display() returns a readable string with key metrics."""
        result = compute_fill_probability(
            order_type=OrderType.LIMIT,
            limit_price=149.50,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=2_000_000,
            time_of_day=TimeOfDay.MORNING,
            seed=42,
        )
        s = result.format_display()
        assert isinstance(s, str)
        assert "Fill Prob:" in s
        assert "%" in s
        assert "LIMIT" in s
        assert "bps away" in s


# ══════════════════════════════════════════════════════════════════════
# 7C - Borrow / Financing
# ══════════════════════════════════════════════════════════════════════


class TestBorrowFinancing:
    """Tests for the borrowing & financing cost model (Phase 7C)."""

    def test_borrow_cost_basic(self):
        """Short borrow with typical rate should compute correct cost.

        Formula: borrow_cost = short_value × borrow_rate × (days / 252)
        For $100K short at 0.5% annual for 5 days:
            = 100,000 × 0.005 × (5/252) ≈ $9.92
        """
        result = compute_borrow_cost(
            short_value=100_000, borrow_rate_annual=0.005, days_held=5
        )
        assert isinstance(result, BorrowCostResult)
        expected_cost = 100_000 * 0.005 * (5 / 252)
        assert result.borrow_cost_dollars == pytest.approx(expected_cost, rel=1e-3)
        assert result.borrow_cost_bps > 0
        assert result.short_value == pytest.approx(100_000, rel=1e-3)
        assert result.borrow_rate_annual == pytest.approx(0.005)

    def test_borrow_cost_zero_value(self):
        """Zero short value should return zero cost.

        No position means no borrow cost.
        """
        result = compute_borrow_cost(short_value=0, borrow_rate_annual=0.005, days_held=5)
        assert result.borrow_cost_dollars == 0.0
        assert result.borrow_cost_bps == 0.0

    def test_borrow_cost_zero_days(self):
        """Zero days held should return zero cost.

        If the position was never held, no borrow cost accrues.
        """
        result = compute_borrow_cost(
            short_value=100_000, borrow_rate_annual=0.005, days_held=0
        )
        assert result.borrow_cost_dollars == 0.0
        assert result.borrow_cost_bps == 0.0

    def test_margin_cost_basic(self):
        """Margin financing with typical rate should compute correctly.

        Formula: borrowed = position_value × (1 - margin_requirement)
                 margin_cost = borrowed × margin_rate × (days / 252)
        For $100K position at 50% margin, 6.5% annual, 10 days:
            borrowed = 50,000
            cost = 50,000 × 0.065 × (10/252) ≈ $128.97
        """
        result = compute_margin_cost(
            position_value=100_000,
            margin_requirement=0.50,
            margin_rate_annual=0.065,
            days_held=10,
        )
        assert isinstance(result, MarginCostResult)
        assert result.borrowed_amount == pytest.approx(50_000, rel=1e-3)
        expected_cost = 50_000 * 0.065 * (10 / 252)
        assert result.margin_cost_dollars == pytest.approx(expected_cost, rel=1e-3)
        assert result.margin_cost_bps > 0

    def test_margin_cost_no_leverage(self):
        """margin_requirement >= 1.0 should return zero cost (no leverage).

        When the full position is funded with cash (100% margin), there
        is no borrowed amount and hence no margin interest cost.
        """
        result = compute_margin_cost(
            position_value=100_000,
            margin_requirement=1.0,
            margin_rate_annual=0.065,
            days_held=10,
        )
        assert result.borrowed_amount == 0.0
        assert result.margin_cost_dollars == 0.0
        assert result.margin_cost_bps == 0.0

        # Also test margin_requirement > 1.0
        result_over = compute_margin_cost(
            position_value=100_000,
            margin_requirement=1.5,
            margin_rate_annual=0.065,
            days_held=10,
        )
        assert result_over.margin_cost_dollars == 0.0

    def test_financing_for_trade_long(self):
        """Long position: only margin cost if leverage enabled.

        A long position without leverage should have zero financing costs.
        A long position with leverage should have only margin cost
        (no borrow cost, no dividend cost).
        """
        # No leverage
        result_no_lev = compute_financing_for_trade(
            direction="LONG", position_value=100_000, days_held=5, use_leverage=False
        )
        assert result_no_lev.borrow_cost is None
        assert result_no_lev.margin_cost is None
        assert result_no_lev.dividend_cost_dollars == 0.0
        assert result_no_lev.total_financing_cost_dollars == 0.0

        # With leverage
        result_lev = compute_financing_for_trade(
            direction="LONG", position_value=100_000, days_held=5, use_leverage=True
        )
        assert result_lev.borrow_cost is None
        assert result_lev.margin_cost is not None
        assert result_lev.margin_cost.margin_cost_dollars > 0
        assert result_lev.dividend_cost_dollars == 0.0
        assert result_lev.total_financing_cost_dollars > 0

    def test_financing_for_trade_short(self):
        """Short position: borrow + dividend + optional margin.

        A short position should always have borrow costs. If leverage
        is enabled, it should also have margin costs. If dividends are
        specified, dividend costs should be included.
        """
        result = compute_financing_for_trade(
            direction="SHORT",
            position_value=50_000,
            days_held=10,
            use_leverage=True,
            borrow_rate_annual=0.005,
            dividend_per_share=0.50,
            shares=200,
        )
        assert result.borrow_cost is not None
        assert result.borrow_cost.borrow_cost_dollars > 0
        assert result.margin_cost is not None
        assert result.margin_cost.margin_cost_dollars > 0
        assert result.dividend_cost_dollars == pytest.approx(200 * 0.50)
        assert result.total_financing_cost_dollars > 0
        # Total should equal sum of components
        expected_total = (
            result.borrow_cost.borrow_cost_dollars
            + result.margin_cost.margin_cost_dollars
            + result.dividend_cost_dollars
        )
        assert result.total_financing_cost_dollars == pytest.approx(
            expected_total, rel=1e-3
        )

    def test_financing_htb(self):
        """Hard-to-borrow stocks should have higher borrow rate.

        When hard_to_borrow=True, the effective borrow rate should be
        borrow_rate_annual + htb_premium_rate.
        """
        result_etb = compute_financing_for_trade(
            direction="SHORT",
            position_value=50_000,
            days_held=10,
            borrow_rate_annual=0.005,
            hard_to_borrow=False,
        )
        result_htb = compute_financing_for_trade(
            direction="SHORT",
            position_value=50_000,
            days_held=10,
            borrow_rate_annual=0.005,
            hard_to_borrow=True,
            htb_premium_rate=0.10,
        )
        assert result_htb.borrow_cost is not None
        assert result_etb.borrow_cost is not None
        assert result_htb.borrow_cost.borrow_cost_dollars > result_etb.borrow_cost.borrow_cost_dollars
        # Effective rate for HTB = 0.005 + 0.10 = 0.105
        assert result_htb.borrow_cost.borrow_rate_annual == pytest.approx(0.105)

    def test_get_default_borrow_rate(self):
        """Verify SPY/ETF, large cap, small cap, and HTB rates.

        - Ultra-liquid ETFs (SPY, QQQ): 0.25%
        - Other ETFs: 0.50%
        - Large-cap stocks (default): 0.50%
        - Small-cap stocks: 3.00%
        - HTB/meme stocks (GME): 10.00%
        """
        # Ultra-liquid ETFs
        assert get_default_borrow_rate("SPY") == 0.0025
        assert get_default_borrow_rate("QQQ") == 0.0025
        assert get_default_borrow_rate("IVV") == 0.0025
        assert get_default_borrow_rate("VOO") == 0.0025

        # Other ETFs
        assert get_default_borrow_rate("XLF", is_etf=True) == 0.005

        # Large-cap stocks (default)
        assert get_default_borrow_rate("AAPL") == 0.005
        assert get_default_borrow_rate("MSFT") == 0.005

        # Small-cap stocks
        assert get_default_borrow_rate("RUTL", is_small_cap=True) == 0.03

        # HTB / meme stocks
        assert get_default_borrow_rate("GME") == 0.10
        assert get_default_borrow_rate("AMC") == 0.10

    def test_financing_summary(self, sample_trades_for_summary):
        """Aggregate financing across trades should compute correct totals.

        The summary should break down costs by direction and compute
        total financing cost as a fraction of P&L.
        """
        summary = compute_financing_summary(
            trades=sample_trades_for_summary,
            use_leverage=True,
        )
        assert isinstance(summary, dict)
        assert summary["trade_count"] == 3
        assert summary["total_borrow_cost"] > 0  # SHORT trades have borrow costs
        assert summary["total_margin_cost"] > 0  # use_leverage=True
        assert summary["total_financing_cost"] > 0
        assert "by_direction" in summary
        assert "LONG" in summary["by_direction"]
        assert "SHORT" in summary["by_direction"]
        # SHORT direction should have borrow cost
        assert summary["by_direction"]["SHORT"]["borrow_cost"] > 0
        # LONG direction should have margin cost but no borrow cost
        assert summary["by_direction"]["LONG"]["borrow_cost"] == 0.0
        assert summary["by_direction"]["LONG"]["margin_cost"] > 0

    def test_borrow_cost_result_format_display(self):
        """Verify BorrowCostResult.format_display() returns readable string."""
        result = compute_borrow_cost(
            short_value=100_000, borrow_rate_annual=0.005, days_held=5
        )
        s = result.format_display()
        assert isinstance(s, str)
        assert "Borrow:" in s
        assert "$" in s
        assert "0.5% ann" in s
        assert "5 days" in s
        assert "bps" in s

    def test_margin_cost_result_format_display(self):
        """Verify MarginCostResult.format_display() returns readable string."""
        result = compute_margin_cost(
            position_value=100_000,
            margin_requirement=0.50,
            margin_rate_annual=0.065,
            days_held=10,
        )
        s = result.format_display()
        assert isinstance(s, str)
        assert "Margin:" in s
        assert "$" in s
        assert "6.5% ann" in s
        assert "borrowed" in s
        assert "bps" in s


# ══════════════════════════════════════════════════════════════════════
# Integration Tests
# ══════════════════════════════════════════════════════════════════════


class TestPhase7Integration:
    """Integration tests for Phase 7 with RiskManager, TradeRecord, config."""

    def test_risk_params_phase7_defaults(self):
        """Phase 7 params should default to disabled in RiskParams.

        All Phase 7 execution modeling features should be off by default
        for backward compatibility.
        """
        params = RiskParams()
        assert params.enable_market_impact is False
        assert params.enable_fill_probability is False
        assert params.enable_borrow_costs is False
        assert params.enable_margin_costs is False
        assert params.avg_daily_volume == 1e6
        assert params.impact_gamma == 0.314
        assert params.impact_eta == 0.142
        assert params.borrow_rate_annual == 0.005
        assert params.hard_to_borrow is False
        assert params.htb_premium_rate == 0.10
        assert params.margin_rate_annual == 0.065
        assert params.margin_requirement == 0.50

    def test_risk_manager_with_market_impact(
        self, risk_params_phase7_enabled, sample_brick
    ):
        """RiskManager._make_result with market impact enabled should populate impact fields.

        When enable_market_impact is True, the TradeResult should have
        non-zero market_impact_bps and market_impact_cost.
        """
        params = risk_params_phase7_enabled
        # Only enable market impact for this test
        params.enable_fill_probability = False
        params.enable_borrow_costs = False
        params.enable_margin_costs = False

        rm = RiskManager(params=params)
        pos = rm.open_position(
            entry_price=100.0,
            direction=SignalDirection.LONG,
            brick_size=0.5,
            entry_ts=1700000000.0,
            entry_brick_idx=0,
        )
        # Simulate enough bricks to hit time stop (10 bricks)
        for i in range(params.time_stop_bricks):
            brick = Brick(
                index=i + 1,
                direction=BrickDirection.UP,
                open_price=100.5 + i * 0.5,
                close_price=101.0 + i * 0.5,
                high=101.5 + i * 0.5,
                low=100.0 + i * 0.5,
                timestamp=1700000000.0 + (i + 1) * 60,
                brick_size=0.5,
            )
            result = rm.evaluate(brick)
            if result is not None:
                break

        assert result is not None
        assert result.market_impact_bps > 0
        assert result.market_impact_cost >= 0

    def test_risk_manager_with_borrow_costs(
        self, risk_params_phase7_enabled, sample_brick
    ):
        """Short trade with borrow costs enabled should populate borrow_cost field.

        When enable_borrow_costs is True and the position is SHORT,
        the TradeResult should have non-zero borrow_cost.
        """
        params = risk_params_phase7_enabled
        params.enable_market_impact = False
        params.enable_fill_probability = False
        params.enable_margin_costs = False

        rm = RiskManager(params=params)
        pos = rm.open_position(
            entry_price=100.0,
            direction=SignalDirection.SHORT,
            brick_size=0.5,
            entry_ts=1700000000.0,
            entry_brick_idx=0,
        )
        # Simulate enough bricks to trigger time stop
        for i in range(params.time_stop_bricks):
            brick = Brick(
                index=i + 1,
                direction=BrickDirection.DOWN,
                open_price=99.5 - i * 0.5,
                close_price=99.0 - i * 0.5,
                high=100.0 - i * 0.5,
                low=98.5 - i * 0.5,
                timestamp=1700000000.0 + (i + 1) * 60,
                brick_size=0.5,
            )
            result = rm.evaluate(brick)
            if result is not None:
                break

        assert result is not None
        assert result.borrow_cost > 0

    def test_risk_manager_with_margin_costs(
        self, risk_params_phase7_enabled, sample_brick
    ):
        """Trade with margin costs enabled should populate margin_cost field.

        When enable_margin_costs is True, the TradeResult should have
        non-zero margin_cost.
        """
        params = risk_params_phase7_enabled
        params.enable_market_impact = False
        params.enable_fill_probability = False
        params.enable_borrow_costs = False

        rm = RiskManager(params=params)
        pos = rm.open_position(
            entry_price=100.0,
            direction=SignalDirection.LONG,
            brick_size=0.5,
            entry_ts=1700000000.0,
            entry_brick_idx=0,
        )
        # Simulate enough bricks to trigger time stop
        for i in range(params.time_stop_bricks):
            brick = Brick(
                index=i + 1,
                direction=BrickDirection.UP,
                open_price=100.5 + i * 0.5,
                close_price=101.0 + i * 0.5,
                high=101.5 + i * 0.5,
                low=100.0 + i * 0.5,
                timestamp=1700000000.0 + (i + 1) * 60,
                brick_size=0.5,
            )
            result = rm.evaluate(brick)
            if result is not None:
                break

        assert result is not None
        assert result.margin_cost > 0

    def test_risk_manager_phase7_disabled(
        self, risk_params_phase7_disabled, sample_brick
    ):
        """With Phase 7 disabled, execution costs should be zero (backward compat).

        When all Phase 7 features are disabled, the TradeResult should
        have zero values for all Phase 7 fields, preserving backward
        compatibility with existing backtests.
        """
        params = risk_params_phase7_disabled
        rm = RiskManager(params=params)
        pos = rm.open_position(
            entry_price=100.0,
            direction=SignalDirection.LONG,
            brick_size=0.5,
            entry_ts=1700000000.0,
            entry_brick_idx=0,
        )
        # Simulate enough bricks to trigger time stop
        for i in range(params.time_stop_bricks):
            brick = Brick(
                index=i + 1,
                direction=BrickDirection.UP,
                open_price=100.5 + i * 0.5,
                close_price=101.0 + i * 0.5,
                high=101.5 + i * 0.5,
                low=100.0 + i * 0.5,
                timestamp=1700000000.0 + (i + 1) * 60,
                brick_size=0.5,
            )
            result = rm.evaluate(brick)
            if result is not None:
                break

        assert result is not None
        assert result.market_impact_bps == 0.0
        assert result.market_impact_cost == 0.0
        assert result.fill_probability == 1.0
        assert result.borrow_cost == 0.0
        assert result.margin_cost == 0.0
        assert result.dividend_cost == 0.0
        assert result.financing_cost_total == 0.0

    def test_trade_record_phase7_fields(self):
        """TradeRecord has Phase 7 fields, all defaulting to zero/empty.

        The TradeRecord dataclass should include all Phase 7 execution
        modeling fields with sensible defaults.
        """
        record = TradeRecord(
            trade_id="R000001",
            symbol="SPY",
            direction=SignalDirection.LONG,
            pattern_type=PatternType.BULL_RUN,
            entry_price=100.0,
            exit_price=102.5,
            entry_ts=1700000000.0,
            exit_ts=1700000600.0,
            pnl_bricks=5.0,
            pnl_dollars=250.0,
            status=PositionStatus.CLOSED_TP,
            bricks_held=5,
            signal_strength=0.8,
        )
        assert record.market_impact_bps == 0.0
        assert record.market_impact_cost == 0.0
        assert record.fill_probability == 1.0
        assert record.was_filled is True
        assert record.borrow_cost == 0.0
        assert record.margin_cost == 0.0
        assert record.dividend_cost == 0.0
        assert record.financing_cost_total == 0.0

    def test_renko_config_phase7_params(self):
        """RenkoConfig should have all Phase 7 params with correct defaults.

        All Phase 7 parameters should be present on RenkoConfig and
        default to disabled for backward compatibility.
        """
        config = RenkoConfig()
        assert config.enable_market_impact is False
        assert config.avg_daily_volume == 1e6
        assert config.impact_gamma == 0.314
        assert config.impact_eta == 0.142
        assert config.enable_fill_probability is False
        assert config.enable_borrow_costs is False
        assert config.borrow_rate_annual == 0.005
        assert config.hard_to_borrow is False
        assert config.htb_premium_rate == 0.10
        assert config.enable_margin_costs is False
        assert config.margin_rate_annual == 0.065
        assert config.margin_requirement == 0.50

    def test_backtest_request_phase7_params(self):
        """RenkoBacktestRequest should have all Phase 7 params with correct defaults.

        The Pydantic model for the backtest API should expose all Phase 7
        parameters with the same defaults as RenkoConfig.
        """
        from renko.router import RenkoBacktestRequest

        # Create a minimal valid request (need at least 50 prices)
        prices = [100.0 + i * 0.1 for i in range(50)]
        req = RenkoBacktestRequest(prices=prices)
        assert req.enable_market_impact is False
        assert req.avg_daily_volume == 1e6
        assert req.impact_gamma == 0.314
        assert req.impact_eta == 0.142
        assert req.enable_fill_probability is False
        assert req.enable_borrow_costs is False
        assert req.borrow_rate_annual == 0.005
        assert req.hard_to_borrow is False
        assert req.htb_premium_rate == 0.10
        assert req.enable_margin_costs is False
        assert req.margin_rate_annual == 0.065
        assert req.margin_requirement == 0.50


# ══════════════════════════════════════════════════════════════════════
# Additional Edge Case & Robustness Tests
# ══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Additional edge case and robustness tests for Phase 7."""

    def test_market_impact_negative_price(self):
        """Negative price should return trivial zero-impact result."""
        result = compute_market_impact(
            shares=10_000, price=-150.0, avg_daily_volume=5_000_000, daily_volatility=0.02
        )
        assert result.total_impact_bps == 0.0
        assert result.impact_cost_dollars == 0.0

    def test_market_impact_negative_shares(self):
        """Negative shares should return trivial zero-impact result."""
        result = compute_market_impact(
            shares=-10_000, price=150.0, avg_daily_volume=5_000_000, daily_volatility=0.02
        )
        assert result.total_impact_bps == 0.0

    def test_estimate_adv_empty_prices(self):
        """Empty price series should return default estimate without crashing."""
        adv = estimate_adv_from_prices([])
        # Should return default: turnover * shares_outstanding
        assert adv == pytest.approx(0.01 * 1e9)

    def test_estimate_adv_invalid_params(self):
        """Zero/negative turnover or shares_outstanding should return 0."""
        adv = estimate_adv_from_prices(
            [100, 101], assumed_turnover_rate=0, shares_outstanding=1e9
        )
        assert adv == 0.0

        adv2 = estimate_adv_from_prices(
            [100, 101], assumed_turnover_rate=0.01, shares_outstanding=0
        )
        assert adv2 == 0.0

    def test_fill_prob_stop_limit_order(self):
        """STOP_LIMIT order should use logit model like LIMIT."""
        result = compute_fill_probability(
            order_type=OrderType.STOP_LIMIT,
            limit_price=149.50,
            market_price=150.00,
            order_size_shares=500,
            avg_daily_volume=2_000_000,
            time_of_day=TimeOfDay.MORNING,
            seed=42,
        )
        assert result.order_type == OrderType.STOP_LIMIT
        assert 0.01 <= result.fill_probability <= 1.0
        assert result.distance_bps > 0

    def test_borrow_cost_negative_days(self, caplog):
        """Negative days_held should be clamped to zero with warning."""
        with caplog.at_level("WARNING"):
            result = compute_borrow_cost(
                short_value=100_000, borrow_rate_annual=0.005, days_held=-5
            )
        assert result.borrow_cost_dollars == 0.0
        assert any("Negative days_held" in r.message for r in caplog.records)

    def test_margin_cost_negative_days(self, caplog):
        """Negative days_held should be clamped to zero with warning."""
        with caplog.at_level("WARNING"):
            result = compute_margin_cost(
                position_value=100_000,
                margin_requirement=0.50,
                margin_rate_annual=0.065,
                days_held=-5,
            )
        assert result.margin_cost_dollars == 0.0
        assert any("Negative days_held" in r.message for r in caplog.records)

    def test_financing_for_trade_zero_value(self):
        """Zero position value should return zero financing costs."""
        result = compute_financing_for_trade(
            direction="SHORT", position_value=0, days_held=5
        )
        assert result.total_financing_cost_dollars == 0.0
        assert result.borrow_cost is None
        assert result.margin_cost is None

    def test_financing_summary_empty(self):
        """Empty trade list should return zero summary."""
        summary = compute_financing_summary(trades=[])
        assert summary["total_borrow_cost"] == 0.0
        assert summary["total_margin_cost"] == 0.0
        assert summary["total_dividend_cost"] == 0.0
        assert summary["total_financing_cost"] == 0.0
        assert summary["trade_count"] == 0

    def test_fill_prob_for_backtest_invalid_direction(self, caplog):
        """Invalid direction should default to LONG with a warning."""
        with caplog.at_level("WARNING"):
            result = compute_fill_probability_for_backtest(
                direction="SIDEWAYS",
                entry_price=150.0,
                market_price=150.0,
                order_size_usd=50_000,
                seed=42,
            )
        assert result is not None
        assert any("Unrecognized direction" in r.message for r in caplog.records)

    def test_trade_record_to_dict_includes_phase7(self):
        """TradeRecord.to_dict() should include Phase 7 fields."""
        record = TradeRecord(
            trade_id="R000001",
            symbol="SPY",
            direction=SignalDirection.LONG,
            pattern_type=PatternType.BULL_RUN,
            entry_price=100.0,
            exit_price=102.5,
            entry_ts=1700000000.0,
            exit_ts=1700000600.0,
            pnl_bricks=5.0,
            pnl_dollars=250.0,
            status=PositionStatus.CLOSED_TP,
            bricks_held=5,
            signal_strength=0.8,
            market_impact_bps=3.2,
            market_impact_cost=48.0,
            fill_probability=0.95,
            borrow_cost=5.0,
            margin_cost=8.0,
            dividend_cost=0.0,
            financing_cost_total=13.0,
        )
        d = record.to_dict()
        assert d["market_impact_bps"] == 3.2
        assert d["market_impact_cost"] == 48.0
        assert d["fill_probability"] == 0.95
        assert d["borrow_cost"] == 5.0
        assert d["margin_cost"] == 8.0
        assert d["dividend_cost"] == 0.0
        assert d["financing_cost_total"] == 13.0

    def test_trade_result_to_dict_includes_phase7(self):
        """TradeResult.to_dict() should include Phase 7 fields."""
        result = TradeResult(
            entry_price=100.0,
            exit_price=102.5,
            direction=SignalDirection.LONG,
            pnl_bricks=5.0,
            pnl_dollars=250.0,
            status=PositionStatus.CLOSED_TP,
            entry_ts=1700000000.0,
            exit_ts=1700000600.0,
            bricks_held=5,
            peak_profit_bricks=5.0,
            market_impact_bps=3.2,
            market_impact_cost=48.0,
            fill_probability=0.95,
            borrow_cost=5.0,
            margin_cost=8.0,
            dividend_cost=0.0,
            financing_cost_total=13.0,
        )
        d = result.to_dict()
        assert d["market_impact_bps"] == 3.2
        assert d["market_impact_cost"] == 48.0
        assert d["fill_probability"] == 0.95
        assert d["borrow_cost"] == 5.0
        assert d["margin_cost"] == 8.0
        assert d["dividend_cost"] == 0.0
        assert d["financing_cost_total"] == 13.0
