"""
Tests for the Layer 1 auto-tuner (worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.2).

Covers: the pure decision logic (classify_result, pick_direction, baseline
parsing) with no I/O, then the async evaluate/pick/run flow with the
Supabase and tuning_registry calls mocked (no live network, no real
file I/O for the integration-level tests).
"""

from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator import auto_tuner


# ─── Pure logic: classify_result ────────────────────────────────────────

class TestClassifyResult:
    def test_lower_metric_is_improved(self):
        assert auto_tuner.classify_result(0.10, baseline=0.20) == "improved"

    def test_higher_metric_is_regressed(self):
        assert auto_tuner.classify_result(0.30, baseline=0.20) == "regressed"

    def test_within_epsilon_is_neutral(self):
        assert auto_tuner.classify_result(0.205, baseline=0.20, eval_epsilon=0.01) == "neutral"

    def test_exactly_at_epsilon_boundary_is_neutral(self):
        assert auto_tuner.classify_result(0.21, baseline=0.20, eval_epsilon=0.01) == "neutral"


# ─── Pure logic: pick_direction ─────────────────────────────────────────

class TestPickDirection:
    def test_no_history_defaults_to_plus_one(self):
        assert auto_tuner.pick_direction([]) == 1

    def test_all_pending_defaults_to_plus_one(self):
        history = [{"cycle_result": "pending", "value": 0.5, "previous_value": 0.3}]
        assert auto_tuner.pick_direction(history) == 1

    def test_improved_keeps_same_direction_increasing(self):
        history = [{"cycle_result": "improved", "value": 0.5, "previous_value": 0.3}]
        assert auto_tuner.pick_direction(history) == 1

    def test_improved_keeps_same_direction_decreasing(self):
        history = [{"cycle_result": "improved", "value": 0.3, "previous_value": 0.5}]
        assert auto_tuner.pick_direction(history) == -1

    def test_regressed_flips_direction(self):
        # last move was an INCREASE (0.3 -> 0.5) and it regressed -> try decreasing next
        history = [{"cycle_result": "regressed", "value": 0.5, "previous_value": 0.3}]
        assert auto_tuner.pick_direction(history) == -1

    def test_neutral_keeps_same_direction(self):
        history = [{"cycle_result": "neutral", "value": 0.5, "previous_value": 0.3}]
        assert auto_tuner.pick_direction(history) == 1

    def test_skips_pending_entries_to_find_last_evaluated(self):
        history = [
            {"cycle_result": "pending", "value": 0.6, "previous_value": 0.5},
            {"cycle_result": "regressed", "value": 0.5, "previous_value": 0.3},
        ]
        assert auto_tuner.pick_direction(history) == -1


# ─── Pure logic: baseline parsing ───────────────────────────────────────

class TestParseBaselineMetric:
    def test_parses_valid_reason(self):
        assert auto_tuner._parse_baseline_metric("auto_tune baseline_metric=0.123456") == pytest.approx(0.123456)

    def test_parses_negative_and_scientific_notation(self):
        assert auto_tuner._parse_baseline_metric("x baseline_metric=-1.5e-2 y") == pytest.approx(-0.015)

    def test_none_reason_returns_none(self):
        assert auto_tuner._parse_baseline_metric(None) is None

    def test_missing_pattern_returns_none(self):
        assert auto_tuner._parse_baseline_metric("some other reason string") is None


# ─── _evaluate_pending ──────────────────────────────────────────────────

@pytest.mark.asyncio
class TestEvaluatePending:
    async def test_no_history_returns_none(self):
        with patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=[])):
            outcome = await auto_tuner._evaluate_pending("MIN_RR", metric_now=0.1)
        assert outcome is None

    async def test_not_pending_returns_none(self):
        history = [{"cycle_result": "improved", "reason": "auto_tune baseline_metric=0.2"}]
        with patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=history)):
            outcome = await auto_tuner._evaluate_pending("MIN_RR", metric_now=0.1)
        assert outcome is None

    async def test_unparseable_baseline_returns_none(self):
        history = [{"cycle_result": "pending", "reason": "no baseline here"}]
        with patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=history)):
            outcome = await auto_tuner._evaluate_pending("MIN_RR", metric_now=0.1)
        assert outcome is None

    async def test_improved_marks_result_no_revert(self):
        history = [{
            "id": "row-1", "cycle_result": "pending",
            "reason": "auto_tune baseline_metric=0.30",
            "value": 1.6, "previous_value": 1.5,
        }]
        mark_mock = AsyncMock(return_value=True)
        write_mock = AsyncMock()
        with (
            patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=history)),
            patch.object(auto_tuner.tuning_registry, "write_param", new=write_mock),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.update_tuning_param_cycle_result",
                new=mark_mock,
            ),
        ):
            outcome = await auto_tuner._evaluate_pending("MIN_RR", metric_now=0.10)  # 0.10 < 0.30 - eps -> improved

        assert outcome["result"] == "improved"
        assert outcome["reverted"] is False
        mark_mock.assert_awaited_once_with("row-1", "improved")
        write_mock.assert_not_awaited()

    async def test_regressed_marks_result_and_reverts(self):
        history = [{
            "id": "row-2", "cycle_result": "pending",
            "reason": "auto_tune baseline_metric=0.10",
            "value": 1.6, "previous_value": 1.5,
        }]
        mark_mock = AsyncMock(return_value=True)
        write_mock = AsyncMock(return_value={"applied": True, "value": 1.5})
        with (
            patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=history)),
            patch.object(auto_tuner.tuning_registry, "write_param", new=write_mock),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.update_tuning_param_cycle_result",
                new=mark_mock,
            ),
        ):
            outcome = await auto_tuner._evaluate_pending("MIN_RR", metric_now=0.30)  # 0.30 > 0.10 + eps -> regressed

        assert outcome["result"] == "regressed"
        assert outcome["reverted"] is True
        mark_mock.assert_awaited_once_with("row-2", "regressed")
        write_mock.assert_awaited_once()
        call_args = write_mock.call_args
        assert call_args.args[0] == "MIN_RR"
        assert call_args.args[1] == 1.5  # reverted to previous_value


# ─── _pick_next_param ───────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPickNextParam:
    async def test_never_tuned_param_picked_immediately(self):
        async def fake_history(name, limit=1):
            if name == "KELLY_REDUCTION":
                return []
            return [{"ts": "2026-08-20T00:00:00Z"}]

        with patch.object(auto_tuner.tuning_registry, "get_history", side_effect=fake_history):
            target = await auto_tuner._pick_next_param(("EOD_DRIFT_THRESHOLD", "KELLY_REDUCTION"))
        assert target == "KELLY_REDUCTION"

    async def test_oldest_ts_picked_among_tuned_params(self):
        async def fake_history(name, limit=1):
            ts_map = {
                "EOD_DRIFT_THRESHOLD": "2026-08-22T00:00:00Z",
                "KELLY_REDUCTION": "2026-08-20T00:00:00Z",  # oldest -> picked
                "STAGNATION_EPSILON": "2026-08-23T00:00:00Z",
            }
            return [{"ts": ts_map[name]}]

        with patch.object(auto_tuner.tuning_registry, "get_history", side_effect=fake_history):
            target = await auto_tuner._pick_next_param(
                ("EOD_DRIFT_THRESHOLD", "KELLY_REDUCTION", "STAGNATION_EPSILON")
            )
        assert target == "KELLY_REDUCTION"


# ─── _compute_system_health_metric ──────────────────────────────────────

@pytest.mark.asyncio
class TestComputeSystemHealthMetric:
    async def test_averages_abs_bias_across_symbols(self):
        async def fake_bias(symbol, **kwargs):
            return {"AAPL": {"bias": 0.4}, "US500": {"bias": -0.2}}.get(symbol)

        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                new=AsyncMock(return_value=[{"symbol": "AAPL"}, {"symbol": "US500"}]),
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_raw",
                side_effect=fake_bias,
            ),
        ):
            metric = await auto_tuner._compute_system_health_metric()
        assert metric == pytest.approx((0.4 + 0.2) / 2)

    async def test_none_when_no_symbol_has_enough_data(self):
        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                new=AsyncMock(return_value=[{"symbol": "AAPL"}]),
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_raw",
                new=AsyncMock(return_value=None),
            ),
        ):
            metric = await auto_tuner._compute_system_health_metric()
        assert metric is None

    async def test_none_when_fetch_active_symbols_fails(self):
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            new=AsyncMock(side_effect=Exception("network error")),
        ):
            metric = await auto_tuner._compute_system_health_metric()
        assert metric is None


# ─── run_auto_tuning — full cycle, mocked ───────────────────────────────

@pytest.mark.asyncio
class TestRunAutoTuning:
    async def test_kill_switch_skips_everything(self, monkeypatch):
        monkeypatch.setattr(auto_tuner.settings, "TUNING_AUTONOMY_ENABLED", False)
        summary = await auto_tuner.run_auto_tuning()
        assert summary["skipped"] == "tuning_autonomy_disabled"
        assert summary["tuned"] == 0

    async def test_insufficient_data_skips_tuning(self, monkeypatch):
        monkeypatch.setattr(auto_tuner.settings, "TUNING_AUTONOMY_ENABLED", True)
        with patch.object(auto_tuner, "_compute_system_health_metric", new=AsyncMock(return_value=None)):
            summary = await auto_tuner.run_auto_tuning()
        assert summary["skipped"] == "insufficient_data"
        assert summary["tuned"] == 0

    async def test_happy_path_evaluates_and_tunes_one_param(self, monkeypatch):
        monkeypatch.setattr(auto_tuner.settings, "TUNING_AUTONOMY_ENABLED", True)
        write_mock = AsyncMock(return_value={"applied": True, "value": 0.11, "rate_limited": True})

        with (
            patch.object(auto_tuner, "_compute_system_health_metric", new=AsyncMock(return_value=0.15)),
            patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=[])),
            patch.object(auto_tuner.tuning_registry, "write_param", new=write_mock),
            patch.object(auto_tuner.tuning_registry, "read_param", return_value=0.10),
        ):
            summary = await auto_tuner.run_auto_tuning()

        assert summary["checked"] == len(auto_tuner.TIER_D_PILOT_PARAMS)
        assert summary["evaluated"] == 0  # no history anywhere -> nothing pending to evaluate
        assert summary["tuned"] == 1
        assert summary["target"] in auto_tuner.TIER_D_PILOT_PARAMS
        write_mock.assert_awaited_once()
        # Reason must carry the baseline metric for the NEXT cycle to evaluate against.
        _, kwargs = write_mock.call_args
        assert "baseline_metric=0.150000" in kwargs["reason"]

    async def test_probe_value_is_the_bound_extreme_in_chosen_direction(self, monkeypatch):
        """Layer 1 requests the full bound extreme; Layer 0's rate limiter
        is what clips it to a small step (separation of concerns)."""
        monkeypatch.setattr(auto_tuner.settings, "TUNING_AUTONOMY_ENABLED", True)
        write_mock = AsyncMock(return_value={"applied": True, "value": 1.6, "rate_limited": True})

        with (
            patch.object(auto_tuner, "_compute_system_health_metric", new=AsyncMock(return_value=0.15)),
            patch.object(auto_tuner.tuning_registry, "get_history", new=AsyncMock(return_value=[])),
            patch.object(auto_tuner, "_pick_next_param", new=AsyncMock(return_value="MIN_RR")),
            patch.object(auto_tuner, "pick_direction", return_value=1),
            patch.object(auto_tuner.tuning_registry, "write_param", new=write_mock),
            patch.object(auto_tuner.tuning_registry, "read_param", return_value=1.5),
        ):
            await auto_tuner.run_auto_tuning()

        args, _ = write_mock.call_args
        lo, hi = auto_tuner.tuning_registry.PARAM_BOUNDS["MIN_RR"]
        assert args[0] == "MIN_RR"
        assert args[1] == hi  # direction=+1 -> probe the UPPER bound
