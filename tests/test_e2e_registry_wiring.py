"""
Tests for the Layer 1 E2E read-site migration (worklog/PARAMETER_TUNING_PLAYBOOK.md
§2.5.2.1): every Tier D read site now checks the tuning registry BEFORE
falling back to settings/module defaults. Each test here seeds a real
registry file entry (via the same tmp_path + TUNING_REGISTRY_YAML pattern
as tests/test_tuning_registry.py) and asserts the tuned value is actually
used, not just written and ignored.

The "no registry entry -> unchanged" regression guard for each site is
already covered by the pre-existing test suites (test_calibration_enforcement.py,
test_raw_bias_cycle_reset.py, test_drift_monitor.py, etc.), which assert
exact default-math outcomes and all still pass unmodified.
"""

import asyncio

import pytest

from regime_platform.core import tuning_registry as reg
from regime_platform.sweep_orchestrator import (
    calibration_enforcement,
    drift_monitor,
    eod_correction,
)


@pytest.fixture(autouse=True)
def _isolated_registry_file(tmp_path, monkeypatch):
    yaml_path = tmp_path / "tuning_registry.yaml"
    monkeypatch.setenv("TUNING_REGISTRY_YAML", str(yaml_path))
    reg.reload()
    yield yaml_path
    reg.reload()


async def _seed(name: str, value: float) -> None:
    result = await reg.write_param(name, value, reason="test seed", updated_by="test")
    assert result["applied"] is True


# ─── eod_correction._classify_bias ──────────────────────────────────────

class TestClassifyBiasRegistryOverride:
    def test_tuned_threshold_changes_classification(self):
        # Default threshold is 0.10 -> bias=0.15 is OVERCONFIDENT today.
        assert eod_correction._classify_bias(0.15) == "OVERCONFIDENT"
        asyncio.run(_seed("EOD_BIAS_ENFORCED_THRESHOLD", 0.20))
        # With a wider tuned threshold, the same bias is now CALIBRATED.
        assert eod_correction._classify_bias(0.15) == "CALIBRATED"

    def test_symmetric_on_the_underconfident_side_too(self):
        asyncio.run(_seed("EOD_BIAS_ENFORCED_THRESHOLD", 0.20))
        assert eod_correction._classify_bias(-0.15) == "CALIBRATED"
        assert eod_correction._classify_bias(-0.25) == "UNDERCONFIDENT"


# ─── eod_correction._classify_bias_raw ──────────────────────────────────

class TestClassifyBiasRawRegistryOverride:
    def test_tuned_overconfident_threshold_is_used(self):
        assert eod_correction._classify_bias_raw(0.35) == "OVERCONFIDENT"  # default 0.30
        asyncio.run(_seed("EOD_BIAS_RAW_OVERCONFIDENT", 0.40))
        assert eod_correction._classify_bias_raw(0.35) == "CALIBRATED"

    def test_tuned_underconfident_threshold_is_used(self):
        assert eod_correction._classify_bias_raw(-0.25) == "UNDERCONFIDENT"  # default -0.20
        asyncio.run(_seed("EOD_BIAS_RAW_UNDERCONFIDENT", -0.30))
        assert eod_correction._classify_bias_raw(-0.25) == "CALIBRATED"

    def test_explicit_argument_still_overrides_everything(self):
        """An explicitly-passed threshold must win over the registry --
        preserves the existing function signature's contract."""
        asyncio.run(_seed("EOD_BIAS_RAW_OVERCONFIDENT", 0.40))
        assert eod_correction._classify_bias_raw(0.35, overconfident_threshold=0.30) == "OVERCONFIDENT"


# ─── eod_correction._stagnation_streak / _stagnation_tier (via _settings_number) ──

class TestStagnationRegistryOverride:
    def test_tuned_epsilon_widens_the_stagnation_window(self):
        # Default epsilon 0.02: a 0.03 move is NOT stagnant.
        assert eod_correction._stagnation_streak([0.45], current_bias=0.48) == 1
        asyncio.run(_seed("STAGNATION_EPSILON", 0.05))
        assert eod_correction._stagnation_streak([0.45], current_bias=0.48) == 2

    def test_tuned_tier1_cycles_changes_escalation_point(self):
        assert eod_correction._stagnation_tier(2) == 1  # default TIER1_CYCLES=2
        asyncio.run(_seed("STAGNATION_TIER1_CYCLES", 5))
        assert eod_correction._stagnation_tier(2) == 0

    def test_tuned_reset_cycles_changes_auto_recovery_point(self):
        assert eod_correction._stagnation_tier(8) == 0  # default RESET_CYCLES=8
        asyncio.run(_seed("STAGNATION_RESET_CYCLES", 12))
        assert eod_correction._stagnation_tier(8) == 2  # no longer reset at 8


# ─── eod_correction.bayesian_adjusted_p_win (BAYES_PRIOR_COUNT) ─────────

class TestEodCorrectionBayesianPriorRegistryOverride:
    def test_tuned_prior_count_changes_shrink_strength(self):
        default_result = eod_correction.bayesian_adjusted_p_win(0.90, 30)
        asyncio.run(_seed("BAYES_PRIOR_COUNT", 25))
        tuned_result = eod_correction.bayesian_adjusted_p_win(0.90, 30)
        # A bigger prior count shrinks harder toward 0.5 for the same n.
        assert tuned_result < default_result


# ─── calibration_enforcement.bayesian_adjusted_p_win (mirrored) ────────

class TestCalibrationEnforcementBayesianPriorRegistryOverride:
    def test_tuned_prior_count_changes_shrink_strength(self):
        default_result = calibration_enforcement.bayesian_adjusted_p_win(0.90, 30)
        asyncio.run(_seed("BAYES_PRIOR_COUNT", 25))
        tuned_result = calibration_enforcement.bayesian_adjusted_p_win(0.90, 30)
        assert tuned_result < default_result

    def test_both_files_read_the_same_registry_key(self):
        """The whole point of moving both mirrored copies onto the
        registry: they now agree (to eod_correction's pre-existing 4dp
        rounding -- calibration_enforcement's version doesn't round,
        a small pre-existing cosmetic difference between the two mirrored
        functions, unrelated to this migration) instead of by a sync test
        on their hardcoded defaults."""
        asyncio.run(_seed("BAYES_PRIOR_COUNT", 18))
        a = eod_correction.bayesian_adjusted_p_win(0.80, 40)
        b = calibration_enforcement.bayesian_adjusted_p_win(0.80, 40)
        assert a == pytest.approx(round(b, 4), abs=1e-9)


# ─── calibration_enforcement KELLY_REDUCTION default ────────────────────

class TestApplyCalibrationAdjustmentKellyReductionRegistryOverride:
    async def _run(self, action):
        from unittest.mock import AsyncMock, patch

        with patch(
            "regime_platform.sweep_orchestrator.calibration_enforcement.settings"
        ) as mock_settings:
            mock_settings.SUPABASE_URL = "https://x.supabase.co"
            mock_settings.SUPABASE_KEY = "k"
            mock_settings.EOD_CALIBRATION_ENFORCE = True
            with patch(
                "regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
                new=AsyncMock(return_value=action),
            ):
                return await calibration_enforcement.apply_calibration_adjustment(
                    "XAUUSD", p_win=0.65, kelly_f=0.20,
                )

    def test_tuned_kelly_reduction_used_when_no_tier1_override(self):
        from datetime import datetime, timezone

        action = {
            "action": "bayesian_penalty",
            "bias": 0.18,
            "ts": datetime.now(timezone.utc).isoformat(),
            "detail": {"n": 20},  # no kelly_reduction_override key
        }
        asyncio.run(_seed("KELLY_REDUCTION", 0.30))
        result = asyncio.run(self._run(action))
        assert result["applied"] is True
        assert result["kelly_f"] == pytest.approx(0.20 * 0.30, abs=1e-9)

    def test_tier1_override_in_detail_still_wins_over_registry(self):
        """detail.kelly_reduction_override (Tier-1 escalation, fix #3c)
        must still take precedence over the registry-tuned default --
        preserves the stagnation breaker's own escalation contract."""
        from datetime import datetime, timezone

        action = {
            "action": "bayesian_penalty",
            "bias": 0.18,
            "ts": datetime.now(timezone.utc).isoformat(),
            "detail": {"n": 20, "kelly_reduction_override": 0.15},
        }
        asyncio.run(_seed("KELLY_REDUCTION", 0.30))
        result = asyncio.run(self._run(action))
        assert result["kelly_f"] == pytest.approx(0.20 * 0.15, abs=1e-9)


# ─── drift_monitor.check_drift (EOD_DRIFT_THRESHOLD) ───────────────────

class TestDriftMonitorRegistryOverride:
    def test_tuned_threshold_changes_trigger_point(self):
        from unittest.mock import AsyncMock, patch

        async def run_with_bias(bias):
            settings_mock = AsyncMock()
            settings_mock.EOD_DRIFT_MONITOR_ENABLED = True
            with (
                patch("regime_platform.sweep_orchestrator.drift_monitor.settings", settings_mock),
                patch(
                    "regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
                    new=AsyncMock(return_value={"bias": bias, "n": 25, "win_rate": 0.5, "avg_pred": 0.5 + bias}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.supabase_client._insert_row",
                    new=AsyncMock(return_value=True),
                ),
            ):
                return await drift_monitor.check_drift("AAPL")

        # Default threshold 0.15: bias=0.18 triggers a penalty action.
        row = asyncio.run(run_with_bias(0.18))
        assert row is not None

        asyncio.run(_seed("EOD_DRIFT_THRESHOLD", 0.25))
        # With the tuned (wider) threshold, the same bias no longer triggers.
        row = asyncio.run(run_with_bias(0.18))
        assert row is None


# ─── routers/calibration.py display value matches enforcement ─────────

class TestCalibrationRouterDisplayMatchesEnforcement:
    def test_threshold_enforced_field_reflects_registry_value(self):
        from regime_platform.routers.calibration import _build_bias_response

        # 0.18 is within EOD_BIAS_ENFORCED_THRESHOLD's PARAM_BOUNDS (0.05,
        # 0.20) -- 0.22 would be silently clamped to 0.20 by design (the
        # bounds mechanism working correctly), which isn't what this test
        # is checking.
        asyncio.run(_seed("EOD_BIAS_ENFORCED_THRESHOLD", 0.18))
        resp = _build_bias_response(
            "AAPL",
            enforced={"bias": 0.15, "n": 30, "win_rate": 0.35, "avg_pred": 0.50},
            raw=None,
            window_days=7,
        )
        assert resp.threshold_enforced == pytest.approx(0.18)
        # And the status itself must agree with the displayed threshold:
        # bias=0.15 is CALIBRATED under a tuned 0.18 threshold, even
        # though it would be OVERCONFIDENT under the old hardcoded 0.10.
        assert resp.status == "CALIBRATED"
