"""
Tests for the Phase 3 portfolio comparison
(regime_platform/sweep_orchestrator/portfolio_compare.py).

Covers the pure functions with synthetic resolved-signal data (no network):
  - build_returns_matrix: (days × assets) log-return proxy from pnl/notional
  - equal_weight_equity: daily cumulative PnL with equal notional shares
  - paper_equity: daily cumulative PnL from realized_pnl
  - compare: three-way summary + the min-days data gate
"""

import numpy as np
import pytest

from regime_platform.sweep_orchestrator.portfolio_compare import (
    build_returns_matrix,
    equal_weight_equity,
    paper_equity,
    compare,
    MIN_DAYS,
)


def _sig(symbol, day, pnl, notional=100.0, outcome="tp_hit"):
    return {
        "symbol": symbol,
        "ts": f"2026-08-{day:02d}T00:00:00+00:00",
        "resolved_at": f"2026-08-{day:02d}T00:30:00+00:00",
        "outcome": outcome,
        "pnl_dollars": pnl,
        "size_notional": notional,
    }


class TestBuildReturnsMatrix:
    def test_shape_and_values(self):
        resolved = [
            _sig("AUDUSD", 1, 1.0, notional=100.0),
            _sig("EURUSD", 1, -0.5, notional=100.0),
            _sig("AUDUSD", 2, 2.0, notional=100.0),
        ]
        matrix, used = build_returns_matrix(resolved, ["AUDUSD", "EURUSD", "BTCUSD"])
        assert used == ["AUDUSD", "EURUSD"]
        assert matrix.shape == (2, 2)
        # day1 AUDUSD: 1.0/100 = 0.01 ; day2 AUDUSD: 2.0/100 = 0.02
        assert matrix[0, 0] == pytest.approx(0.01)
        assert matrix[1, 0] == pytest.approx(0.02)
        # day1 EURUSD: -0.5/100 = -0.005
        assert matrix[0, 1] == pytest.approx(-0.005)

    def test_drops_symbols_with_no_data(self):
        resolved = [_sig("AUDUSD", 1, 1.0)]
        matrix, used = build_returns_matrix(resolved, ["AUDUSD", "EURUSD"])
        assert used == ["AUDUSD"]
        assert matrix.shape == (1, 1)

    def test_empty_when_no_matches(self):
        matrix, used = build_returns_matrix([], ["AUDUSD", "EURUSD"])
        assert matrix.shape == (0, 0)
        assert used == []


class TestEqualWeightEquity:
    def test_daily_accumulation(self):
        resolved = [
            _sig("AUDUSD", 1, 1.0),
            _sig("EURUSD", 1, -0.5),
            _sig("AUDUSD", 2, 2.0),
        ]
        eq = equal_weight_equity(resolved, ["AUDUSD", "EURUSD"])
        assert len(eq) == 2
        assert eq[0]["day"] == "2026-08-01"
        assert eq[0]["daily_pnl"] == pytest.approx(0.5)   # 1.0 + (-0.5)
        assert eq[0]["cumulative_pnl"] == pytest.approx(0.5)
        assert eq[1]["daily_pnl"] == pytest.approx(2.0)
        assert eq[1]["cumulative_pnl"] == pytest.approx(2.5)
        # equal share of 1000 base over 2 symbols
        assert eq[0]["notional_share"] == pytest.approx(500.0)

    def test_ignores_symbols_not_in_list(self):
        resolved = [_sig("AUDUSD", 1, 1.0), _sig("EURUSD", 1, 1.0)]
        eq = equal_weight_equity(resolved, ["AUDUSD"])
        assert len(eq) == 1
        assert eq[0]["daily_pnl"] == pytest.approx(1.0)


class TestPaperEquity:
    def test_uses_realized_pnl_and_close_ts(self):
        rows = [
            {"symbol": "AUDUSD", "close_ts": "2026-08-01T01:00:00+00:00", "realized_pnl": 1.5},
            {"symbol": "EURUSD", "close_ts": "2026-08-01T02:00:00+00:00", "realized_pnl": -0.5},
        ]
        eq = paper_equity(rows)
        assert len(eq) == 1
        assert eq[0]["daily_pnl"] == pytest.approx(1.0)
        assert eq[0]["cumulative_pnl"] == pytest.approx(1.0)


class TestCompare:
    def test_insufficient_data_gate(self):
        resolved = [_sig("AUDUSD", 1, 1.0)]
        result = compare(resolved, [], ["AUDUSD"], min_days=MIN_DAYS)
        assert result["insufficient_data"] is True
        assert result["days_available"] == 1
        assert result["min_days_required"] == MIN_DAYS

    def test_sufficient_data_runs_optimizer(self):
        # 10 days × 2 symbols of resolved signals
        resolved = []
        for d in range(1, 11):
            resolved.append(_sig("AUDUSD", d, 1.0, notional=100.0))
            resolved.append(_sig("EURUSD", d, 0.5, notional=100.0))
        paper = [
            {"symbol": "AUDUSD", "close_ts": f"2026-08-{d:02d}T01:00:00+00:00",
             "realized_pnl": 1.0} for d in range(1, 11)
        ]
        result = compare(resolved, paper, ["AUDUSD", "EURUSD"], min_days=7)
        assert result["insufficient_data"] is False
        assert result["days_available"] == 10
        assert set(result["symbols_used"]) == {"AUDUSD", "EURUSD"}
        # optimizer ran (may fall back to equal weight on tiny matrix, but
        # the weights dict must exist with both symbols)
        assert set(result["optimizer"]["weights"].keys()) == {"AUDUSD", "EURUSD"}
        assert result["equal_weight"]["days"] == 10
        assert result["paper"]["days"] == 10
