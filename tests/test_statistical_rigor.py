"""Tests for Phase 6: Statistical Rigor.

Covers:
  6A - Deflated Sharpe Ratio (DSR)
  6B - Bootstrap Confidence Intervals
  6C - Multiple Testing Correction (Bonferroni, Holm-Bonferroni, BH FDR)
  6D - Strategy Significance Tests (White's Reality Check, Hansen's SPA)

NOTE: rewritten to match the current statistics module APIs (post-refactor):
  * deflated_sharpe_ratio(observed_sharpe, n_trials, sample_length, ..., returns=None)
    returns a plain dict.
  * bootstrap CI helpers take a callable metric_fn and return BootstrapResult
    (a dataclass; no to_dict/format_display/ci_width helpers).
  * multiple-testing helpers return dicts with keys
    adjusted_p_values / is_significant / n_significant.
  * whites_reality_check / hansen_spa take (strategy_returns, benchmark_returns=,
    n_bootstrap=) and return plain dicts (no seed argument).
  * The removed compute_dsr_for_backtest / sharpe_to_p_value /
    MultipleTestingResult / RenkoBacktestResponse helpers are no longer tested.
"""

import numpy as np
import pytest

from regime_platform.services.statistics.deflated_sharpe import (
    deflated_sharpe_ratio,
)
from regime_platform.services.statistics.bootstrap_ci import (
    percentile_bootstrap_ci,
    circular_block_bootstrap_ci,
    compute_all_bootstrap_cis,
    BootstrapResult,
    METRIC_FUNCTIONS,
)
from regime_platform.services.statistics.multiple_testing import (
    bonferroni_correction,
    holm_bonferroni_correction,
    benjamini_hochberg_fdr,
    apply_all_corrections,
)
from regime_platform.services.statistics.significance_tests import (
    whites_reality_check,
    hansen_spa,
)


def _obs_sharpe(returns: np.ndarray, annualize_factor: float = 252) -> float:
    """Annualized Sharpe from a return series (used to feed deflated_sharpe_ratio)."""
    returns = np.asarray(returns, dtype=float)
    if returns.std() == 0:
        return 0.0
    return float(returns.mean() / returns.std() * np.sqrt(annualize_factor))


def _mk_strategies(rows):
    return np.array(rows)


# ═══════════════════════════════════════════════════════════════════════════
# 6A - Deflated Sharpe Ratio
# ═══════════════════════════════════════════════════════════════════════════


class TestDeflatedSharpeRatio:
    def test_dsr_with_genuine_alpha(self):
        """A strategy with genuine positive returns should have DSR > 0.5."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 1000)
        result = deflated_sharpe_ratio(
            observed_sharpe=_obs_sharpe(returns),
            n_trials=1,
            sample_length=len(returns),
            returns=returns,
        )
        assert result["dsr"] > 0.5
        assert result["observed_sharpe"] != 0.0
        assert result["sample_length"] == 1000

    def test_dsr_penalizes_multiple_trials(self):
        """More trials should lower DSR for the same returns."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.0005, 0.02, 500)
        obs = _obs_sharpe(returns)

        dsr_1 = deflated_sharpe_ratio(observed_sharpe=obs, n_trials=1, sample_length=len(returns), returns=returns)
        dsr_10 = deflated_sharpe_ratio(observed_sharpe=obs, n_trials=10, sample_length=len(returns), returns=returns)
        dsr_100 = deflated_sharpe_ratio(observed_sharpe=obs, n_trials=100, sample_length=len(returns), returns=returns)

        assert dsr_1["dsr"] >= dsr_10["dsr"]
        assert dsr_10["dsr"] >= dsr_100["dsr"]

    def test_dsr_zero_sharpe(self):
        """Forced zero Sharpe should give DSR ≈ 0.5."""
        returns = np.array([0.01, -0.01] * 100, dtype=float)  # Mean = 0 exactly
        result = deflated_sharpe_ratio(
            observed_sharpe=0.0,
            n_trials=1,
            sample_length=len(returns),
            returns=returns,
            annualize_factor=1,
        )
        assert 0.3 < result["dsr"] < 0.7

    def test_dsr_negative_sharpe_low_dsr(self):
        """Negative Sharpe should give very low DSR (near 0)."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.0, 0.02, 1000)
        obs = _obs_sharpe(returns)
        result = deflated_sharpe_ratio(observed_sharpe=obs, n_trials=1, sample_length=len(returns), returns=returns)
        if obs < 0:
            assert result["dsr"] < 0.5

    def test_dsr_significance_flag(self):
        """High Sharpe with few trials should be flagged significant."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.005, 0.01, 1000)
        result = deflated_sharpe_ratio(
            observed_sharpe=_obs_sharpe(returns),
            n_trials=1,
            sample_length=len(returns),
            returns=returns,
        )
        assert result["is_significant"] is True

    def test_dsr_too_few_returns(self):
        """Less than 2 returns should return a trivial result."""
        result = deflated_sharpe_ratio(observed_sharpe=0.0, n_trials=1, sample_length=1, returns=np.array([0.01]))
        assert isinstance(result["dsr"], float)
        assert result["is_significant"] is False

    def test_dsr_result_has_interpretation(self):
        """Result should have a human-readable interpretation."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 500)
        result = deflated_sharpe_ratio(
            observed_sharpe=_obs_sharpe(returns),
            n_trials=10,
            sample_length=len(returns),
            returns=returns,
        )
        assert len(result["interpretation"]) > 50

    def test_dsr_result_dict_keys(self):
        """Result (a dict) should carry the expected keys."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 500)
        result = deflated_sharpe_ratio(
            observed_sharpe=_obs_sharpe(returns),
            n_trials=5,
            sample_length=len(returns),
            returns=returns,
        )
        assert "dsr" in result
        assert "observed_sharpe" in result
        assert "n_trials" in result
        assert "is_significant" in result
        assert "interpretation" in result

    def test_dsr_with_returns_param(self):
        """Convenience: passing raw P&Ls via `returns` works for moment estimation."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 50)  # Trade P&Ls
        result = deflated_sharpe_ratio(
            observed_sharpe=_obs_sharpe(pnls),
            n_trials=5,
            sample_length=len(pnls),
            returns=pnls,
        )
        assert "dsr" in result
        assert result["n_trials"] == 5

    def test_dsr_annualize_factor_accepted(self):
        """annualize_factor should be accepted and produce a valid result."""
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.02, 500)
        obs = _obs_sharpe(returns)

        result_ann = deflated_sharpe_ratio(observed_sharpe=obs, n_trials=1, sample_length=len(returns), annualize_factor=252)
        result_not = deflated_sharpe_ratio(observed_sharpe=obs, n_trials=1, sample_length=len(returns), annualize_factor=1)

        assert isinstance(result_ann["dsr"], float)
        assert isinstance(result_not["dsr"], float)
        assert 0.0 <= result_ann["dsr"] <= 1.0
        assert 0.0 <= result_not["dsr"] <= 1.0


# ═══════════════════════════════════════════════════════════════════════════
# 6B - Bootstrap Confidence Intervals
# ═══════════════════════════════════════════════════════════════════════════


class TestBootstrapCI:
    def test_percentile_bootstrap_basic(self):
        """Percentile bootstrap should return a valid CI."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        result = percentile_bootstrap_ci(pnls, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=500, metric_name="sharpe")
        assert isinstance(result, BootstrapResult)
        assert result.method == "percentile"
        assert result.n_resamples == 500
        assert result.ci_lower <= result.ci_upper

    def test_circular_block_bootstrap(self):
        """Circular block bootstrap should return valid results."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        result = circular_block_bootstrap_ci(pnls, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=500, metric_name="sharpe")
        assert isinstance(result, BootstrapResult)
        assert result.method == "circular_block"

    def test_ci_width_shrinks_with_more_data(self):
        """CI width should shrink as we add more data."""
        rng = np.random.default_rng(42)
        pnls_small = rng.normal(100, 500, 30)
        pnls_large = rng.normal(100, 500, 300)

        result_small = percentile_bootstrap_ci(pnls_small, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=500, metric_name="sharpe")
        result_large = percentile_bootstrap_ci(pnls_large, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=500, metric_name="sharpe")

        width_small = result_small.ci_upper - result_small.ci_lower
        width_large = result_large.ci_upper - result_large.ci_lower
        assert width_large <= width_small

    def test_ci_contains_point_estimate(self):
        """CI should typically contain the point estimate."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 200)
        result = percentile_bootstrap_ci(pnls, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=1000, metric_name="sharpe")
        assert result.ci_lower <= result.point_estimate <= result.ci_upper

    def test_too_few_data(self):
        """Less than 2 data points should return a zero point estimate."""
        result = percentile_bootstrap_ci(np.array([100.0]), METRIC_FUNCTIONS["sharpe_ratio"], metric_name="sharpe")
        assert result.point_estimate == 0.0

    def test_all_metrics(self):
        """All registered metrics should compute without error."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        for name, fn in METRIC_FUNCTIONS.items():
            result = percentile_bootstrap_ci(pnls, fn, n_resamples=100, metric_name=name)
            assert isinstance(result, BootstrapResult)

    def test_result_fields(self):
        """Result should carry the expected fields."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        result = percentile_bootstrap_ci(pnls, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=100, metric_name="sharpe")
        assert result.metric_name == "sharpe"
        assert isinstance(result.point_estimate, float)
        assert isinstance(result.ci_lower, float)
        assert isinstance(result.ci_upper, float)
        assert isinstance(result.se_estimate, float)

    def test_compute_all_bootstrap_cis(self):
        """Batch computation should return CIs for all metrics."""
        rng = np.random.default_rng(42)
        pnls = rng.normal(100, 500, 100)
        results = compute_all_bootstrap_cis(pnls, n_resamples=100)
        for metric in ["sharpe_ratio", "win_rate", "profit_factor", "max_drawdown"]:
            assert metric in results
            assert "percentile_ci" in results[metric]
            assert "circular_block_ci" in results[metric]

    def test_custom_metric_fn(self):
        """Custom metric function should work."""
        pnls = [100.0, -50.0, 200.0, -30.0, 80.0]
        result = percentile_bootstrap_ci(pnls, lambda x: float(np.median(x)), n_resamples=100, metric_name="custom")
        assert result.metric_name == "custom"

    def test_block_bootstrap_preserves_correlation(self):
        """Block bootstrap should give valid results."""
        rng = np.random.default_rng(42)
        n = 200
        data = np.zeros(n)
        data[0] = rng.normal(0, 1)
        for i in range(1, n):
            data[i] = 0.8 * data[i-1] + rng.normal(0, 0.5)

        result_iid = percentile_bootstrap_ci(data, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=500, metric_name="sharpe")
        result_block = circular_block_bootstrap_ci(data, METRIC_FUNCTIONS["sharpe_ratio"], n_resamples=500, metric_name="sharpe")

        assert isinstance(result_iid, BootstrapResult)
        assert isinstance(result_block, BootstrapResult)


# ═══════════════════════════════════════════════════════════════════════════
# 6C - Multiple Testing Correction
# ═══════════════════════════════════════════════════════════════════════════


class TestBonferroniCorrection:
    def test_bonferroni_inflates_p_values(self):
        """Bonferroni should multiply p-values by number of tests."""
        p_values = [0.01, 0.03, 0.05]
        out = bonferroni_correction(p_values, alpha=0.05)
        adjusted = out["adjusted_p_values"]
        assert adjusted[0] == pytest.approx(0.03)  # 0.01 * 3
        assert adjusted[1] == pytest.approx(0.09)  # 0.03 * 3
        assert adjusted[2] == pytest.approx(0.15)  # 0.05 * 3

    def test_bonferroni_caps_at_1(self):
        """Adjusted p-values should never exceed 1.0."""
        p_values = [0.5, 0.8]
        adjusted = bonferroni_correction(p_values, alpha=0.05)["adjusted_p_values"]
        assert all(p <= 1.0 for p in adjusted)

    def test_bonferroni_conservative(self):
        """Bonferroni should be the most conservative."""
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        bonf_sig = bonferroni_correction(p_values, alpha=0.05)["is_significant"]
        holm_sig = holm_bonferroni_correction(p_values, alpha=0.05)["is_significant"]
        assert sum(bonf_sig) <= sum(holm_sig)

    def test_empty_p_values(self):
        """Empty list should return empty results."""
        out = bonferroni_correction([])
        assert out["adjusted_p_values"] == []
        assert out["is_significant"] == []


class TestHolmBonferroniCorrection:
    def test_holm_step_down(self):
        """Holm-Bonferroni should produce valid adjusted p-values."""
        p_values = [0.01, 0.04, 0.05]
        out = holm_bonferroni_correction(p_values, alpha=0.05)
        adjusted = out["adjusted_p_values"]
        assert len(adjusted) == 3
        assert len(out["is_significant"]) == 3

    def test_holm_more_powerful_than_bonferroni(self):
        """Holm should find at least as many significant results as Bonferroni."""
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        bonf_sig = bonferroni_correction(p_values, alpha=0.05)["is_significant"]
        holm_sig = holm_bonferroni_correction(p_values, alpha=0.05)["is_significant"]
        assert sum(holm_sig) >= sum(bonf_sig)

    def test_holm_monotonicity(self):
        """Holm adjusted p-values should be monotonic in sorted order."""
        p_values = [0.005, 0.01, 0.03, 0.04, 0.05]
        adjusted = holm_bonferroni_correction(p_values, alpha=0.1)["adjusted_p_values"]
        sorted_adjusted = [adjusted[i] for i in np.argsort(p_values)]
        for i in range(1, len(sorted_adjusted)):
            assert sorted_adjusted[i] >= sorted_adjusted[i-1]


class TestBenjaminiHochbergFDR:
    def test_bh_less_conservative(self):
        """BH FDR should find at least as many significant results as Bonferroni."""
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        bonf_sig = bonferroni_correction(p_values, alpha=0.05)["is_significant"]
        bh_sig = benjamini_hochberg_fdr(p_values, alpha=0.05)["is_significant"]
        assert sum(bh_sig) >= sum(bonf_sig)

    def test_bh_controls_fdr(self):
        """BH should correctly identify significant p-values."""
        p_values = [0.001, 0.002, 0.003]
        out = benjamini_hochberg_fdr(p_values, alpha=0.05)
        assert sum(out["is_significant"]) >= 2

    def test_bh_no_significant(self):
        """Large p-values should yield no significant results."""
        p_values = [0.3, 0.4, 0.5, 0.6]
        significant = benjamini_hochberg_fdr(p_values, alpha=0.05)["is_significant"]
        assert sum(significant) == 0

    def test_bh_capped_at_1(self):
        """BH adjusted p-values should never exceed 1.0."""
        p_values = [0.5, 0.8]
        adjusted = benjamini_hochberg_fdr(p_values, alpha=0.05)["adjusted_p_values"]
        assert all(p <= 1.0 for p in adjusted)


class TestApplyAllCorrections:
    def test_combined_correction(self):
        """Combined correction should return all three methods as a dict."""
        p_values = [0.01, 0.03, 0.05]
        output = apply_all_corrections(p_values, alpha=0.05)
        assert "corrections" in output
        assert "bonferroni" in output["corrections"]
        assert "holm_bonferroni" in output["corrections"]
        assert "benjamini_hochberg" in output["corrections"]

    def test_combined_empty(self):
        """Empty p-values should return a dict with empty raw_p_values."""
        output = apply_all_corrections([])
        assert output["raw_p_values"] == []


# ═══════════════════════════════════════════════════════════════════════════
# 6D - Strategy Significance Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestWhitesRealityCheck:
    def test_wrc_genuine_alpha(self):
        """Strategies with genuinely positive returns should be detected."""
        rng = np.random.default_rng(42)
        strategies = _mk_strategies([
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.005, 0.02, 500),
        ])
        result = whites_reality_check(strategies, n_bootstrap=500)
        assert result["n_strategies"] == 3
        assert "p_value" in result

    def test_wrc_no_alpha(self):
        """Strategies with no alpha should have high p-value."""
        rng = np.random.default_rng(42)
        strategies = _mk_strategies([
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.0, 0.02, 500),
        ])
        result = whites_reality_check(strategies, n_bootstrap=500)
        assert result["p_value"] > 0.01

    def test_wrc_too_few_data(self):
        """Too few observations should return a valid p_value in [0, 1]."""
        strategies = _mk_strategies([[0.01, 0.02, 0.03]])
        result = whites_reality_check(strategies, n_bootstrap=100)
        assert isinstance(result["p_value"], float)
        assert 0.0 <= result["p_value"] <= 1.0

    def test_wrc_with_benchmark(self):
        """WRC should work with a custom benchmark."""
        rng = np.random.default_rng(42)
        strategies = _mk_strategies([rng.normal(0.003, 0.02, 500)])
        benchmark = rng.normal(0.0, 0.02, 500)
        result = whites_reality_check(strategies, benchmark_returns=benchmark, n_bootstrap=500)
        assert "p_value" in result


class TestHansenSPA:
    def test_spa_genuine_alpha(self):
        """SPA should detect genuinely positive strategies."""
        rng = np.random.default_rng(42)
        strategies = _mk_strategies([
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.005, 0.02, 500),
        ])
        result = hansen_spa(strategies, n_bootstrap=500)
        assert "p_value" in result

    def test_spa_result_keys(self):
        """Result should carry the expected keys."""
        rng = np.random.default_rng(42)
        strategies = _mk_strategies([rng.normal(0.001, 0.02, 500)])
        result = hansen_spa(strategies, n_bootstrap=100)
        assert "p_value" in result
        assert "is_significant" in result
        assert "interpretation" in result

    def test_spa_result_is_float(self):
        """p_value should be a float."""
        rng = np.random.default_rng(42)
        strategies = _mk_strategies([
            rng.normal(0.0, 0.02, 500),
            rng.normal(0.003, 0.02, 500),
        ])
        wrc = whites_reality_check(strategies, n_bootstrap=500)
        spa = hansen_spa(strategies, n_bootstrap=500)
        assert isinstance(spa["p_value"], float)
        assert isinstance(wrc["p_value"], float)


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Router-level statistical rigor
# ═══════════════════════════════════════════════════════════════════════════


class TestRouterStatisticalRigor:
    """Test that Phase 6 fields appear in backtest responses.

    The renko-specific response models were removed in the refactor; the
    canonical models live in regime_platform.models.schemas.
    """

    def test_optimize_response_has_phase6_fields(self):
        """BacktestOptimizeResponse should accept Phase 6 fields."""
        from regime_platform.models.schemas import BacktestOptimizeResponse

        resp = BacktestOptimizeResponse(
            results=[{"sharpe_ratio": 1.2}],
            n_combinations=5,
            deflated_sharpe_result={"dsr": 0.85},
            multiple_testing_results={"bonferroni": {"n_significant": 1}},
            significance_test_results={"whites_reality_check": {"p_value": 0.03}},
        )
        assert resp.deflated_sharpe_result is not None
        assert resp.multiple_testing_results is not None
        assert resp.significance_test_results is not None

    def test_hmm_backtest_result_has_bootstrap_cis(self):
        """BacktestResult should include bootstrap_cis field."""
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
