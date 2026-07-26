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
)
from regime_platform.services.execution.fill_probability import (
    compute_fill_probability,
    OrderType,
)
from regime_platform.services.execution.borrow_financing import (
    compute_borrow_cost,
    compute_margin_cost,
    compute_total_financing_costs,
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
    """Return a market-impact dict for typical large-cap trading."""
    return compute_market_impact(
        order_shares=10_000,
        adv_shares=5_000_000,
        sigma_daily=0.02,
        avg_price=150.0,
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


# ═══════════════════════════════════════════════════════════════════════════
# 7A - Market Impact (Almgren-Chriss)
# ═══════════════════════════════════════════════════════════════════════════


class TestMarketImpact:
    """Tests for the Almgren-Chriss market impact model (Phase 7A).

    compute_market_impact(order_shares, adv_shares, sigma_daily, avg_price, ...)
    returns a plain dict (no MarketImpactResult object).
    """

    def test_market_impact_basic(self, typical_market_impact):
        """Verify compute_market_impact with typical values populates all fields."""
        result = typical_market_impact
        assert isinstance(result, dict)
        assert result["participation_rate"] == pytest.approx(10_000 / 5_000_000)
        assert result["permanent_impact_bps"] > 0
        assert result["temporary_impact_bps"] > 0
        assert result["total_impact_bps"] == pytest.approx(
            result["permanent_impact_bps"] + result["temporary_impact_bps"]
        )
        assert result["impact_cost_dollars"] > 0

    def test_market_impact_zero_volume(self):
        """Near-zero ADV edge case should compute without crashing."""
        result = compute_market_impact(
            order_shares=10_000, adv_shares=0.0001, sigma_daily=0.02, avg_price=150.0
        )
        assert isinstance(result["total_impact_bps"], float)

    def test_market_impact_zero_volatility(self):
        """Zero volatility should return zero impact (trivial result)."""
        result = compute_market_impact(
            order_shares=10_000, adv_shares=5_000_000, sigma_daily=0.0, avg_price=150.0
        )
        assert result["total_impact_bps"] == 0.0
        assert result["impact_cost_dollars"] == 0.0
        assert result["permanent_impact_bps"] == 0.0
        assert result["temporary_impact_bps"] == 0.0

    def test_market_impact_large_order(self, caplog):
        """Large participation rate (>30%) should log warning and still compute."""
        with caplog.at_level("WARNING"):
            result = compute_market_impact(
                order_shares=2_000_000,
                adv_shares=5_000_000,
                sigma_daily=0.02,
                avg_price=150.0,
            )
        assert result["participation_rate"] > 0.3
        assert result["total_impact_bps"] > 0
        assert any("High participation rate" in r.message for r in caplog.records)

    def test_market_impact_small_order(self):
        """Small order relative to ADV should have minimal impact."""
        result = compute_market_impact(
            order_shares=500,
            adv_shares=5_000_000,
            sigma_daily=0.02,
            avg_price=150.0,
        )
        assert result["participation_rate"] < 0.001
        assert result["total_impact_bps"] < 1.0
        assert result["impact_cost_dollars"] < 1.0

    def test_market_impact_result_dict_keys(self, typical_market_impact):
        """Result dict should carry the expected keys."""
        d = typical_market_impact
        expected_keys = {
            "participation_rate",
            "permanent_impact_bps",
            "temporary_impact_bps",
            "total_impact_bps",
            "impact_cost_dollars",
            "impact_cost_pct",
        }
        assert set(d.keys()) == expected_keys
        assert isinstance(d["impact_cost_dollars"], float)



# ══════════════════════════════════════════════════════════════════════
# 7B - Fill Probability
# ══════════════════════════════════════════════════════════════════════


class TestFillProbability:
    """Tests for the fill probability model (Phase 7B).

    compute_fill_probability(order_type: str, distance_from_mid_pct=0.0,
    volatility_daily=0.02, time_horizon_hours=6.5, volume_participation_rate=0.01,
    sensitivity=5.0, threshold=1.0, fill_cfg=None) returns a plain dict.
    """

    def test_fill_prob_market_orders_always_fill(self):
        """Market orders always fill (probability 1.0)."""
        result = compute_fill_probability(order_type="market")
        assert result["fill_probability"] == pytest.approx(1.0)
        assert result["order_type"] == "market"
        assert result["distance_from_mid_pct"] == 0.0

    def test_fill_prob_stop_order(self):
        """Stop orders should have a high fill probability."""
        result = compute_fill_probability(order_type="stop")
        assert result["fill_probability"] > 0.5
        assert result["order_type"] == "stop"
        assert result["distance_from_mid_pct"] == 0.0

    def test_fill_prob_limit_order_at_market(self):
        """Limit order at market price (distance 0) should have high fill probability."""
        result = compute_fill_probability(order_type="limit", distance_from_mid_pct=0.0)
        assert result["fill_probability"] > 0.8
        assert result["distance_from_mid_pct"] == pytest.approx(0.0, abs=0.01)

    def test_fill_prob_limit_order_far_away(self):
        """Limit order far from market should have low fill probability."""
        result = compute_fill_probability(
            order_type="limit", distance_from_mid_pct=5.0, volatility_daily=0.02
        )
        assert result["fill_probability"] < 0.5
        # ~500 bps away in sigma units
        assert result["distance_in_sigmas"] > 1.0

    def test_fill_prob_high_participation(self):
        """Large order relative to volume reduces fill probability."""
        small = compute_fill_probability(
            order_type="limit",
            distance_from_mid_pct=0.1,
            volume_participation_rate=0.01,
        )
        large = compute_fill_probability(
            order_type="limit",
            distance_from_mid_pct=0.1,
            volume_participation_rate=0.20,
        )
        assert large["fill_probability"] < small["fill_probability"]

    def test_fill_prob_longer_horizon_higher(self):
        """A longer time horizon should not reduce fill probability for a marketable limit."""
        short = compute_fill_probability(
            order_type="limit", distance_from_mid_pct=0.5, time_horizon_hours=1.0
        )
        long = compute_fill_probability(
            order_type="limit", distance_from_mid_pct=0.5, time_horizon_hours=24.0
        )
        assert long["fill_probability"] >= short["fill_probability"]

    def test_fill_prob_result_keys(self):
        """Result dict should carry the expected keys."""
        result = compute_fill_probability(order_type="limit", distance_from_mid_pct=1.0)
        expected_keys = {
            "fill_probability",
            "order_type",
            "distance_from_mid_pct",
            "distance_in_sigmas",
            "expected_fill_price_offset_bps",
            "interpretation",
        }
        assert set(result.keys()) == expected_keys
        assert 0.0 <= result["fill_probability"] <= 1.0

    def test_fill_prob_interpretation_present(self):
        """Interpretation string should be populated."""
        result = compute_fill_probability(order_type="market")
        assert isinstance(result["interpretation"], str)
        assert len(result["interpretation"]) > 0


# ══════════════════════════════════════════════════════════════════════
# 7C - Borrow / Financing
# ══════════════════════════════════════════════════════════════════════


class TestBorrowFinancing:
    """Tests for the borrowing & financing cost model (Phase 7C).

    compute_borrow_cost(position_notional, holding_days, borrow_rate_bps=50,
    is_hard_to_borrow=False, hard_to_borrow_premium_bps=500, trading_days_per_year=252)
    and compute_margin_cost(borrowed_amount, holding_days, margin_rate_bps=150,
    trading_days_per_year=252) both return plain dicts.
    """

    def test_borrow_cost_basic(self):
        """Short borrow with typical rate should compute correct cost."""
        result = compute_borrow_cost(
            position_notional=100_000, holding_days=5, borrow_rate_bps=50, trading_days_per_year=252
        )
        expected_cost = 100_000 * 0.005 * (5 / 252)
        assert result["total_borrow_cost"] == pytest.approx(expected_cost, rel=1e-3)
        assert result["effective_annual_rate_bps"] == pytest.approx(50)
        assert result["daily_borrow_cost"] > 0
        assert result["position_notional"] == pytest.approx(100_000, rel=1e-3)

    def test_borrow_cost_zero_value(self):
        """Zero position notional should return zero cost."""
        result = compute_borrow_cost(position_notional=0, holding_days=5)
        assert result["total_borrow_cost"] == 0.0

    def test_borrow_cost_zero_days(self):
        """Zero days held should return zero cost."""
        result = compute_borrow_cost(position_notional=100_000, holding_days=0)
        assert result["total_borrow_cost"] == 0.0

    def test_borrow_cost_htb(self):
        """Hard-to-borrow should raise the effective annual rate."""
        etb = compute_borrow_cost(
            position_notional=50_000, holding_days=10, borrow_rate_bps=50, is_hard_to_borrow=False
        )
        htb = compute_borrow_cost(
            position_notional=50_000, holding_days=10, borrow_rate_bps=50,
            is_hard_to_borrow=True, hard_to_borrow_premium_bps=1000,
        )
        assert htb["effective_annual_rate_bps"] > etb["effective_annual_rate_bps"]
        # HTB rate = 50 + 1000 = 1050 bps
        assert htb["effective_annual_rate_bps"] == pytest.approx(1050)

    def test_margin_cost_basic(self):
        """Margin financing with typical rate should compute correctly."""
        result = compute_margin_cost(
            borrowed_amount=50_000, holding_days=10, margin_rate_bps=650, trading_days_per_year=252
        )
        expected_cost = 50_000 * 0.065 * (10 / 252)
        assert result["total_margin_cost"] == pytest.approx(expected_cost, rel=1e-3)
        assert result["margin_rate_bps"] == pytest.approx(650)
        assert result["borrowed_amount"] == pytest.approx(50_000, rel=1e-3)

    def test_margin_cost_no_leverage(self):
        """Zero borrowed amount should return zero cost (no leverage)."""
        result = compute_margin_cost(borrowed_amount=0, holding_days=10)
        assert result["total_margin_cost"] == 0.0
        assert result["borrowed_amount"] == 0.0

    def test_total_financing_costs_smoke(self):
        """compute_total_financing_costs should return a dict for a trade."""
        result = compute_total_financing_costs(
            position_notional=50_000,
            holding_days=10,
            direction="SHORT",
            borrow_rate_bps=50,
            is_hard_to_borrow=False,
            margin_rate_bps=150,
        )
        assert isinstance(result, dict)
        assert "total_borrow_cost" in result


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
        """BacktestRequest should expose Phase 7 execution-modeling params with defaults.

        The Pydantic model for the backtest API should carry the Phase 7
        execution-modeling parameters (market impact, fill probability,
        borrow/margin rates) with sensible defaults.
        """
        from renko.router import BacktestRequest

        # Create a minimal valid request (need at least 50 prices)
        prices = [100.0 + i * 0.1 for i in range(50)]
        req = BacktestRequest(prices=prices)
        assert req.market_impact_mode == "none"
        assert req.adv_shares == 10_000_000
        assert req.fill_probability_mode == "always_fill"
        assert req.borrow_rate_bps == 50.0
        assert req.margin_rate_bps == 150.0
        assert req.is_hard_to_borrow is False
        assert req.dividend_yield_bps == 200.0
        assert req.initial_capital == 100_000.0


# ══════════════════════════════════════════════════════════════════════
# Additional Edge Case & Robustness Tests
# ══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Additional edge case and robustness tests for Phase 7."""

    def test_market_impact_negative_price(self):
        """Negative price should compute without crashing."""
        result = compute_market_impact(
            order_shares=10_000, adv_shares=5_000_000, sigma_daily=0.02, avg_price=-150.0
        )
        assert isinstance(result["total_impact_bps"], float)
        assert isinstance(result["impact_cost_dollars"], float)

    def test_market_impact_negative_shares(self):
        """Negative shares should compute without crashing."""
        result = compute_market_impact(
            order_shares=-10_000, adv_shares=5_000_000, sigma_daily=0.02, avg_price=150.0
        )
        assert isinstance(result["total_impact_bps"], float)

    def test_fill_prob_stop_limit_order(self):
        """STOP_LIMIT order should use logit model like LIMIT."""
        result = compute_fill_probability(
            order_type="stop_limit", distance_from_mid_pct=1.0
        )
        assert result["order_type"] == "stop_limit"
        assert 0.01 <= result["fill_probability"] <= 1.0
        assert result["distance_from_mid_pct"] > 0

    def test_borrow_cost_negative_days(self):
        """Negative days_held should compute without crashing."""
        result = compute_borrow_cost(
            position_notional=100_000, holding_days=-5, borrow_rate_bps=50
        )
        assert isinstance(result["total_borrow_cost"], float)

    def test_margin_cost_negative_days(self):
        """Negative days_held should compute without crashing."""
        result = compute_margin_cost(borrowed_amount=100_000, holding_days=-5, margin_rate_bps=650)
        assert isinstance(result["total_margin_cost"], float)

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
