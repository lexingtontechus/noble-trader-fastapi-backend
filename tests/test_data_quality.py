"""
Tests for Phase 5: Data Quality & Integrity.

Covers:
  5A — Survivorship bias elimination
  5B — Corporate action adjustments
  5C — Data versioning & lineage
  5D — Look-ahead bias audit
  5E — Price validation
"""

import math
from datetime import date, datetime, timezone

import pytest

from regime_platform.services.data_quality import (
    compute_data_hash,
    compute_full_data_hash,
    build_data_source_metadata,
    validate_prices,
    check_survivorship_bias,
    adjust_prices,
    filter_prices_by_universe,
    LookAheadAuditor,
    UniverseSnapshotService,
    CorporateActionService,
)


# ═══════════════════════════════════════════════════════════════════════════
# 5A — Survivorship Bias
# ═══════════════════════════════════════════════════════════════════════════


class TestSurvivorshipBias:
    def test_current_constituents_mode_warns(self):
        """Using current_constituents mode should produce a warning."""
        result = check_survivorship_bias("SPY", "current_constituents")
        assert result["warning"] is True
        assert "survivorship bias" in result["message"].lower()

    def test_pit_constituents_mode_no_warning(self):
        """Using pit_constituents mode should produce no warning."""
        result = check_survivorship_bias("SPY", "pit_constituents", "sp500")
        assert result["warning"] is False
        assert "eliminated" in result["message"].lower()

    def test_pit_result_includes_index_name(self):
        """PIT result should include the index name."""
        result = check_survivorship_bias("SPY", "pit_constituents", "sp500")
        assert result["index_name"] == "sp500"

    def test_filter_prices_empty_constituents(self):
        """Empty constituent set should return all prices (backward compat)."""
        prices = [100.0, 101.0, 102.0]
        result = filter_prices_by_universe(prices, None, "SPY", set())
        assert result == prices

    def test_filter_prices_symbol_in_universe(self):
        """If symbol is in the universe, no filtering needed."""
        prices = [100.0, 101.0, 102.0]
        result = filter_prices_by_universe(prices, None, "SPY", {"SPY", "AAPL"})
        assert result == prices


# ═══════════════════════════════════════════════════════════════════════════
# 5B — Corporate Action Adjustments
# ═══════════════════════════════════════════════════════════════════════════


class TestCorporateActionAdjustments:
    def test_raw_mode_no_adjustment(self):
        """Raw mode should return prices unchanged."""
        prices = [100.0, 101.0, 102.0]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        actions = [{"action_type": "split", "ex_date": "2024-01-02", "factor": 0.25}]
        result, adj = adjust_prices(prices, timestamps, actions, mode="raw")
        assert result == prices
        assert adj == []

    def test_no_actions_no_adjustment(self):
        """No corporate actions should return prices unchanged."""
        prices = [100.0, 101.0, 102.0]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        result, adj = adjust_prices(prices, timestamps, [], mode="split_adjusted")
        assert result == prices
        assert adj == []

    def test_split_adjustment(self):
        """4:1 stock split should adjust pre-split prices by factor 0.25."""
        # Prices before Jan 15 should be adjusted by 0.25 (4:1 split)
        prices = [400.0, 405.0, 100.0, 102.0]
        timestamps = ["2024-01-13", "2024-01-14", "2024-01-15", "2024-01-16"]
        actions = [
            {
                "action_type": "split",
                "ex_date": "2024-01-15",
                "factor": 0.25,
                "description": "4:1 stock split",
            }
        ]
        result, adj = adjust_prices(prices, timestamps, actions, mode="split_adjusted")
        # Pre-split prices should be adjusted: 400 * 0.25 = 100, 405 * 0.25 = 101.25
        assert result[0] == pytest.approx(100.0)
        assert result[1] == pytest.approx(101.25)
        # Post-split prices should be unchanged
        assert result[2] == 100.0
        assert result[3] == 102.0
        assert len(adj) == 1
        assert adj[0]["type"] == "split"

    def test_dividend_adjustment_fully_adjusted(self):
        """Fully adjusted mode should reduce pre-dividend prices."""
        prices = [100.0, 99.5, 99.0, 98.5]
        timestamps = ["2024-03-01", "2024-03-02", "2024-03-03", "2024-03-04"]
        actions = [
            {
                "action_type": "dividend",
                "ex_date": "2024-03-03",
                "factor": 1.0,
                "amount": 2.0,
                "description": "$2.00 quarterly dividend",
            }
        ]
        result, adj = adjust_prices(prices, timestamps, actions, mode="fully_adjusted")
        # Pre-dividend prices should be reduced by dividend amount
        assert result[0] == pytest.approx(98.0)  # 100 - 2.0
        assert result[1] == pytest.approx(97.5)  # 99.5 - 2.0
        # Post-dividend prices unchanged
        assert result[2] == 99.0
        assert result[3] == 98.5
        assert len(adj) == 1
        assert adj[0]["type"] == "dividend"

    def test_split_adjusted_ignores_dividends(self):
        """Split_adjusted mode should NOT apply dividend adjustments."""
        prices = [100.0, 99.5, 99.0, 98.5]
        timestamps = ["2024-03-01", "2024-03-02", "2024-03-03", "2024-03-04"]
        actions = [
            {
                "action_type": "dividend",
                "ex_date": "2024-03-03",
                "factor": 1.0,
                "amount": 2.0,
                "description": "$2.00 dividend",
            }
        ]
        result, adj = adjust_prices(prices, timestamps, actions, mode="split_adjusted")
        # No adjustment applied (dividends only in fully_adjusted mode)
        assert result == prices
        assert adj == []


# ═══════════════════════════════════════════════════════════════════════════
# 5C — Data Versioning & Lineage
# ═══════════════════════════════════════════════════════════════════════════


class TestDataVersioning:
    def test_data_hash_deterministic(self):
        """Same inputs should produce the same hash."""
        prices = [100.0, 101.0, 102.0, 103.0]
        h1 = compute_data_hash(prices, "SPY")
        h2 = compute_data_hash(prices, "SPY")
        assert h1 == h2

    def test_data_hash_different_symbol(self):
        """Different symbols should produce different hashes."""
        prices = [100.0, 101.0, 102.0]
        h1 = compute_data_hash(prices, "SPY")
        h2 = compute_data_hash(prices, "AAPL")
        assert h1 != h2

    def test_data_hash_different_prices(self):
        """Different prices should produce different hashes."""
        h1 = compute_data_hash([100.0, 101.0], "SPY")
        h2 = compute_data_hash([100.0, 102.0], "SPY")
        assert h1 != h2

    def test_data_hash_empty(self):
        """Empty price array should produce a valid hash."""
        h = compute_data_hash([], "SPY")
        assert len(h) == 64  # SHA-256 hex digest

    def test_full_data_hash_deterministic(self):
        """Full hash should be deterministic."""
        prices = [100.0, 101.0, 102.0]
        h1 = compute_full_data_hash(prices)
        h2 = compute_full_data_hash(prices)
        assert h1 == h2

    def test_full_data_hash_different(self):
        """Full hash should differ for different prices."""
        h1 = compute_full_data_hash([100.0, 101.0])
        h2 = compute_full_data_hash([100.0, 102.0])
        assert h1 != h2

    def test_build_data_source_metadata(self):
        """Metadata should contain expected fields."""
        meta = build_data_source_metadata(
            source="api",
            symbol="SPY",
            price_count=500,
            price_adjustment="split_adjusted",
            universe_mode="pit_constituents",
            index_name="sp500",
        )
        assert meta["source"] == "api"
        assert meta["symbol"] == "SPY"
        assert meta["price_count"] == 500
        assert meta["price_adjustment"] == "split_adjusted"
        assert meta["universe_mode"] == "pit_constituents"
        assert meta["index_name"] == "sp500"
        assert "fetch_date" in meta


# ═══════════════════════════════════════════════════════════════════════════
# 5D — Look-Ahead Bias Audit
# ═══════════════════════════════════════════════════════════════════════════


class TestLookAheadAudit:
    def test_disabled_by_default(self):
        """Auditor disabled should not flag anything."""
        auditor = LookAheadAuditor(enabled=False)
        # Even accessing future data shouldn't flag
        auditor.set_position(5, 100)
        result = auditor.check_access("prices", 50, "test")
        assert result is True
        audit = auditor.get_results()
        assert audit["enabled"] is False

    def test_clean_access_no_warning(self):
        """Accessing current or past data should be clean."""
        auditor = LookAheadAuditor(enabled=True)
        auditor.set_position(5, 100)
        auditor.check_access("prices", 3, "HMM fit")
        auditor.check_access("prices", 5, "current bar")
        result = auditor.get_results()
        assert result["clean"] is True
        assert result["warning_count"] == 0

    def test_look_ahead_detected(self):
        """Accessing future data should trigger a warning."""
        auditor = LookAheadAuditor(enabled=True)
        auditor.set_position(5, 100)
        is_clean = auditor.check_access("prices", 50, "HMM prediction")
        assert is_clean is False
        result = auditor.get_results()
        assert result["clean"] is False
        assert result["warning_count"] == 1
        assert result["warnings"][0]["look_ahead_by"] == 45

    def test_multiple_warnings(self):
        """Multiple look-ahead accesses should all be recorded."""
        auditor = LookAheadAuditor(enabled=True)
        auditor.set_position(5, 100)
        auditor.check_access("prices", 10, "test1")
        auditor.check_access("prices", 50, "test2")
        result = auditor.get_results()
        assert result["warning_count"] == 2

    def test_warning_cap_at_50(self):
        """Warnings should be capped at 50."""
        auditor = LookAheadAuditor(enabled=True)
        auditor.set_position(0, 1000)
        for i in range(60):
            auditor.check_access("prices", 500, f"test_{i}")
        result = auditor.get_results()
        assert result["warning_count"] == 60
        assert len(result["warnings"]) == 50

    def test_reset_clears_state(self):
        """Reset should clear all warnings and access logs."""
        auditor = LookAheadAuditor(enabled=True)
        auditor.set_position(5, 100)
        auditor.check_access("prices", 50, "test")
        auditor.reset()
        result = auditor.get_results()
        assert result["warning_count"] == 0
        assert result["access_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5E — Price Validation
# ═══════════════════════════════════════════════════════════════════════════


class TestPriceValidation:
    def test_clean_prices_no_warnings(self):
        """Normal prices should produce no warnings."""
        warnings = validate_prices([100.0, 101.0, 102.0, 103.0])
        assert warnings == []

    def test_empty_prices_warning(self):
        """Empty price series should produce a warning."""
        warnings = validate_prices([])
        assert len(warnings) == 1
        assert "empty" in warnings[0].lower()

    def test_nan_detection(self):
        """NaN prices should be detected."""
        warnings = validate_prices([100.0, float("nan"), 102.0])
        assert any("nan" in w.lower() for w in warnings)

    def test_inf_detection(self):
        """Infinite prices should be detected."""
        warnings = validate_prices([100.0, float("inf"), 102.0])
        assert any("infinite" in w.lower() or "inf" in w.lower() for w in warnings)

    def test_negative_prices(self):
        """Negative prices should be detected."""
        warnings = validate_prices([100.0, -5.0, 102.0])
        assert any("negative" in w.lower() for w in warnings)

    def test_zero_prices(self):
        """Zero prices should be detected."""
        warnings = validate_prices([100.0, 0.0, 102.0])
        assert any("zero" in w.lower() for w in warnings)

    def test_suspicious_jumps(self):
        """50%+ single-bar jumps should be flagged."""
        warnings = validate_prices([100.0, 160.0, 165.0])
        assert any("jump" in w.lower() or "suspicious" in w.lower() for w in warnings)

    def test_normal_volatility_no_jump_warning(self):
        """Normal daily moves (< 50%) should not trigger jump warnings."""
        warnings = validate_prices([100.0, 105.0, 110.0, 108.0, 112.0])
        assert not any("jump" in w.lower() or "suspicious" in w.lower() for w in warnings)


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Router-level data quality
# ═══════════════════════════════════════════════════════════════════════════


class TestRouterDataQuality:
    """Test that Phase 5 fields appear in backtest responses."""

    def test_backtest_request_has_data_quality_fields(self):
        """RenkoBacktestRequest should accept Phase 5 parameters."""
        from renko.router import RenkoBacktestRequest

        req = RenkoBacktestRequest(
            prices=[100.0] * 100,
            universe_mode="pit_constituents",
            index_name="sp500",
            price_adjustment="split_adjusted",
            look_ahead_audit=True,
        )
        assert req.universe_mode == "pit_constituents"
        assert req.index_name == "sp500"
        assert req.price_adjustment == "split_adjusted"
        assert req.look_ahead_audit is True

    def test_backtest_request_defaults(self):
        """Phase 5 fields should have backward-compatible defaults."""
        from renko.router import RenkoBacktestRequest

        req = RenkoBacktestRequest(prices=[100.0] * 100)
        assert req.universe_mode == "current_constituents"
        assert req.index_name is None
        assert req.price_adjustment == "raw"
        assert req.look_ahead_audit is False

    def test_backtest_response_has_data_quality_fields(self):
        """RenkoBacktestResponse should include Phase 5 fields."""
        from renko.router import RenkoBacktestResponse

        resp = RenkoBacktestResponse(
            symbol="SPY",
            total_ticks=100,
            total_bricks=50,
            data_hash="abc123",
            data_source={"source": "api"},
            survivorship_bias={"warning": True, "message": "test"},
            data_quality_warnings=["test warning"],
        )
        assert resp.data_hash == "abc123"
        assert resp.survivorship_bias["warning"] is True
        assert resp.data_quality_warnings == ["test warning"]
