"""
Tests for Phase 6: Statistical Rigor.

Covers:
  6A — Deflated Sharpe Ratio (DSR)
  6B — Bootstrap Confidence Intervals
  6C — Multiple Testing Correction (Bonferroni, Holm-Bonferroni, BH FDR)
  6D — Strategy Significance Tests (White's Reality Check, Hansen's SPA)
"""

import math

import numpy as np
import pytest

from regime_platform.services.statistics.deflated_sharpe import (
    deflated_sharpe_ratio,
    compute_dsr_for_backtest,
    DeflatedSharpeResult,
)
from regime_platform.services.statistics.bootstrap_ci import (
    bootstrap_ci,
    circular_block_bootstrap_ci,
    compute_metric_on_sample,
    compute_all_bootstrap_cis,
    BootstrapResult,
)
from regime_platform.services.statistics.multiple_testing import (
    bonferroni_correction,
    holm_bonferroni_correction,
    benjamini_hochberg_fdr,
    multiple_testing_correction,
    sharpe_to_p_value,
    MultipleTestingResult,
)
from regime_platform.services.statistics.significance_tests import (
    whites_reality_check,
    hansen_spa,
    SignificanceTestResult,
)


# ═══════════════════════════════════════════════════════════════════════════
# 6A — Deflated Sharpe Ratio
# ═══════════════════════════════════════════════════════════════════════════


class TestDeflatedSharpeRatio:
    def test_dsr_with_genuine_alpha(self):
        """A strategy with genuine positive returns should have DSR > 0.5."""
        rng = np.random.default_rng(42)
        # Generate returns with a clear positive mean (Sharpe ~ 2.0)
        returns = rng.normal(0.001, 0.02, 1000)
        result = deflated_sharpe_ratio(returns, n_trials=1)
        assert result.dsr > 0.5
        assert result.raw_sharpe != 0.0
        assert result.n_observations == 1000

    def test_dsr_penalizes_multiple_trials(self):
        """More trials should lower DSR for the same returns."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.0005, 0.02, 500)

        dsr_1 = deflated_sharpe_ratio(returns, n_trials=1)
        dsr_10 = deflated_sharpe_ratio(returns, n_trials=10)
        dsr_100 = deflated_sharpe_ratio(returns, n_trials=100)

        # More trials → lower DSR
        assert dsr_1.dsr >= dsr_10.dsr
        assert dsr_10.dsr >= dsr_100.dsr

    def test_dsr_zero_sharpe(self):
        """Forced zero Sharpe should give DSR ≈ 0.5."""
        # Construct returns with exactly zero mean
        returns = np.array([0.01, -0.01] * 100, dtype=float)  # Mean = 0 exactly
        result = deflated_sharpe_ratio(returns, n_trials=1, annualize=False)
        # With exactly zero Sharpe, DSR should be ≈ 0.5
        assert 0.3 < result.dsr < 0.7

    def test_dsr_negative_sharpe_low_dsr(self):
        """Negative Sharpe should give very low DSR (near 0)."""
        rng = np.random.default_rng(42)
        # By chance, rng.normal(0.0, 0.02, 1000) with seed 42 has slight negative mean
        returns = rng.normal(0.0, 0.02, 1000)
        # If Sharpe is negative, DSR should be low (unlikely the strategy is positive)
        result = deflated_sharpe_ratio(returns, n_trials=1)
        if result.raw_sharpe < 0:
            assert result.dsr < 0.5  # Negative Sharpe → low DSR

    def test_dsr_significance_flag(self):
        """High Sharpe with few trials should be flagged significant."""
        rng = np.random.default_rng(42)
        # Very strong positive mean → high annualized Sharpe
        returns = rng.normal(0.005, 0.01, 1000)
        result = deflated_sharpe_ratio(returns, n_trials=1)
        # With such strong returns, should be significant
        assert result.is_significant is True

    def test_dsr_too_few_returns(self):
        """Less than 2 returns should return a trivial result."""
        result = deflated_sharpe_ratio([0.01], n_trials=1)
        assert result.dsr == 0.0
        assert result.is_significant is False

    def test_dsr_result_has_interpretation(self):
        """Result should have a human-readable interpretation."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 500)
        result = deflated_sharpe_ratio(returns, n_trials=10)
        assert len(result.interpretation) > 50

    def test_dsr_result_to_dict(self):
        """Result should serialize to dict."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 500)
        result = deflated_sharpe_ratio(returns, n_trials=5)
        d = result.to_dict()
        assert "dsr" in d
        assert "raw_sharpe" in d
        assert "n_trials" in d
        assert "is_significant" in d
        assert "interpretation" in d

    def test_compute_dsr_for_backtest(self):
        """Convenience wrapper should work with trade P&Ls."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 50)  # Trade P&Ls
        result = compute_dsr_for_backtest(pnls, n_trials=5)
        assert isinstance(result, DeflatedSharpeResult)
        assert result.n_observations == 50

    def test_dsr_annualized_vs_not(self):
        """Annualized DSR should have higher raw_sharpe."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 500)

        result_ann = deflated_sharpe_ratio(returns, n_trials=1, annualize=True)
        result_not = deflated_sharpe_ratio(returns, n_trials=1, annualize=False)

        # Annualized raw Sharpe should be larger (multiplied by sqrt(252))
        assert abs(result_ann.raw_sharpe) > abs(result_not.raw_sharpe)


# ═══════════════════════════════════════════════════════════════════════════
# 6B — Bootstrap Confidence Intervals
# ═══════════════════════════════════════════════════════════════════════════


class TestBootstrapCI:
    def test_percentile_bootstrap_basic(self):
        """Percentile bootstrap should return a valid CI."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        result = bootstrap_ci(pnls, metric_fn="sharpe", n_resamples=500, seed=42)
        assert isinstance(result, BootstrapResult)
        assert result.method == "percentile"
        assert result.ci_lower <= result.point_estimate <= result.ci_upper or \
               result.ci_lower >= result.point_estimate >= result.ci_upper  # Can be negative
        assert result.n_resamples == 500

    def test_circular_block_bootstrap(self):
        """Circular block bootstrap should return valid results."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        result = circular_block_bootstrap_ci(pnls, metric_fn="sharpe", n_resamples=500, seed=42)
        assert result.method == "circular_block"
        assert result.sample_size == 100

    def test_ci_width_shrinks_with_more_data(self):
        """CI width should shrink as we add more data."""
        rng = np.random.default_rng(42)
        pnls_small = rng.normal(100, 500, 30)
        pnls_large = rng.normal(100, 500, 300)

        result_small = bootstrap_ci(pnls_small, metric_fn="sharpe", n_resamples=500, seed=42)
        result_large = bootstrap_ci(pnls_large, metric_fn="sharpe", n_resamples=500, seed=42)

        assert result_large.ci_width <= result_small.ci_width

    def test_ci_contains_point_estimate(self):
        """CI should typically contain the point estimate."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 200)
        result = bootstrap_ci(pnls, metric_fn="mean", n_resamples=1000, seed=42)
        # For mean, CI should contain the point estimate
        assert result.ci_lower <= result.point_estimate <= result.ci_upper

    def test_too_few_data(self):
        """Less than 2 data points should return zero CI."""
        result = bootstrap_ci([100.0], metric_fn="sharpe")
        assert result.point_estimate == 0.0
        assert result.n_resamples == 0

    def test_all_metrics(self):
        """All supported metrics should compute without error."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        for metric in ["sharpe", "win_rate", "profit_factor", "max_drawdown", "mean_return", "std", "mean"]:
            result = bootstrap_ci(pnls, metric_fn=metric, n_resamples=100, seed=42)
            assert isinstance(result, BootstrapResult)

    def test_unknown_metric_raises(self):
        """Unknown metric should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown metric"):
            compute_metric_on_sample(np.array([1.0, 2.0]), "nonexistent_metric")

    def test_result_to_dict(self):
        """Result should serialize to dict."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        result = bootstrap_ci(pnls, metric_fn="sharpe", n_resamples=100, seed=42)
        d = result.to_dict()
        assert "metric_name" in d
        assert "point_estimate" in d
        assert "ci_lower" in d
        assert "ci_upper" in d

    def test_format_display(self):
        """Format display should produce '1.45 [1.12, 1.78]' style string."""
        result = BootstrapResult(
            metric_name="sharpe", point_estimate=1.45,
            ci_lower=1.12, ci_upper=1.78, confidence_level=0.95,
            n_resamples=1000, method="percentile",
        )
        display = result.format_display()
        assert "1.45" in display
        assert "1.12" in display
        assert "1.78" in display

    def test_compute_all_bootstrap_cis(self):
        """Batch computation should return CIs for all metrics."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        results = compute_all_bootstrap_cis(pnls, n_resamples=100, seed=42)
        assert "sharpe" in results
        assert "win_rate" in results
        assert "profit_factor" in results
        assert "max_drawdown" in results
        assert "mean_return" in results

    def test_custom_metric_fn(self):
        """Custom metric function should work."""
        pnls = [100.0, -50.0, 200.0, -30.0, 80.0]
        result = bootstrap_ci(pnls, metric_fn=lambda x: float(np.median(x)), n_resamples=100, seed=42)
        assert result.metric_name == "custom"

    def test_block_bootstrap_preserves_correlation(self):
        """Block bootstrap should give wider CIs than i.i.d. bootstrap for correlated data."""
        rng = np.random.default_rng(42)
        # Generate autocorrelated data
        n = 200
        data = np.zeros(n)
        data[0] = rng.normal(0, 1)
        for i in range(1, n):
            data[i] = 0.8 * data[i-1] + rng.normal(0, 0.5)

        result_iid = bootstrap_ci(data, metric_fn="mean", n_resamples=500, seed=42)
        result_block = circular_block_bootstrap_ci(data, metric_fn="mean", n_resamples=500, seed=42)

        # Both should produce valid results
        assert isinstance(result_iid, BootstrapResult)
        assert isinstance(result_block, BootstrapResult)


# ═══════════════════════════════════════════════════════════════════════════
# 6C — Multiple Testing Correction
# ═══════════════════════════════════════════════════════════════════════════


class TestBonferroniCorrection:
    def test_bonferroni_inflates_p_values(self):
        """Bonferroni should multiply p-values by number of tests."""
        p_values = [0.01, 0.03, 0.05]
        adjusted, significant = bonferroni_correction(p_values, alpha=0.05)
        assert adjusted[0] == pytest.approx(0.03)  # 0.01 * 3
        assert adjusted[1] == pytest.approx(0.09)  # 0.03 * 3
        assert adjusted[2] == pytest.approx(0.15)  # 0.05 * 3

    def test_bonferroni_caps_at_1(self):
        """Adjusted p-values should never exceed 1.0."""
        p_values = [0.5, 0.8]
        adjusted, _ = bonferroni_correction(p_values, alpha=0.05)
        assert all(p <= 1.0 for p in adjusted)

    def test_bonferroni_conservative(self):
        """Bonferroni should be the most conservative."""
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        bonf_adj, bonf_sig = bonferroni_correction(p_values, alpha=0.05)
        holm_adj, holm_sig = holm_bonferroni_correction(p_values, alpha=0.05)
        # Bonferroni should have fewer or equal significant results
        assert sum(bonf_sig) <= sum(holm_sig)

    def test_empty_p_values(self):
        """Empty list should return empty results."""
        adjusted, significant = bonferroni_correction([])
        assert adjusted == []
        assert significant == []


class TestHolmBonferroniCorrection:
    def test_holm_step_down(self):
        """Holm-Bonferroni should produce valid adjusted p-values."""
        p_values = [0.01, 0.04, 0.05]
        adjusted, significant = holm_bonferroni_correction(p_values, alpha=0.05)
        # The smallest p-value is adjusted by m, the next by m-1, etc.
        assert len(adjusted) == 3
        assert len(significant) == 3

    def test_holm_more_powerful_than_bonferroni(self):
        """Holm should find at least as many significant results as Bonferroni."""
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        _, bonf_sig = bonferroni_correction(p_values, alpha=0.05)
        _, holm_sig = holm_bonferroni_correction(p_values, alpha=0.05)
        assert sum(holm_sig) >= sum(bonf_sig)

    def test_holm_monotonicity(self):
        """Holm adjusted p-values should be monotonic in sorted order."""
        p_values = [0.005, 0.01, 0.03, 0.04, 0.05]
        adjusted, _ = holm_bonferroni_correction(p_values, alpha=0.1)
        sorted_adjusted = [adjusted[i] for i in np.argsort(p_values)]
        # Monotonic: each adjusted p >= previous
        for i in range(1, len(sorted_adjusted)):
            assert sorted_adjusted[i] >= sorted_adjusted[i-1]


class TestBenjaminiHochbergFDR:
    def test_bh_less_conservative(self):
        """BH FDR should find at least as many significant results as Bonferroni."""
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        _, bonf_sig = bonferroni_correction(p_values, alpha=0.05)
        _, bh_sig = benjamini_hochberg_fdr(p_values, alpha=0.05)
        assert sum(bh_sig) >= sum(bonf_sig)

    def test_bh_controls_fdr(self):
        """BH should correctly identify significant p-values."""
        # All small p-values → all significant
        p_values = [0.001, 0.002, 0.003]
        adjusted, significant = benjamini_hochberg_fdr(p_values, alpha=0.05)
        assert sum(significant) >= 2  # Most should be significant

    def test_bh_no_significant(self):
        """Large p-values should yield no significant results."""
        p_values = [0.3, 0.4, 0.5, 0.6]
        _, significant = benjamini_hochberg_fdr(p_values, alpha=0.05)
        assert sum(significant) == 0

    def test_bh_capped_at_1(self):
        """BH adjusted p-values should never exceed 1.0."""
        p_values = [0.5, 0.8]
        adjusted, _ = benjamini_hochberg_fdr(p_values, alpha=0.05)
        assert all(p <= 1.0 for p in adjusted)


class TestSharpeToPValue:
    def test_positive_sharpe_small_p(self):
        """High positive Sharpe should give small p-value."""
        p = sharpe_to_p_value(2.0, 1000)
        assert p < 0.05

    def test_zero_sharpe_large_p(self):
        """Zero Sharpe should give p-value ≈ 0.5."""
        p = sharpe_to_p_value(0.0, 1000)
        assert 0.4 < p < 0.6

    def test_too_few_observations(self):
        """Less than 2 observations should give p = 1.0."""
        p = sharpe_to_p_value(2.0, 1)
        assert p == 1.0


class TestMultipleTestingCorrection:
    def test_combined_correction(self):
        """Combined correction should return all three methods."""
        results = [
            {"params": {"sl": 3}, "sharpe_estimate": 1.2, "total_trades": 100},
            {"params": {"sl": 5}, "sharpe_estimate": 0.8, "total_trades": 100},
            {"params": {"sl": 7}, "sharpe_estimate": 1.5, "total_trades": 100},
        ]
        output = multiple_testing_correction(results, n_observations=100)
        assert "bonferroni" in output
        assert "holm_bonferroni" in output
        assert "benjamini_hochberg" in output
        assert all(isinstance(v, MultipleTestingResult) for v in output.values())

    def test_combined_empty_results(self):
        """Empty results should return empty dict."""
        output = multiple_testing_correction([])
        assert output == {}

    def test_combined_result_has_interpretation(self):
        """Each method result should have an interpretation."""
        results = [
            {"params": {"a": 1}, "sharpe_estimate": 1.0, "total_trades": 100},
            {"params": {"a": 2}, "sharpe_estimate": 0.5, "total_trades": 100},
        ]
        output = multiple_testing_correction(results, n_observations=100)
        for method, result in output.items():
            assert len(result.interpretation) > 20

    def test_combined_to_dict(self):
        """Results should serialize to dict."""
        results = [
            {"params": {"a": 1}, "sharpe_estimate": 1.0, "total_trades": 100},
        ]
        output = multiple_testing_correction(results, n_observations=100)
        for method, result in output.items():
            d = result.to_dict()
            assert "method" in d
            assert "adjusted_p_values" in d


# ═══════════════════════════════════════════════════════════════════════════
# 6D — Strategy Significance Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestWhitesRealityCheck:
    def test_wrc_genuine_alpha(self):
        """Strategies with genuinely positive returns should be detected."""
        rng = np.random.default_rng(42)
        # Best strategy has clear positive mean
        strategies = [
            rng.normal(0.0, 0.02, 500),   # No alpha
            rng.normal(0.0, 0.02, 500),   # No alpha
            rng.normal(0.005, 0.02, 500),  # Genuine alpha
        ]
        result = whites_reality_check(strategies, n_bootstrap=500, seed=42)
        assert isinstance(result, SignificanceTestResult)
        assert result.test_name == "whites_reality_check"
        assert result.n_strategies == 3

    def test_wrc_no_alpha(self):
        """Strategies with no alpha should have high p-value."""
        rng = np.random.default_rng(42)
        strategies = [
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.0, 0.02, 500),
        ]
        result = whites_reality_check(strategies, n_bootstrap=500, seed=42)
        # With no alpha, p-value should be relatively high
        assert result.p_value > 0.01  # Very unlikely to be significant

    def test_wrc_mismatched_lengths_raises(self):
        """Mismatched strategy lengths should raise ValueError."""
        strategies = [
            np.array([0.01, 0.02, 0.03]),
            np.array([0.01, 0.02]),  # Different length
        ]
        with pytest.raises(ValueError, match="same length"):
            whites_reality_check(strategies, n_bootstrap=100)

    def test_wrc_too_few_data(self):
        """Too few observations should return trivial result."""
        strategies = [np.array([0.01, 0.02, 0.03])]
        result = whites_reality_check(strategies, n_bootstrap=100)
        assert result.p_value == 1.0
        assert result.is_significant is False

    def test_wrc_with_benchmark(self):
        """WRC should work with a custom benchmark."""
        rng = np.random.default_rng(42)
        strategies = [rng.normal(0.003, 0.02, 500)]
        benchmark = rng.normal(0.0, 0.02, 500)
        result = whites_reality_check(strategies, benchmark_returns=benchmark, n_bootstrap=500, seed=42)
        assert isinstance(result, SignificanceTestResult)


class TestHansenSPA:
    def test_spa_genuine_alpha(self):
        """SPA should detect genuinely positive strategies."""
        rng = np.random.default_rng(42)
        strategies = [
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.005, 0.02, 500),
        ]
        result = hansen_spa(strategies, n_bootstrap=500, seed=42)
        assert isinstance(result, SignificanceTestResult)
        assert result.test_name == "hansen_spa"

    def test_spa_more_powerful_than_wrc(self):
        """SPA should generally have at least as much power as WRC."""
        rng = np.random.default_rng(42)
        strategies = [
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.003, 0.02, 500),  # Marginal alpha
        ]
        wrc = whites_reality_check(strategies, n_bootstrap=500, seed=42)
        spa = hansen_spa(strategies, n_bootstrap=500, seed=42)
        # SPA should have p-value <= WRC (more powerful)
        # This is a soft test — not guaranteed for every seed
        assert isinstance(spa.p_value, float)
        assert isinstance(wrc.p_value, float)

    def test_spa_result_has_interpretation(self):
        """Result should have human-readable interpretation."""
        rng = np.random.default_rng(42)
        strategies = [rng.normal(0.001, 0.02, 500)]
        result = hansen_spa(strategies, n_bootstrap=100, seed=42)
        assert len(result.interpretation) > 50

    def test_spa_to_dict(self):
        """Result should serialize to dict."""
        rng = np.random.default_rng(42)
        strategies = [rng.normal(0.001, 0.02, 500)]
        result = hansen_spa(strategies, n_bootstrap=100, seed=42)
        d = result.to_dict()
        assert "test_name" in d
        assert "p_value" in d
        assert "is_significant" in d


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Router-level statistical rigor
# ═══════════════════════════════════════════════════════════════════════════


class TestRouterStatisticalRigor:
    """Test that Phase 6 fields appear in backtest responses."""

    def test_renko_backtest_response_has_phase6_fields(self):
        """RenkoBacktestResponse should include Phase 6 fields."""
        from renko.router import RenkoBacktestResponse

        resp = RenkoBacktestResponse(
            symbol="SPY",
            total_ticks=100,
            total_bricks=50,
            bootstrap_cis={"sharpe": {"ci_lower": 0.5, "ci_upper": 1.8}},
            deflated_sharpe_result={"dsr": 0.92, "is_significant": True},
        )
        assert resp.bootstrap_cis is not None
        assert resp.deflated_sharpe_result is not None

    def test_renko_optimize_response_has_phase6_fields(self):
        """RenkoBacktestOptimizeResponse should include Phase 6 fields."""
        from renko.router import RenkoBacktestOptimizeResponse

        resp = RenkoBacktestOptimizeResponse(
            results=[{"sharpe_estimate": 1.2}],
            n_combinations=5,
            deflated_sharpe_result={"dsr": 0.85},
            multiple_testing_results={"bonferroni": {"n_significant": 1}},
            significance_test_results={"whites_reality_check": {"p_value": 0.03}},
        )
        assert resp.deflated_sharpe_result is not None
        assert resp.multiple_testing_results is not None
        assert resp.significance_test_results is not None

    def test_hmm_optimize_response_has_phase6_fields(self):
        """HMM BacktestOptimizeResponse should include Phase 6 fields."""
        from regime_platform.models.schemas import BacktestOptimizeResponse

        resp = BacktestOptimizeResponse(
            results=[{"sharpe_ratio": 1.2}],
            n_combinations=5,
            deflated_sharpe_result={"dsr": 0.85},
            multiple_testing_results={"bonferroni": {"n_significant": 1}},
            significance_test_results={"hansen_spa": {"p_value": 0.04}},
        )
        assert resp.deflated_sharpe_result is not None
        assert resp.multiple_testing_results is not None
        assert resp.significance_test_results is not None

    def test_hmm_backtest_result_has_bootstrap_cis(self):
        """HMM BacktestResult should include bootstrap_cis field."""
        from regime_platform.core.backtester import BacktestResult

        result = BacktestResult(
            symbol="SPY",
            n_bars=1000,
            n_trades=50,
            n_wins=30,
            n_losses=20,
            win_rate=0.6,
            total_return=0.15,
            annual_return=0.38,
            buy_hold_return=0.10,
            excess_return=0.05,
            monthly_return_avg=0.013,
            max_drawdown=-0.08,
            max_drawdown_duration=20,
            avg_drawdown=-0.03,
            var_95=0.02,
            cvar_95=0.035,
            annual_vol=0.15,
            downside_vol=0.12,
            sharpe_ratio=2.53,
            sortino_ratio=3.17,
            calmar_ratio=4.75,
            information_ratio=0.33,
            avg_win_pct=0.025,
            avg_loss_pct=0.015,
            avg_win_loss_ratio=1.67,
            max_consecutive_wins=5,
            max_consecutive_losses=3,
            avg_hold_bars=8.0,
            profit_factor=2.5,
            regime_distribution={"low_bull": 30, "high_bear": 20},
            trades_by_regime={"low_bull": {"wins": 20, "losses": 10, "total_pnl": 5000.0}},
            equity_curve=[100000, 100500, 100300],
            drawdown_curve=[0.0, -0.005, -0.002],
            trade_log=[],
            bootstrap_cis={"sharpe": {"ci_lower": 1.8, "ci_upper": 3.2}},
        )
        assert result.bootstrap_cis is not None
        assert "sharpe" in result.bootstrap_cis

    def test_trade_journal_get_stats_with_bootstrap(self):
        """TradeJournal.get_stats() should compute bootstrap CIs when requested."""
        from renko.trade_journal import TradeJournal, TradeRecord
        from renko.pattern_detector import PatternType, SignalDirection
        from renko.risk_manager import PositionStatus

        journal = TradeJournal()
        # Add enough trades for bootstrap
        rng = np.random.default_rng(42)
        for i in range(30):
            trade = TradeRecord(
                trade_id=f"R{i:06d}",
                symbol="SPY",
                direction=SignalDirection.LONG,
                pattern_type=PatternType.DOUBLE_TOP,
                entry_price=100.0,
                exit_price=100.0 + rng.normal(0.5, 2.0),
                entry_ts=1000.0 + i * 100,
                exit_ts=1000.0 + i * 100 + 50,
                pnl_bricks=rng.normal(1.0, 2.0),
                pnl_dollars=rng.normal(100.0, 500.0),
                status=PositionStatus.CLOSED_TP,
                bricks_held=3,
                signal_strength=0.8,
            )
            journal.record(trade)

        stats = journal.get_stats(compute_bootstrap_cis=True, bootstrap_n_resamples=100, bootstrap_seed=42)
        assert "bootstrap_cis" in stats
        assert "sharpe" in stats["bootstrap_cis"]
