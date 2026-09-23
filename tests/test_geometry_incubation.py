"""Geometry incubation forward-test gate — pure math + reconcile wiring.

worklog/20260923_GEOMETRY_FORWARD_TEST_SCOPE.md
"""

from __future__ import annotations

from regime_platform.core.feature_flags import (
    get_geometry_incubation_max_expiry_regression,
    get_geometry_incubation_min_days,
    get_geometry_incubation_min_resolved,
    get_geometry_incubation_mode,
)
from regime_platform.sweep_orchestrator.geometry_incubation import (
    ForwardStats,
    compute_forward_stats,
    evaluate_promotion,
)


class TestFlags:
    def test_defaults(self):
        assert get_geometry_incubation_mode() == "shadow"
        assert get_geometry_incubation_min_days() == 7
        assert get_geometry_incubation_min_resolved() == 20
        assert get_geometry_incubation_max_expiry_regression() == 0.10


class TestComputeForwardStats:
    def test_basic_aggregation(self):
        rows = (
            [{"outcome": "tp_hit"}] * 6
            + [{"outcome": "sl_hit"}] * 4
            + [{"outcome": "expired"}] * 2
        )
        stats = compute_forward_stats(rows, sl_bricks=3, tp_bricks=6)
        assert stats.wins == 6
        assert stats.losses == 4
        assert stats.expired == 2
        assert stats.n_resolved == 10
        assert stats.pct_expired == round(2 / 12, 4)
        # win_rate=0.6, rr=2.0 -> expectancy_r = 0.6*2 - 0.4 = 0.8
        assert stats.expectancy_r == 0.8

    def test_empty_rows(self):
        stats = compute_forward_stats([], sl_bricks=3, tp_bricks=6)
        assert stats.n_resolved == 0
        assert stats.pct_expired is None
        assert stats.expectancy_r is None

    def test_all_expired(self):
        rows = [{"outcome": "expired"}] * 5
        stats = compute_forward_stats(rows, sl_bricks=3, tp_bricks=6)
        assert stats.n_resolved == 0
        assert stats.pct_expired == 1.0
        assert stats.expectancy_r is None


class TestEvaluatePromotion:
    def _stats(self, **kw):
        defaults = dict(
            n_resolved=0, wins=0, losses=0, expired=0,
            pct_expired=None, expectancy_r=None,
        )
        defaults.update(kw)
        return ForwardStats(**defaults)

    def test_holds_when_not_enough_days(self):
        v = evaluate_promotion(
            days_elapsed=2.0,
            stats=self._stats(n_resolved=50, expectancy_r=0.5, pct_expired=0.1),
            baseline_pct_expired=0.2,
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "incubating"
        assert v.reason == "insufficient_days"

    def test_holds_when_not_enough_samples(self):
        v = evaluate_promotion(
            days_elapsed=10.0,
            stats=self._stats(n_resolved=5, expectancy_r=0.5, pct_expired=0.1),
            baseline_pct_expired=0.2,
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "incubating"
        assert v.reason == "insufficient_samples"

    def test_rejects_non_positive_expectancy(self):
        v = evaluate_promotion(
            days_elapsed=10.0,
            stats=self._stats(n_resolved=30, expectancy_r=-0.05, pct_expired=0.1),
            baseline_pct_expired=0.2,
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "rejected"
        assert v.reason == "expectancy_r_non_positive"

    def test_rejects_expiry_regression(self):
        v = evaluate_promotion(
            days_elapsed=10.0,
            stats=self._stats(n_resolved=30, expectancy_r=0.3, pct_expired=0.5),
            baseline_pct_expired=0.2,   # regressed 0.3 > 0.10 cap
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "rejected"
        assert v.reason == "pct_expired_regression"

    def test_promotes_within_regression_tolerance(self):
        v = evaluate_promotion(
            days_elapsed=10.0,
            stats=self._stats(n_resolved=30, expectancy_r=0.3, pct_expired=0.25),
            baseline_pct_expired=0.2,   # regressed 0.05 <= 0.10 cap
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "promoted"
        assert v.reason == "promoted"

    def test_promotes_with_no_baseline(self):
        # First-ever geometry for a symbol — nothing to regress against.
        v = evaluate_promotion(
            days_elapsed=10.0,
            stats=self._stats(n_resolved=30, expectancy_r=0.3, pct_expired=0.9),
            baseline_pct_expired=None,
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "promoted"

    def test_expectancy_none_does_not_block_promotion(self):
        # n_resolved could be 0 wins/losses with only... not realistic given
        # min_resolved gate already passed, but guard against None safely.
        v = evaluate_promotion(
            days_elapsed=10.0,
            stats=self._stats(n_resolved=25, expectancy_r=None, pct_expired=0.1),
            baseline_pct_expired=0.2,
            min_days=7, min_resolved=20, max_expiry_regression=0.10,
        )
        assert v.status == "promoted"
