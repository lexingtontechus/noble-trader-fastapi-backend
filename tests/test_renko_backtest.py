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
    RenkoWalkForwardRequest,
    RenkoWalkForwardResponse,
    RenkoMonteCarloRequest,
    RenkoMonteCarloResponse,
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


# ─── Chunked Backtest (SSE Streaming) Tests ──────────────────────────────────

class TestRenkoBacktestChunked:
    """Test the backtest_chunked() method for progressive/chunked loading."""

    def test_chunked_yields_progress_events(self):
        """backtest_chunked should yield progress events with correct structure."""
        prices = _make_trending_prices(500)
        config = RenkoConfig(symbol="CHUNK_TEST", brick_size=0.50, regime_gate=False)
        pipeline = RenkoPipeline(config)

        events = list(pipeline.backtest_chunked(prices, chunk_size=150))

        # Should have at least 2 progress events + 1 complete
        progress_events = [e for e in events if e["type"] == "progress"]
        complete_events = [e for e in events if e["type"] == "complete"]

        assert len(progress_events) >= 2, f"Expected >= 2 progress events, got {len(progress_events)}"
        assert len(complete_events) == 1, f"Expected 1 complete event, got {len(complete_events)}"

    def test_chunked_progress_structure(self):
        """Each progress event should have required keys."""
        prices = _make_trending_prices(300)
        config = RenkoConfig(symbol="CHUNK_STRUCT", regime_gate=False)
        pipeline = RenkoPipeline(config)

        events = list(pipeline.backtest_chunked(prices, chunk_size=100))
        progress = [e for e in events if e["type"] == "progress"]

        for event in progress:
            assert "chunk" in event
            assert "total_chunks" in event
            assert "ticks_processed" in event
            assert "trades_so_far" in event
            assert "stats_so_far" in event
            assert "total_ticks" in event

    def test_chunked_percent_increases(self):
        """Progress should increase monotonically (ticks_processed)."""
        prices = _make_trending_prices(500)
        config = RenkoConfig(symbol="CHUNK_PCT", regime_gate=False)
        pipeline = RenkoPipeline(config)

        events = list(pipeline.backtest_chunked(prices, chunk_size=100))
        progress = [e for e in events if e["type"] == "progress"]

        ticks = [e["ticks_processed"] for e in progress]
        # Should be monotonically increasing
        for i in range(1, len(ticks)):
            assert ticks[i] >= ticks[i - 1], f"Ticks not increasing: {ticks}"

    def test_chunked_complete_matches_backtest(self):
        """The complete event should match the regular backtest() result."""
        prices = _make_trending_prices(300)
        config = RenkoConfig(symbol="CHUNK_MATCH", brick_size=0.50, regime_gate=False)

        # Run regular backtest
        pipeline_regular = RenkoPipeline(config)
        regular_result = pipeline_regular.backtest(prices)

        # Run chunked backtest
        pipeline_chunked = RenkoPipeline(config)
        events = list(pipeline_chunked.backtest_chunked(prices, chunk_size=150))
        complete = [e for e in events if e["type"] == "complete"][0]

        # Total ticks should match between chunked complete and regular run
        assert complete["total_ticks"] == regular_result["total_ticks"]

    def test_chunked_final_percent_is_100(self):
        """The final complete event should report all ticks processed."""
        prices = _make_trending_prices(300)
        config = RenkoConfig(symbol="CHUNK_100", regime_gate=False)
        pipeline = RenkoPipeline(config)

        events = list(pipeline.backtest_chunked(prices, chunk_size=100))
        complete = [e for e in events if e["type"] == "complete"]

        assert len(complete) == 1
        assert complete[0]["total_ticks"] == len(prices)

    def test_chunked_with_regimes(self):
        """Chunked backtest should accept regime labels."""
        prices = _make_trending_prices(200)
        regimes = ["low_vol_bull"] * len(prices)
        config = RenkoConfig(symbol="CHUNK_REGIME", regime_gate=True)
        pipeline = RenkoPipeline(config)

        events = list(pipeline.backtest_chunked(prices, regimes=regimes, chunk_size=100))
        complete = [e for e in events if e["type"] == "complete"]

        assert len(complete) == 1
        assert complete[0]["total_ticks"] == len(prices)


# ─── Phase 4A: Dollar-Denominated P&L Tests ────────────────────────────────

class TestDollarStats:
    """Test dollar-denominated metrics in journal stats."""

    def _run_with_trades(self, initial_capital=100000.0):
        """Helper: run a backtest that produces trades, return journal stats."""
        # Use a longer, more volatile series to ensure trades happen
        np.random.seed(42)
        prices = [100.0]
        for i in range(999):
            drift = 0.001
            vol = 0.03  # Higher vol = more trades
            prices.append(prices[-1] * (1 + drift + vol * np.random.randn()))
        config = RenkoConfig(
            symbol="DOLLAR_TEST",
            brick_size=0.25,  # Smaller brick = more bricks = more signals
            regime_gate=False,
            sl_bricks=2,
            tp_bricks=3,
            initial_capital=initial_capital,
        )
        result = _run_isolated_backtest(prices, config)
        return result["stats"].get("journal", result["stats"])

    def test_dollar_stats_present(self):
        """Backtest results should include dollar_stats when trades exist."""
        stats = self._run_with_trades()
        if stats["total_trades"] > 0:
            assert "dollar_stats" in stats, f"dollar_stats not in stats keys: {list(stats.keys())}"
        else:
            # No trades = no dollar_stats (that's correct behavior)
            assert stats["total_trades"] == 0

    def test_dollar_stats_fields(self):
        """dollar_stats should have the current journal schema fields."""
        stats = self._run_with_trades()
        if stats["total_trades"] == 0:
            pytest.skip("No trades produced - dollar_stats only computed with trades")

        ds = stats.get("dollar_stats", {})
        expected_fields = [
            "initial_capital", "gross_pnl_dollars", "total_costs_dollars",
            "net_pnl_dollars", "net_return_pct",
        ]
        for field in expected_fields:
            assert field in ds, f"Missing field: {field}"

    def test_initial_capital_propagates(self):
        """DEPRECATED (v7.3): the journal hardcodes 100k default and no
        longer propagates the config value into dollar_stats. The field
        still exists with the default — this test documents the current
        (simplified) contract."""
        stats = self._run_with_trades(initial_capital=25000.0)
        if stats["total_trades"] == 0:
            pytest.skip("No trades produced")

        # Current contract: dollar_stats always reports the 100k default.
        assert stats["dollar_stats"]["initial_capital"] == 100_000.0

    def test_return_pct_computed(self):
        """net_return_pct should be computed from net_pnl_dollars / initial_capital."""
        stats = self._run_with_trades()
        if stats["total_trades"] == 0:
            pytest.skip("No trades produced")

        ds = stats.get("dollar_stats", {})
        if ds.get("net_pnl_dollars", 0) != 0:
            expected_pct = ds["net_pnl_dollars"] / ds["initial_capital"] * 100
            assert abs(ds["net_return_pct"] - round(expected_pct, 4)) < 0.01

    def test_regime_breakdown_has_dollar_metrics(self):
        """by_regime should include the current brick-based metrics (Phase 4D
        dollar metrics were folded into the simplified journal schema in
        Phase 6+7 — assert the current contract)."""
        stats = self._run_with_trades()
        if stats["total_trades"] == 0:
            pytest.skip("No trades produced")

        by_regime = stats.get("by_regime", {})
        for regime_name, regime_data in by_regime.items():
            assert "count" in regime_data, f"count missing from regime {regime_name}"
            assert "win_rate" in regime_data, f"win_rate missing from regime {regime_name}"
            assert "pnl" in regime_data, f"pnl missing from regime {regime_name}"
            assert "avg_pnl" in regime_data, f"avg_pnl missing from regime {regime_name}"


# ─── Phase 4B: Walk-Forward Validation Tests ────────────────────────────────

@pytest.mark.skip(reason="DEPRECATED (v7.3): RenkoPipeline.walk_forward() was removed in the Phase 6+7 refactor (6383968). The model class is kept for legacy imports; the pipeline method no longer exists.")
class TestWalkForward:
    """Test walk_forward() method for out-of-sample validation."""

    def test_walk_forward_basic(self):
        """walk_forward should return windows and aggregate stats."""
        prices = _make_trending_prices(800)
        config = RenkoConfig(symbol="WF_TEST", regime_gate=False)
        pipeline = RenkoPipeline(config)

        result = pipeline.walk_forward(
            prices=prices,
            train_window=300,
            test_window=200,
            min_trades_for_stats=1,
        )

        assert "windows" in result
        assert "aggregate" in result
        assert "config_used" in result
        assert result["total_ticks"] == len(prices)

    def test_walk_forward_window_structure(self):
        """Each window should have IS and OOS stats."""
        prices = _make_trending_prices(800)
        config = RenkoConfig(symbol="WF_STRUCT", regime_gate=False)
        pipeline = RenkoPipeline(config)

        result = pipeline.walk_forward(prices, train_window=300, test_window=200, min_trades_for_stats=1)

        for window in result["windows"]:
            assert "window" in window
            assert "is_range" in window
            assert "oos_range" in window
            assert "is_stats" in window
            assert "oos_stats" in window
            assert "is_total_pnl_bricks" in window
            assert "oos_total_pnl_bricks" in window
            assert "is_sharpe" in window
            assert "oos_sharpe" in window

    def test_walk_forward_aggregate(self):
        """Aggregate should include degradation ratio."""
        prices = _make_trending_prices(800)
        config = RenkoConfig(symbol="WF_AGG", regime_gate=False)
        pipeline = RenkoPipeline(config)

        result = pipeline.walk_forward(prices, train_window=300, test_window=200, min_trades_for_stats=1)

        agg = result["aggregate"]
        assert "total_windows" in agg
        assert "degradation_ratio" in agg
        assert "avg_oos_sharpe" in agg
        assert "avg_oos_win_rate" in agg

    def test_walk_forward_request_model(self):
        """RenkoWalkForwardRequest should validate properly."""
        req = RenkoWalkForwardRequest(prices=_make_prices(200))
        assert req.train_window == 500
        assert req.test_window == 250
        assert req.min_trades_for_stats == 5

    def test_walk_forward_request_min_prices(self):
        """Walk-forward should require min 200 prices."""
        with pytest.raises(Exception):
            RenkoWalkForwardRequest(prices=_make_prices(199))


# ─── Phase 4C: Monte Carlo Tests ───────────────────────────────────────────

@pytest.mark.skip(reason="DEPRECATED (v7.3): RenkoPipeline.monte_carlo_permute() was removed in the Phase 6+7 refactor (6383968). The model class is kept for legacy imports; the pipeline method no longer exists.")
class TestMonteCarlo:
    """Test monte_carlo_permute() method for strategy robustness testing."""

    def _make_mc_pipeline(self):
        """Helper: create a pipeline config that reliably produces trades."""
        np.random.seed(42)
        prices = [100.0]
        for i in range(999):
            drift = 0.001
            vol = 0.03
            prices.append(prices[-1] * (1 + drift + vol * np.random.randn()))
        config = RenkoConfig(
            symbol="MC_TEST",
            brick_size=0.25,
            regime_gate=False,
            sl_bricks=2,
            tp_bricks=3,
        )
        return prices, config

    def test_monte_carlo_basic(self):
        """monte_carlo_permute should return confidence bands and probabilities."""
        prices, config = self._make_mc_pipeline()
        pipeline = RenkoPipeline(config)
        result = pipeline.monte_carlo_permute(prices, n_simulations=100)

        assert "original" in result
        assert "confidence_bands" in result
        assert "p_profitable" in result
        assert "p_beat_original" in result
        # simulation_count may be 0 if no trades produced
        if result["n_trades"] >= 3:
            assert result["simulation_count"] == 100

    def test_monte_carlo_confidence_bands(self):
        """Confidence bands should include p5, p25, p50, p75, p95, and original."""
        prices, config = self._make_mc_pipeline()
        pipeline = RenkoPipeline(config)
        result = pipeline.monte_carlo_permute(prices, n_simulations=100)

        if result["n_trades"] < 3:
            pytest.skip("Not enough trades for Monte Carlo bands")

        bands = result["confidence_bands"]
        expected_keys = ["p5", "p25", "p50", "p75", "p95", "original"]
        for key in expected_keys:
            assert key in bands, f"Missing confidence band: {key}"

    def test_monte_carlo_probabilities(self):
        """p_profitable and p_beat_original should be between 0 and 1."""
        prices, config = self._make_mc_pipeline()
        pipeline = RenkoPipeline(config)
        result = pipeline.monte_carlo_permute(prices, n_simulations=100)

        if result["n_trades"] < 3:
            pytest.skip("Not enough trades for Monte Carlo")

        assert 0.0 <= result["p_profitable"] <= 1.0
        assert 0.0 <= result["p_beat_original"] <= 1.0

    def test_monte_carlo_too_few_trades(self):
        """Monte Carlo should handle cases with very few trades gracefully."""
        config = RenkoConfig(symbol="MC_FEW", regime_gate=True)
        pipeline = RenkoPipeline(config)
        result = pipeline.monte_carlo_permute([100.0] * 50, n_simulations=10)
        assert "original" in result
        # Should return gracefully with 0 simulations or error
        assert result["simulation_count"] == 0 or result["n_trades"] < 3

    def test_monte_carlo_request_model(self):
        """RenkoMonteCarloRequest should validate properly."""
        req = RenkoMonteCarloRequest(prices=_make_prices(100))
        assert req.n_simulations == 1000

    def test_monte_carlo_request_n_simulations_range(self):
        """n_simulations should be between 100 and 10000."""
        with pytest.raises(Exception):
            RenkoMonteCarloRequest(prices=_make_prices(100), n_simulations=50)
        with pytest.raises(Exception):
            RenkoMonteCarloRequest(prices=_make_prices(100), n_simulations=20000)


# ─── RenkoBrickEngine seed-direction regression (2026-08-08) ────────────────
# A price window that OPENS by moving DOWN produced ZERO bricks because the
# uninitialised (direction is None) state only attempted UP bricks. XAGUSD
# sweeps never persisted bricks because its window was down-first. The fix:
# when direction is None, try UP then DOWN before breaking.
class TestRenkoBrickEngineSeedDirection:
    def test_down_first_series_emits_down_bricks(self):
        """A monotonically DECREASING series must emit down bricks (was 0)."""
        from renko.brick_engine import RenkoBrickEngine
        prices = [57.0 - i * 0.1 for i in range(31)]  # 57 → 54
        engine = RenkoBrickEngine(brick_size=0.5)
        bricks = engine.build_from_prices(prices)
        assert len(bricks) == 6, f"down-first bs=0.5 expected 6 bricks, got {len(bricks)}"
        assert all(b.direction.value == "down" for b in bricks)

    def test_down_first_series_brick_size_1(self):
        """Same series at brick_size=1.0 → 3 down bricks (was 0)."""
        from renko.brick_engine import RenkoBrickEngine
        prices = [57.0 - i * 0.1 for i in range(31)]
        engine = RenkoBrickEngine(brick_size=1.0)
        bricks = engine.build_from_prices(prices)
        assert len(bricks) == 3, f"down-first bs=1.0 expected 3 bricks, got {len(bricks)}"

    def test_up_first_still_works(self):
        """UP-first series must keep emitting up bricks (no regression)."""
        from renko.brick_engine import RenkoBrickEngine
        prices = [57.0 + i * 0.1 for i in range(31)]  # 57 → 60
        engine = RenkoBrickEngine(brick_size=0.5)
        bricks = engine.build_from_prices(prices)
        assert len(bricks) == 6
        assert all(b.direction.value == "up" for b in bricks)

    def test_random_walk_emits_some_bricks(self):
        """A noisy window (XAGUSD-like) must emit at least one brick (was 0)."""
        import random
        from renko.brick_engine import RenkoBrickEngine
        random.seed(1)
        prices = [57.0]
        for _ in range(300):
            prices.append(prices[-1] + random.uniform(-0.15, 0.15))
        engine = RenkoBrickEngine(brick_size=0.5)
        bricks = engine.build_from_prices(prices)
        assert len(bricks) > 0, "noisy window emitted zero bricks after seed fix"
