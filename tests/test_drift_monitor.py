"""Drift monitor tests (2026-08-13 plan, D — intraday bias-drift fast path)."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.drift_monitor import check_drift


def _patch_all(bias_info, latest=None, insert_ok=True):
    return [
        patch(
            "regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
            new=AsyncMock(return_value=latest),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
            new=AsyncMock(return_value=bias_info),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._insert_row",
            new=AsyncMock(return_value=insert_ok),
        ),
    ]


def _run(bias_info, latest=None, insert_ok=True):
    with ExitStack() as stack:
        for p in _patch_all(bias_info, latest=latest, insert_ok=insert_ok):
            stack.enter_context(p)
        return asyncio.run(check_drift("EURUSD"))


class TestDriftMonitor:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "EOD_DRIFT_MONITOR_ENABLED", False)
        result = asyncio.run(check_drift("EURUSD"))
        assert result is None

    def test_fresh_penalty_action_skips(self):
        # A bayesian_penalty written just now is fresh → no churn.
        latest = {
            "action": "bayesian_penalty",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        result = _run(bias_info={"bias": 0.5, "n": 30}, latest=latest)
        assert result is None

    def test_insufficient_sample_returns_none(self):
        # fetch_calibration_bias returns None below min sample → no action.
        result = _run(bias_info=None, latest=None)
        assert result is None

    def test_bias_below_threshold_returns_none(self):
        result = _run(bias_info={"bias": 0.05, "n": 30, "win_rate": 0.85, "avg_pred": 0.90}, latest=None)
        assert result is None

    def test_overconfident_drift_writes_penalty(self):
        result = _run(
            bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
            latest=None,
            insert_ok=True,
        )
        assert result is not None
        assert result["action"] == "bayesian_penalty"
        assert result["symbol"] == "EURUSD"
        assert result["bias"] == 0.42
        assert result["detail"]["source"] == "drift_monitor"
        assert result["detail"]["n"] == 26

    def test_insert_failure_returns_none(self):
        result = _run(
            bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
            latest=None,
            insert_ok=False,
        )
        assert result is None

    def test_exception_is_best_effort(self):
        with (
            patch(
                "regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            result = asyncio.run(check_drift("EURUSD"))
        assert result is None
