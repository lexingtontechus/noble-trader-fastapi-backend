"""Drift monitor tests (2026-08-13 plan, D — intraday bias-drift fast path).

2026-09-09 (worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
SS16, item #6): check_drift's "don't churn" freshness guard now reads
nt_symbol_calibration_state (fetch_calibration_state) instead of
reconstructing "the latest enforceable action" from nt_eod_actions via
fetch_latest_eod_action(actions=PENALTY_ACTIONS). It still inserts an
audit row into nt_eod_actions when it fires (unchanged), and additionally
upserts the new state table through the single writer,
upsert_calibration_state.
"""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from regime_platform.core import feature_flags
from regime_platform.sweep_orchestrator.drift_monitor import check_drift


def _patch_all(bias_info, current_state=None, insert_ok=True, state_ok=True):
    return [
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value=current_state),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
            new=AsyncMock(return_value=bias_info),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._insert_row",
            new=AsyncMock(return_value=insert_ok),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.upsert_calibration_state",
            new=AsyncMock(return_value=state_ok),
        ),
    ]


def _run(bias_info, current_state=None, insert_ok=True, state_ok=True):
    with ExitStack() as stack:
        for p in _patch_all(bias_info, current_state=current_state, insert_ok=insert_ok, state_ok=state_ok):
            stack.enter_context(p)
        return asyncio.run(check_drift("EURUSD"))


class TestDriftMonitor:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            feature_flags, "get_eod_drift_monitor_enabled", lambda: False
        )
        result = asyncio.run(check_drift("EURUSD"))
        assert result is None

    def test_fresh_penalty_state_skips(self):
        # A bayesian_penalty state row written just now is fresh → no churn.
        current_state = {
            "action": "bayesian_penalty",
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        result = _run(bias_info={"bias": 0.5, "n": 30}, current_state=current_state)
        assert result is None

    def test_stale_penalty_state_does_not_skip(self):
        # An old bayesian_penalty state row (beyond the fresh window) must
        # NOT block a fresh drift detection.
        from datetime import timedelta
        from regime_platform.sweep_orchestrator.calibration_enforcement import (
            ACTION_FRESH_WINDOW_HOURS,
        )
        current_state = {
            "action": "bayesian_penalty",
            "computed_at": (
                datetime.now(timezone.utc) - timedelta(hours=ACTION_FRESH_WINDOW_HOURS + 2)
            ).isoformat(),
        }
        result = _run(
            bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
            current_state=current_state,
        )
        assert result is not None
        assert result["action"] == "bayesian_penalty"

    def test_state_action_none_does_not_skip(self):
        # A CALIBRATED (action="none") state row must not block firing.
        current_state = {
            "action": "none",
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        result = _run(
            bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
            current_state=current_state,
        )
        assert result is not None

    def test_insufficient_sample_returns_none(self):
        # fetch_calibration_bias returns None below min sample → no action.
        result = _run(bias_info=None, current_state=None)
        assert result is None

    def test_bias_below_threshold_returns_none(self):
        result = _run(bias_info={"bias": 0.05, "n": 30, "win_rate": 0.85, "avg_pred": 0.90}, current_state=None)
        assert result is None

    def test_overconfident_drift_writes_penalty(self):
        result = _run(
            bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
            current_state=None,
            insert_ok=True,
        )
        assert result is not None
        assert result["action"] == "bayesian_penalty"
        assert result["symbol"] == "EURUSD"
        assert result["bias"] == 0.42
        assert result["detail"]["source"] == "drift_monitor"
        assert result["detail"]["n"] == 26

    def test_overconfident_drift_upserts_calibration_state(self):
        """The single-writer state upsert fires alongside the audit insert."""
        state_mock = AsyncMock(return_value=True)
        with ExitStack() as stack:
            for p in _patch_all(
                bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
                current_state=None,
            ):
                stack.enter_context(p)
            stack.enter_context(patch(
                "regime_platform.sweep_orchestrator.supabase_client.upsert_calibration_state",
                new=state_mock,
            ))
            result = asyncio.run(check_drift("EURUSD"))
        assert result is not None
        state_mock.assert_awaited_once()
        call_kwargs = state_mock.call_args.kwargs
        assert state_mock.call_args.args[0] == "EURUSD"
        assert call_kwargs["action"] == "bayesian_penalty"
        assert call_kwargs["status"] == "OVERCONFIDENT"
        assert call_kwargs["source"] == "drift_monitor"

    def test_insert_failure_returns_none(self):
        result = _run(
            bias_info={"bias": 0.42, "n": 26, "win_rate": 0.50, "avg_pred": 0.92},
            current_state=None,
            insert_ok=False,
        )
        assert result is None

    def test_exception_is_best_effort(self):
        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            result = asyncio.run(check_drift("EURUSD"))
        assert result is None

    def test_reads_calibration_state_not_eod_actions(self):
        """Regression guard for the 2026-09-09 state/history split:
        check_drift must read the single current-state row, not
        reconstruct it from the nt_eod_actions log."""
        with ExitStack() as stack:
            fetch_mock = AsyncMock(return_value=None)
            stack.enter_context(patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
                new=fetch_mock,
            ))
            stack.enter_context(patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
                new=AsyncMock(return_value={"bias": 0.05, "n": 30}),
            ))
            asyncio.run(check_drift("EURUSD"))
        fetch_mock.assert_awaited_once_with("EURUSD")
