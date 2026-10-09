"""Geometry incubation forward-test gate — pure math + reconcile wiring.

worklog/20260923_GEOMETRY_FORWARD_TEST_SCOPE.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from regime_platform.core.feature_flags import (
    get_geometry_incubation_max_expiry_regression,
    get_geometry_incubation_min_days,
    get_geometry_incubation_min_resolved,
    get_geometry_incubation_mode,
)
from regime_platform.sweep_orchestrator.geometry_incubation import (
    ForwardStats,
    adaptive_min_days,
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


# ── Adaptive evidence-relative time floor (Part B, 2026-10-09) ─────────

class TestAdaptiveMinDays:
    """Pure math of the adaptive time floor (worklog
    20261009_ATR_GEOMETRY_SINGLE_SOURCE.md Part B). Replaces the static
    min_days=7 with a per-symbol floor derived from the fresh resolution
    rate, clamped to [floor_days, cap_days] and fail-open to cap on no
    evidence.
    """

    def _d(self, rate, *, mr=20, floor=1, cap=7):
        return adaptive_min_days(
            rate, min_resolved=mr, floor_days=floor, cap_days=cap,
        )

    def test_no_evidence_fails_open_to_cap(self):
        # None (cold) and non-positive rates → the old static floor.
        assert self._d(None) == 7.0
        assert self._d(0) == 7.0
        assert self._d(-3.0) == 7.0

    def test_fast_symbol_gets_floor(self):
        # XAGUSD ~101/active-day: 20/101 → ceil(0.198) = 1 → clamped to floor.
        assert self._d(101.0) == 1.0

    def test_mid_symbol_scales(self):
        # ~29/active-day (US500): 20/29 → ceil(0.69) = 1.
        assert self._d(29.0) == 1.0
        # ~10/active-day: 20/10 = 2 days exactly.
        assert self._d(10.0) == 2.0
        # ~7/active-day: 20/7 → ceil(2.86) = 3.
        assert self._d(7.0) == 3.0

    def test_slow_symbol_capped(self):
        # ~1/active-day: 20/1 = 20 → clamped to cap 7 (never looser than old).
        assert self._d(1.0) == 7.0

    def test_respects_configured_bounds(self):
        # floor raises a fast symbol; cap lowers a slow one.
        assert self._d(200.0, floor=3) == 3.0
        assert self._d(1.0, cap=3) == 3.0

    def test_min_resolved_scales_the_floor(self):
        # Higher sample bar → longer floor for the same rate.
        assert self._d(10.0, mr=20) == 2.0
        assert self._d(10.0, mr=50) == 5.0


class TestAdaptiveFlags:
    def test_defaults(self):
        from regime_platform.core.feature_flags import (
            get_geometry_incubation_adaptive_days_mode,
            get_geometry_incubation_adaptive_floor_days,
            get_geometry_incubation_adaptive_cap_days,
        )
        # Ships at shadow — computes + logs, never changes the live gate.
        assert get_geometry_incubation_adaptive_days_mode() == "shadow"
        assert get_geometry_incubation_adaptive_floor_days() == 1.0
        assert get_geometry_incubation_adaptive_cap_days() == 7.0


class TestReconcileUsesAdaptiveFloor:
    """The live mode must gate on the adaptive floor; shadow must gate on the
    static floor (zero live change); off must not even fetch the rate.
    """

    def _run(self, monkeypatch, *, adaptive_mode, rate):
        import asyncio
        from regime_platform.sweep_orchestrator import geometry_incubation as gi

        seen = {}
        fetched = {}

        # One incubating row, 2 days elapsed, enough samples to promote.
        row = {
            "id": 1, "symbol": "XAGUSD", "brick_size": 0.001,
            "sl_bricks": 3, "tp_bricks": 12, "asset_class": "metal",
            "started_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            "baseline_pct_expired": 0.2,
        }

        async def _fetch_incubating():
            return [row]

        async def _fetch_forward(*a, **k):
            return [{"outcome": "tp_hit"}] * 20  # n_resolved=20, positive exp

        async def _fetch_rate(symbol, **k):
            fetched["called"] = True
            return rate

        async def _baseline(symbol):
            return 0.2

        def _eval(**kw):
            seen["min_days"] = kw["min_days"]
            return PromotionVerdict("promoted", "promoted")

        async def _patch(table, filters, patch):
            return True

        monkeypatch.setattr(gi, "_fetch_incubating", _fetch_incubating)
        monkeypatch.setattr(gi, "_fetch_forward_resolved", _fetch_forward)
        monkeypatch.setattr(gi, "fetch_resolution_rate", _fetch_rate)
        monkeypatch.setattr(gi, "fetch_baseline_pct_expired", _baseline)
        monkeypatch.setattr(gi, "evaluate_promotion", _eval)
        monkeypatch.setattr(gi, "_patch_rows", _patch)

        # Patch the flag getters the reconcile reads.
        from regime_platform.core import feature_flags as ff
        monkeypatch.setattr(ff, "get_geometry_incubation_mode", lambda: "shadow")
        monkeypatch.setattr(ff, "get_geometry_incubation_min_days", lambda: 7.0)
        monkeypatch.setattr(ff, "get_geometry_incubation_min_resolved", lambda: 20)
        monkeypatch.setattr(ff, "get_geometry_incubation_max_expiry_regression", lambda: 0.10)
        monkeypatch.setattr(ff, "get_geometry_incubation_adaptive_days_mode", lambda: adaptive_mode)
        monkeypatch.setattr(ff, "get_geometry_incubation_adaptive_floor_days", lambda: 1.0)
        monkeypatch.setattr(ff, "get_geometry_incubation_adaptive_cap_days", lambda: 7.0)

        # The reconcile imports these names locally, so patch on the module too.
        import regime_platform.sweep_orchestrator.geometry_incubation as gimod
        for name, val in [
            ("get_geometry_incubation_mode", "shadow"),
            ("get_geometry_incubation_min_days", 7.0),
            ("get_geometry_incubation_min_resolved", 20),
            ("get_geometry_incubation_max_expiry_regression", 0.10),
            ("get_geometry_incubation_adaptive_days_mode", adaptive_mode),
            ("get_geometry_incubation_adaptive_floor_days", 1.0),
            ("get_geometry_incubation_adaptive_cap_days", 7.0),
        ]:
            if hasattr(gimod, name):
                monkeypatch.setattr(gimod, name, (lambda v: (lambda: v))(val))

        asyncio.run(gi.reconcile_incubations())
        return seen, fetched

    def test_live_uses_adaptive_floor(self, monkeypatch):
        # rate 101/active-day → adaptive floor 1.0 (below static 7).
        seen, fetched = self._run(monkeypatch, adaptive_mode="live", rate=101.0)
        assert fetched.get("called") is True
        assert seen["min_days"] == 1.0

    def test_shadow_gates_on_static_floor(self, monkeypatch):
        # shadow computes the rate but still gates on static 7 — zero live change.
        seen, fetched = self._run(monkeypatch, adaptive_mode="shadow", rate=101.0)
        assert fetched.get("called") is True
        assert seen["min_days"] == 7.0

    def test_off_does_not_fetch(self, monkeypatch):
        seen, fetched = self._run(monkeypatch, adaptive_mode="off", rate=101.0)
        assert fetched.get("called") is None
        assert seen["min_days"] == 7.0
