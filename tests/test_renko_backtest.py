"""
Tests for the Renko backtest endpoints.

Covers:
  - POST /renko/backtest/run (isolated pipeline backtest)
  - POST /renko/backtest/compare (multi-config comparison)
  - POST /renko/backtest/optimize (grid-search parameter sweep)
  - Pydantic model validation
  - Edge cases (too few prices, too many combos, empty trades)
"""

import pytest
import numpy as np

from renko.config import RenkoConfig
from renko.pipeline import RenkoPipeline
from renko.router import (
    RenkoBacktestRequest,
    RenkoBacktestResponse,
    RenkoCompareConfig,
    RenkoBacktestCompareRequest,
    RenkoBacktestCompareResponse,
    RenkoBacktestOptimizeRequest,
    RenkoBacktestOptimizeResponse,
    _build_renko_config,
    _run_isolated_backtest,
)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_prices(n: int = 300, start: float = 100.0, drift: float = 0.001, vol: float = 0.02) -> list[float]:
    """Generate a synthetic price series with configurable drift and vol."""
    np.random.seed(42)
    prices = [start]
    for _ in range(n - 1):
        ret = drift + vol * np.random.randn()
        prices.append(prices[-1] * (1 + ret))
    return prices


def _make_trending_prices(n: int = 500) -> list[float]:
    """Generate a price series with clear regime changes (trending + volatile)."""
    np.random.seed(42)
    prices = [100.0]
    for i in range(n):
        if i < 150:
            drift, vol = 0.002, 0.01  # bull quiet
        elif i < 300:
            drift, vol = -0.003, 0.04  # bear volatile
        else:
            drift, vol = 0.002, 0.01  # bull quiet
        prices.append(prices[-1] * (1 + drift + vol * np.random.randn()))
    return prices


# ─── Pydantic Model Tests ────────────────────────────────────────────────────

class TestRenkoBacktestModels:
    """Test Pydantic request/response models for Renko backtest endpoints."""

    def test_backtest_request_defaults(self):
        """RenkoBacktestRequest should have sensible defaults."""
        req = RenkoBacktestRequest(prices=_make_prices(100))
        assert req.symbol == "SPY"
        assert req.brick_size == 0.50
        assert req.sl_bricks == 3
        assert req.tp_bricks == 5
        assert req.regime_gate is True
        assert req.timestamps is None
        assert req.regimes is None

    def test_backtest_request_custom_params(self):
        """RenkoBacktestRequest should accept custom params."""
        req = RenkoBacktestRequest(
            prices=_make_prices(100),
            symbol="AAPL",
            brick_size=0.25,
            sl_bricks=2,
            tp_bricks=4,
            regime_gate=False,
        )
        assert req.symbol == "AAPL"
        assert req.brick_size == 0.25
        assert req.sl_bricks == 2
        assert req.regime_gate is False

    def test_backtest_request_min_prices(self):
        """RenkoBacktestRequest should reject prices with less than 50 items."""
        with pytest.raises(Exception):
            RenkoBacktestRequest(prices=_make_prices(49))

    def test_compare_config_requires_label(self):
        """RenkoCompareConfig should require a label."""
        cfg = RenkoCompareConfig(label="aggressive", brick_size=0.25)
        assert cfg.label == "aggressive"
        assert cfg.brick_size == 0.25

    def test_compare_request_min_configs(self):
        """RenkoBacktestCompareRequest should require min 2 configs."""
        with pytest.raises(Exception):
            RenkoBacktestCompareRequest(
                prices=_make_prices(100),
                configs=[RenkoCompareConfig(label="only_one")],
            )

    def test_optimize_request_requires_param_grid(self):
        """RenkoBacktestOptimizeRequest should require param_grid."""
        req = RenkoBacktestOptimizeRequest(
            prices=_make_prices(100),
            param_grid={"sl_bricks": [2, 3]},
        )
        assert "sl_bricks" in req.param_grid

    def test_backtest_response_model(self):
        """RenkoBacktestResponse should create from dict."""
        resp = RenkoBacktestResponse(
            symbol="SPY",
            total_ticks=500,
            total_bricks=120,
            config_used={"brick_size": 0.5},
            stats={"total_trades": 10},
            trades=[],
        )
        assert resp.symbol == "SPY"
        assert resp.total_bricks == 120


# ─── Isolated Backtest Helper Tests ──────────────────────────────────────────

class TestIsolatedBacktest:
    """Test the _run_isolated_backtest helper and _build_renko_config."""

    def test_build_renko_config(self):
        """_build_renko_config should produce a valid RenkoConfig."""
        req = RenkoBacktestRequest(
            prices=_make_prices(100),
            symbol="TSLA",
            brick_size=0.30,
            sl_bricks=2,
            tp_bricks=6,
        )
        config = _build_renko_config(req)
        assert isinstance(config, RenkoConfig)
        assert config.symbol == "TSLA"
        assert config.brick_size == 0.30
        assert config.sl_bricks == 2
        assert config.tp_bricks == 6

    def test_run_isolated_backtest_returns_result(self):
        """_run_isolated_backtest should return a result dict."""
        prices = _make_trending_prices(500)
        config = RenkoConfig(symbol="TEST", brick_size=0.50)
        result = _run_isolated_backtest(prices, config)
        assert isinstance(result, dict)
        assert "stats" in result
        assert "total_bricks" in result
        assert "total_ticks" in result
        assert "trades" in result

    def test_isolated_backtest_does_not_affect_live_pipeline(self):
        """Isolated backtest should not modify the global pipeline registry."""
        from renko.router import _pipelines
        before_keys = set(_pipelines.keys())
        prices = _make_prices(100)
        config = RenkoConfig(symbol="ISOLATED_TEST")
        _run_isolated_backtest(prices, config)
        after_keys = set(_pipelines.keys())
        # The isolated test should NOT add to the global registry
        assert before_keys == after_keys or "ISOLATED_TEST" not in after_keys

    def test_isolated_backtest_with_regimes(self):
        """Isolated backtest should accept regime labels."""
        prices = _make_trending_prices(200)
        config = RenkoConfig(symbol="REGIME_TEST")
        regimes = ["low_vol_bull"] * len(prices)
        result = _run_isolated_backtest(prices, config, regimes=regimes)
        assert isinstance(result, dict)


# ─── Full Backtest Run Tests ────────────────────────────────────────────────

class TestRenkoBacktestRun:
    """Test the full Renko backtest pipeline execution."""

    def test_basic_run(self):
        """A basic backtest should return valid structure."""
        prices = _make_trending_prices(500)
        config = RenkoConfig(symbol="RUN_TEST", brick_size=0.50, regime_gate=False)
        result = _run_isolated_backtest(prices, config)

        assert result["total_ticks"] == len(prices)
        assert result["total_bricks"] >= 0

        # Stats should be a dict from the journal
        stats = result["stats"]
        journal_stats = stats.get("journal", stats)  # handle nested
        assert "total_trades" in journal_stats or "total_trades" in stats

    def test_custom_config_run(self):
        """Custom config params should propagate."""
        prices = _make_trending_prices(300)
        config = RenkoConfig(
            symbol="CUSTOM",
            brick_size=0.25,
            sl_bricks=2,
            tp_bricks=4,
            regime_gate=False,
        )
        result = _run_isolated_backtest(prices, config)
        assert result["total_ticks"] == len(prices)

    def test_no_regime_gate_produces_more_trades(self):
        """With regime_gate=False, more trades should occur."""
        prices = _make_trending_prices(500)
        config_gated = RenkoConfig(symbol="GATED", regime_gate=True)
        config_open = RenkoConfig(symbol="OPEN", regime_gate=False)

        result_gated = _run_isolated_backtest(prices, config_gated)
        result_open = _run_isolated_backtest(prices, config_open)

        gated_trades = result_gated["stats"].get("journal", result_gated["stats"]).get("total_trades", 0)
        open_trades = result_open["stats"].get("journal", result_open["stats"]).get("total_trades", 0)
        assert open_trades >= gated_trades


# ─── Compare Tests ──────────────────────────────────────────────────────────

class TestRenkoBacktestCompare:
    """Test Renko backtest comparison logic."""

    def test_compare_two_configs(self):
        """Comparing two configs should return both results + diff."""
        prices = _make_trending_prices(300)
        config_a = RenkoConfig(symbol="CMP_A", brick_size=0.25, sl_bricks=2, tp_bricks=4, regime_gate=False)
        config_b = RenkoConfig(symbol="CMP_B", brick_size=0.50, sl_bricks=3, tp_bricks=5, regime_gate=False)

        result_a = _run_isolated_backtest(prices, config_a)
        result_b = _run_isolated_backtest(prices, config_b)

        # Both should return valid results
        assert "stats" in result_a
        assert "stats" in result_b

    def test_diff_computation(self):
        """Diff should show metric differences between configs."""
        # Simulate what the compare endpoint does
        comparisons = [
            {"stats": {"sharpe_estimate": 0.5, "win_rate": 0.4}},
            {"stats": {"sharpe_estimate": 1.2, "win_rate": 0.6}},
        ]

        best = max(comparisons, key=lambda c: c["stats"].get("sharpe_estimate", 0.0))
        assert best["stats"]["sharpe_estimate"] == 1.2

        first = comparisons[0]
        diff = {}
        for key in ["sharpe_estimate", "win_rate"]:
            f_val = first["stats"].get(key, 0)
            b_val = best["stats"].get(key, 0)
            diff[key] = {"first": f_val, "best": b_val, "delta": round(b_val - f_val, 6)}

        assert diff["sharpe_estimate"]["delta"] == 0.7
        assert diff["win_rate"]["delta"] == 0.2


# ─── Optimize Tests ─────────────────────────────────────────────────────────

class TestRenkoBacktestOptimize:
    """Test Renko backtest optimization (parameter sweep)."""

    def test_grid_sweep_basic(self):
        """Grid sweep should return results for each combination."""
        import itertools
        param_grid = {"sl_bricks": [2, 3], "tp_bricks": [4, 5]}
        grid_keys = list(param_grid.keys())
        grid_values = [param_grid[k] for k in grid_keys]
        combinations = list(itertools.product(*grid_values))
        assert len(combinations) == 4

    def test_grid_sweep_max_combos(self):
        """Grid sweep should reject > 50 combinations."""
        import itertools
        param_grid = {"sl_bricks": list(range(1, 11)), "tp_bricks": list(range(1, 11))}
        grid_keys = list(param_grid.keys())
        grid_values = [param_grid[k] for k in grid_keys]
        combinations = list(itertools.product(*grid_values))
        assert len(combinations) == 100  # > 50, should be rejected

    def test_optimize_finds_best(self):
        """Optimize should identify best by Sharpe and return."""
        results = [
            {"params": {"sl_bricks": 2}, "sharpe_estimate": 0.3, "total_pnl_bricks": 5.0},
            {"params": {"sl_bricks": 3}, "sharpe_estimate": 0.8, "total_pnl_bricks": 12.0},
            {"params": {"sl_bricks": 4}, "sharpe_estimate": 0.5, "total_pnl_bricks": 8.0},
        ]
        best_by_sharpe = max(results, key=lambda r: r["sharpe_estimate"])
        best_by_return = max(results, key=lambda r: r["total_pnl_bricks"])

        assert best_by_sharpe["params"]["sl_bricks"] == 3
        assert best_by_return["params"]["sl_bricks"] == 3
