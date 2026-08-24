"""Drift monitor tests (2026-08-13 plan, D — intraday bias-drift fast path)."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.calibration_enforcement import PENALTY_ACTIONS
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

    def test_calls_fetch_with_penalty_actions_filter(self):
        """Regression guard for the 2026-08-24 fix: check_drift must
        request the penalty-filtered fetch, not the latest-row-of-any-type
        default — otherwise a regime_shift_alert written after a fresh
        penalty defeats this function's own 'don't churn' guard."""
        with ExitStack() as stack:
            fetch_mock = AsyncMock(return_value=None)
            stack.enter_context(patch(
                "regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
                new=fetch_mock,
            ))
            stack.enter_context(patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
                new=AsyncMock(return_value={"bias": 0.05, "n": 30}),
            ))
            asyncio.run(check_drift("EURUSD"))
        fetch_mock.assert_awaited_once_with("EURUSD", actions=PENALTY_ACTIONS)


# ─── Real-interaction regression: the live-confirmed incident ──────────
#
# The tests above mock fetch_latest_eod_action directly, which cannot
# catch a bug in its OWN query filtering. This exercises the real function
# (only httpx/settings faked) against a fake nt_eod_actions table shaped
# exactly like the live incident: a regime_shift_alert newer than a still-
# fresh bayesian_penalty. Before the fix, check_drift would have seen the
# alert as "latest," found it not fresh-and-penalty, and re-fired a
# duplicate penalty despite one already being active underneath.

class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


class _FakeEodActionsClient:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        params = params or {}
        symbol = str(params.get("symbol", "")).removeprefix("eq.")
        matched = [r for r in self.rows if r["symbol"] == symbol]
        action_filter = str(params.get("action", ""))
        if action_filter.startswith("in.(") and action_filter.endswith(")"):
            allowed = set(action_filter[len("in.("):-1].split(","))
            matched = [r for r in matched if r["action"] in allowed]
        matched.sort(key=lambda r: r["ts"], reverse=True)
        limit = int(params.get("limit", 1))
        return _FakeResp(matched[:limit])


def test_real_fetch_recognizes_fresh_penalty_buried_under_regime_shift_alert():
    now = datetime.now(timezone.utc)
    rows = [
        # Explicit, clearly-ordered timestamps -- NOT two separate
        # datetime.now() calls, which can tie to the same microsecond and
        # make Python's stable sort preserve list order by accident,
        # silently passing regardless of the fix (caught during review:
        # this exact mistake produced a false-positive pass here).
        {"symbol": "USDCAD", "action": "bayesian_penalty", "bias": 0.3,
         "ts": (now - timedelta(minutes=5)).isoformat(), "detail": {"n": 40}},
        {"symbol": "USDCAD", "action": "regime_shift_alert", "bias": 0.99,
         "ts": now.isoformat(), "detail": {}},
    ]
    mock_settings = MagicMock()
    mock_settings.SUPABASE_URL = "https://x.supabase.co"
    mock_settings.SUPABASE_KEY = "k"
    client = _FakeEodActionsClient(rows)

    insert_mock = AsyncMock(return_value=True)
    with (
        patch("regime_platform.sweep_orchestrator.calibration_enforcement.settings", mock_settings),
        patch("regime_platform.sweep_orchestrator.calibration_enforcement._url",
              return_value="https://x.supabase.co/rest/v1/nt_eod_actions"),
        patch("regime_platform.sweep_orchestrator.calibration_enforcement._headers",
              return_value={"apikey": "k", "Authorization": "Bearer k"}),
        patch("httpx.AsyncClient", return_value=client),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
            new=AsyncMock(return_value={"bias": 0.42, "n": 26, "win_rate": 0.5, "avg_pred": 0.92}),
        ),
        patch("regime_platform.sweep_orchestrator.supabase_client._insert_row", new=insert_mock),
    ):
        # check_drift's own freshness gate must see the penalty underneath
        # the alert and skip (return None), not re-fire a duplicate.
        result = asyncio.run(check_drift("USDCAD"))
    assert result is None
    # The real signal: no duplicate penalty write was attempted. Without
    # this assertion, a broken freshness gate that instead crashes on an
    # unmocked network call would ALSO produce result=None (swallowed by
    # check_drift's own best-effort try/except) for the wrong reason.
    insert_mock.assert_not_awaited()
