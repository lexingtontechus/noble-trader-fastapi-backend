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


# ── Wiring: the reconcile engine has a caller (2026-10-09) ───────────────

class TestReconcileIsWired:
    """worklog 20261009_DEPENDENCY_MAP_AND_HARMONIZATION_THESIS.md.

    `reconcile_incubations` had NO caller anywhere — the forward-test gate
    recorded candidates and never evaluated them (434 rows on prod, 0 with
    n_resolved>0). These tests pin the wiring so it cannot silently regress.
    """

    def test_orchestrator_registers_the_reconcile_job(self):
        """The job must exist on the scheduler with the documented id."""
        import inspect
        from regime_platform.sweep_orchestrator import orchestrator as orch

        src = inspect.getsource(orch.SweepOrchestrator)
        assert "_run_geometry_incubation_reconcile" in src
        assert "geometry-incubation-reconcile" in src
        # Hourly at :48, clear of the sweep grid and the other fixed jobs.
        assert 'CronTrigger.from_crontab("48 * * * *")' in src

    def test_reconcile_runner_calls_the_engine(self, monkeypatch):
        """The runner must invoke reconcile_incubations (not re-implement it)."""
        import asyncio
        from regime_platform.sweep_orchestrator import orchestrator as orch
        from regime_platform.sweep_orchestrator import geometry_incubation as gi

        called = {}

        async def _fake():
            called["yes"] = True
            return {"checked": 2, "promoted": 1, "rejected": 0, "held": 1, "errors": 0}

        monkeypatch.setattr(gi, "reconcile_incubations", _fake)

        inst = object.__new__(orch.SweepOrchestrator)
        asyncio.run(orch.SweepOrchestrator._run_geometry_incubation_reconcile(inst))
        assert called.get("yes") is True

    def test_optimizer_has_no_parallel_incubation_engine(self):
        """The divergent second engine (wrong column, always keep_incubating)
        must be gone; the tier observes the real evaluator instead."""
        from regime_platform.sweep_orchestrator import dynamic_tier_optimizer as opt
        assert not hasattr(opt, "_compute_geometry_incubation")
        assert hasattr(opt, "_refresh_geometry_incubation")

    def test_optimizer_tier_reads_correct_column(self):
        """Regression guard: the tier must read `n_resolved` (the real column),
        never `n_forward_resolved` (which does not exist → always 0).

        Checks the executed code (docstring/comments stripped), so the
        explanatory note naming the old column does not trip it."""
        import inspect
        from regime_platform.sweep_orchestrator import dynamic_tier_optimizer as opt

        src = inspect.getsource(opt._refresh_geometry_incubation)
        # Drop the docstring + comment lines; only the executed body matters.
        body_lines = [
            ln for ln in src.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        # Strip the module docstring block (between the first pair of triple quotes).
        code = "\n".join(body_lines)
        if '"""' in code:
            first = code.index('"""')
            second = code.index('"""', first + 3)
            code = code[:first] + code[second + 3:]
        assert "n_forward_resolved" not in code
        assert "n_resolved" in code
