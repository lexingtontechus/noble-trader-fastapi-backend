"""
Tests for the automated tiered stagnation breaker — fix #3c
(worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §4).

Live evidence: AAPL/US500 stayed OVERCONFIDENT at bit-for-bit identical raw
bias across 6+ consecutive 4h correction cycles (18+ hours) — the standard
actions (emergency_heavy_sweep/bayesian_penalty/kelly_reduction) re-fired
every cycle without resolving, because the root causes (HMM state-flip
noise, a negative-mu Kelly floor) aren't fixable by re-sweeping params.
This breaker generalizes the detection (bias unmoved across cycles,
regardless of WHY) and escalates automatically — no human gate, per user
direction: fully automated, tiered.
"""

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.eod_correction import (
    _stagnation_streak,
    _stagnation_tier,
    run_eod_correction,
    STAGNATION_KILL_SWITCH_ACTION,
)
from regime_platform.sweep_orchestrator.calibration_enforcement import (
    apply_calibration_adjustment,
    PENALTY_ACTIONS,
)


# ─── 1. _stagnation_streak (pure) ──────────────────────────────────────────

class TestStagnationStreak:
    def test_no_history_is_streak_one(self):
        assert _stagnation_streak([], current_bias=0.45, epsilon=0.02) == 1

    def test_consecutive_unmoved_cycles_extend_streak(self):
        # newest-first history; all within epsilon of current
        history = [0.4447, 0.4447, 0.4447]
        assert _stagnation_streak(history, current_bias=0.4447, epsilon=0.02) == 4

    def test_movement_beyond_epsilon_stops_the_streak(self):
        history = [0.44, 0.10, 0.05]  # jumped 0.44->0.10 two cycles ago
        assert _stagnation_streak(history, current_bias=0.45, epsilon=0.02) == 2

    def test_small_noise_within_epsilon_still_counts_as_stagnant(self):
        history = [0.441, 0.443, 0.439]
        assert _stagnation_streak(history, current_bias=0.4447, epsilon=0.02) == 4


# ─── 2. _stagnation_tier (pure) ────────────────────────────────────────────

class TestStagnationTier:
    def test_streak_one_is_tier_zero(self):
        assert _stagnation_tier(1) == 0

    def test_streak_two_is_tier_one(self):
        assert _stagnation_tier(2) == 1

    def test_streak_three_is_still_tier_one(self):
        assert _stagnation_tier(3) == 1

    def test_streak_four_is_tier_two(self):
        assert _stagnation_tier(4) == 2

    def test_streak_seven_is_still_tier_two(self):
        assert _stagnation_tier(7) == 2

    def test_streak_eight_resets_to_tier_zero(self):
        """Auto-recovery: a truly-stuck symbol gets a fresh start + forced
        re-sweep rather than a permanent block."""
        assert _stagnation_tier(8) == 0

    def test_streak_beyond_reset_stays_zero(self):
        assert _stagnation_tier(20) == 0


# ─── 3. run_eod_correction(raw_bias_mode=True) integration ────────────────

def _run_eod_correction_mocked(
    cal_raw,
    action_bias_history=None,
    insert_fn=None,
    symbols=None,
):
    """Mirrors tests/test_raw_bias_cycle_reset.py's mocking pattern, plus
    fetch_recent_action_bias (new dependency for the stagnation breaker)."""
    raw_fetch_mock = AsyncMock(return_value=cal_raw)
    enforced_fetch_mock = AsyncMock(return_value=None)
    insert_mock = insert_fn if insert_fn else AsyncMock(return_value=True)
    clear_mock = AsyncMock()
    clear_stagnation_history_mock = AsyncMock()
    win_rates_mock = AsyncMock(return_value={"recent_3d_wr": None, "prior_3d_wr": None})
    fetch_latest_sweep_mock = AsyncMock(return_value=None)
    active_symbols_mock = AsyncMock(return_value=symbols or [{"symbol": "AAPL"}])
    action_bias_mock = AsyncMock(return_value=action_bias_history or [])

    patches = [
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
              new=active_symbols_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_raw",
              new=raw_fetch_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
              new=enforced_fetch_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client._insert_row",
              new=insert_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client._clear_stale_eod_actions",
              new=clear_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client._clear_stagnation_history",
              new=clear_stagnation_history_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_latest_sweep",
              new=fetch_latest_sweep_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_recent_action_bias",
              new=action_bias_mock),
        patch("regime_platform.sweep_orchestrator.orchestrator._derive_venue",
              return_value=None),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = asyncio.run(run_eod_correction(raw_bias_mode=True))
    return result, insert_mock, action_bias_mock


class TestStagnationBreakerIntegration:
    def test_tier_zero_first_flag_writes_standard_three_actions_only(self):
        """First time flagged (no history) — normal behaviour, no escalation."""
        cal_raw = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        result, insert_mock, _ = _run_eod_correction_mocked(
            cal_raw, action_bias_history=[], symbols=[{"symbol": "AAPL"}],
        )
        actions = [a["action"] for a in result["actions"] if a["symbol"] == "AAPL"]
        assert set(actions) == {"emergency_heavy_sweep", "bayesian_penalty", "kelly_reduction"}
        assert STAGNATION_KILL_SWITCH_ACTION not in actions

    def test_tier_one_escalates_kelly_reduction_in_detail(self):
        """Streak=2 (1 matching prior cycle + current) -> Tier 1: deeper
        kelly_reduction_override written into detail, still only 3 actions."""
        cal_raw = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        captured = {}

        async def capture_insert(table, payload):
            captured.setdefault("payloads", []).append(payload)
            return True

        result, _, _ = _run_eod_correction_mocked(
            cal_raw, action_bias_history=[0.45], insert_fn=capture_insert,
            symbols=[{"symbol": "AAPL"}],
        )
        actions = [a["action"] for a in result["actions"] if a["symbol"] == "AAPL"]
        assert set(actions) == {"emergency_heavy_sweep", "bayesian_penalty", "kelly_reduction"}
        detail = captured["payloads"][0]["detail"]
        assert detail["stagnation_tier"] == 1
        assert detail["kelly_reduction_override"] == pytest.approx(0.25)

    def test_tier_two_writes_kill_switch_action(self):
        """Streak=4 (3 matching prior cycles + current) -> Tier 2: the 4th
        stagnation_kill_switch action is written."""
        cal_raw = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        result, _, _ = _run_eod_correction_mocked(
            cal_raw, action_bias_history=[0.45, 0.45, 0.45], symbols=[{"symbol": "AAPL"}],
        )
        actions = [a["action"] for a in result["actions"] if a["symbol"] == "AAPL"]
        assert actions.count(STAGNATION_KILL_SWITCH_ACTION) == 1
        assert len(actions) == 4

    def test_streak_reaching_reset_cycles_drops_back_to_tier_zero(self):
        """Streak=8 -> auto-recovery: back to Tier 0, no kill switch."""
        cal_raw = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        result, _, _ = _run_eod_correction_mocked(
            cal_raw,
            action_bias_history=[0.45] * 7,
            symbols=[{"symbol": "AAPL"}],
        )
        actions = [a["action"] for a in result["actions"] if a["symbol"] == "AAPL"]
        assert STAGNATION_KILL_SWITCH_ACTION not in actions
        assert len(actions) == 3

    def test_fetch_recent_action_bias_queried_with_bayesian_penalty(self):
        cal_raw = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        _, _, action_bias_mock = _run_eod_correction_mocked(
            cal_raw, action_bias_history=[], symbols=[{"symbol": "AAPL"}],
        )
        action_bias_mock.assert_awaited_once()
        _, kwargs = action_bias_mock.call_args
        assert kwargs.get("action") == "bayesian_penalty"


# ─── 4. apply_calibration_adjustment — enforcement side ────────────────────

class TestEnforcementStagnationHandling:
    def test_stagnation_kill_switch_forces_kelly_zero(self):
        async def fake_fetch(symbol, actions=None):
            return {
                "action": "stagnation_kill_switch",
                "bias": 0.45,
                "ts": None,
                "detail": {"stagnation_streak": 4, "stagnation_tier": 2},
            }

        mock_settings = AsyncMock()
        mock_settings.EOD_CALIBRATION_ENFORCE = True

        with (
            patch("regime_platform.sweep_orchestrator.calibration_enforcement.settings",
                  mock_settings),
            patch("regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
                  new=fake_fetch),
        ):
            result = asyncio.run(
                apply_calibration_adjustment("AAPL", p_win=0.90, kelly_f=0.04)
            )

        assert result["applied"] is True
        assert result["kelly_f"] == 0.0
        assert result["status"] == "STAGNATION_KILL_SWITCH"

    def test_kelly_reduction_override_respected(self):
        """A Tier 1 bayesian_penalty row with kelly_reduction_override=0.25
        must apply that factor instead of the default 0.5."""
        async def fake_fetch(symbol, actions=None):
            return {
                "action": "bayesian_penalty",
                "bias": 0.45,
                "ts": None,
                "detail": {"n": 30, "kelly_reduction_override": 0.25},
            }

        mock_settings = AsyncMock()
        mock_settings.EOD_CALIBRATION_ENFORCE = True

        with (
            patch("regime_platform.sweep_orchestrator.calibration_enforcement.settings",
                  mock_settings),
            patch("regime_platform.sweep_orchestrator.calibration_enforcement.fetch_latest_eod_action",
                  new=fake_fetch),
        ):
            result = asyncio.run(
                apply_calibration_adjustment("AAPL", p_win=0.90, kelly_f=0.04)
            )

        assert result["applied"] is True
        assert result["kelly_f"] == pytest.approx(0.04 * 0.25, abs=1e-6)

    def test_stagnation_kill_switch_in_penalty_actions(self):
        assert "stagnation_kill_switch" in PENALTY_ACTIONS


# ─── 5. _clear_stale_eod_actions clears the kill switch too ───────────────

def test_clear_stale_actions_includes_kill_switch():
    from regime_platform.sweep_orchestrator.supabase_client import _clear_stale_eod_actions

    class _FakeResp:
        status_code = 204
        text = ""

    class _FakeClient:
        def __init__(self):
            self.delete_calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, headers=None, params=None):
            self.delete_calls.append(params)
            return _FakeResp()

    client = _FakeClient()
    mock_settings = AsyncMock()
    mock_settings.SUPABASE_URL = "https://x.supabase.co"
    mock_settings.SUPABASE_KEY = "k"

    with (
        patch("regime_platform.sweep_orchestrator.supabase_client._url",
              return_value="https://x.supabase.co/rest/v1/nt_eod_actions"),
        patch("regime_platform.sweep_orchestrator.supabase_client._headers",
              return_value={"apikey": "k", "Authorization": "Bearer k"}),
        patch("regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
              return_value=client),
        patch("regime_platform.sweep_orchestrator.supabase_client.settings", mock_settings),
    ):
        asyncio.run(_clear_stale_eod_actions("AAPL"))

    assert STAGNATION_KILL_SWITCH_ACTION in client.delete_calls[0]["action"]


# ─── 6. Real cross-cycle interaction (2026-08-23 clear-order bug) ──────────
#
# Every test above mocks _clear_stale_eod_actions and fetch_recent_action_bias
# as two INDEPENDENT AsyncMocks (see _run_eod_correction_mocked) — so none of
# them can catch a bug in how those two real functions interact through the
# same nt_eod_actions table. That's exactly where the 2026-08-23 bug lived:
# the unconditional clear (fix from 20260822_STALE_ACTION_CLEARING_SCOPE.md)
# deleted bayesian_penalty rows every cycle, before fetch_recent_action_bias
# read them — so _stagnation_streak never saw more than the current cycle's
# own value, no matter how many real consecutive OVERCONFIDENT cycles ran.
#
# This section uses one shared in-memory table across the REAL
# _clear_stale_eod_actions / _clear_stagnation_history / _insert_row /
# fetch_recent_action_bias implementations (only httpx + settings are faked)
# and runs run_eod_correction() multiple times in sequence, mirroring actual
# production behavior — a symbol staying OVERCONFIDENT for several real 4h
# cycles in a row.

class _FakeResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json_body = json_body

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    @property
    def text(self):
        return ""

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


class _FakeEodActionsTable:
    """In-memory nt_eod_actions substitute shared across calls within one
    test, faithful enough (insert/delete-by-action-list/select-recent) to
    exercise the real interaction between the functions under test."""

    def __init__(self):
        self.rows: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        if "nt_eod_actions" not in url:
            return _FakeResp(200, [])
        params = params or {}
        symbol = str(params.get("symbol", "")).removeprefix("eq.")
        action = str(params.get("action", "")).removeprefix("eq.")
        limit = int(params.get("limit", 1000))
        matched = [r for r in self.rows if r["symbol"] == symbol and r["action"] == action]
        matched.sort(key=lambda r: r["ts"], reverse=True)
        return _FakeResp(200, matched[:limit])

    async def delete(self, url, headers=None, params=None):
        if "nt_eod_actions" not in url:
            return _FakeResp(204)
        params = params or {}
        symbol = str(params.get("symbol", "")).removeprefix("eq.")
        action_filter = str(params.get("action", ""))
        if action_filter.startswith("in.(") and action_filter.endswith(")"):
            actions = set(action_filter[len("in.("):-1].split(","))
        elif action_filter.startswith("eq."):
            actions = {action_filter[len("eq."):]}
        else:
            actions = set()
        self.rows = [r for r in self.rows if not (r["symbol"] == symbol and r["action"] in actions)]
        return _FakeResp(204)

    async def post(self, url, headers=None, json=None):
        if "nt_eod_actions" in url and json is not None:
            self.rows.append(dict(json))
        return _FakeResp(201)


def _run_real_cycle(table, cal_raw, symbol="AAPL"):
    """Run one real run_eod_correction(raw_bias_mode=True) cycle against the
    shared fake table — only the bias-fetch/symbol-list/heavy-sweep-lookup
    boundary is mocked; the clear/write/read chain under test is real."""
    from regime_platform.core.settings import settings as real_settings

    mock_settings = AsyncMock(wraps=real_settings)
    mock_settings.SUPABASE_URL = "https://x.supabase.co"
    mock_settings.SUPABASE_KEY = "k"

    patches = [
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
              new=AsyncMock(return_value=[{"symbol": symbol}])),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_raw",
              new=AsyncMock(return_value=cal_raw)),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias",
              new=AsyncMock(return_value=None)),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_latest_sweep",
              new=AsyncMock(return_value=None)),
        patch("regime_platform.sweep_orchestrator.orchestrator._derive_venue",
              return_value=None),
        patch("regime_platform.sweep_orchestrator.supabase_client.settings", mock_settings),
        patch("regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
              return_value=table),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = asyncio.run(run_eod_correction(raw_bias_mode=True))
    return result


class TestStagnationHistorySurvivesRealClearCycle:
    def test_streak_accumulates_across_real_consecutive_cycles(self):
        """Regression test for the 2026-08-23 bug: 4 consecutive real cycles
        with an unmoved bias must reach Tier 2 (streak=4), not stay stuck at
        Tier 0 (streak=1 forever) because the cycle-boundary clear wiped the
        very history this test's own earlier cycles wrote."""
        table = _FakeEodActionsTable()
        cal_raw = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}

        tiers = []
        for _ in range(4):
            result = _run_real_cycle(table, cal_raw)
            actions = [a for a in result["actions"] if a["symbol"] == "AAPL"]
            bp_rows = [r for r in table.rows if r["action"] == "bayesian_penalty"]
            tiers.append(bp_rows[-1]["detail"]["stagnation_tier"])

        assert tiers == [0, 1, 1, 2], (
            f"stagnation tier must escalate across real consecutive cycles, got {tiers} "
            "— streak stuck at 1 every cycle means the clear-order bug regressed"
        )
        kill_switch_rows = [r for r in table.rows if r["action"] == STAGNATION_KILL_SWITCH_ACTION]
        assert len(kill_switch_rows) == 1

    def test_recovery_to_calibrated_resets_history_for_next_streak(self):
        """A symbol that recovers to CALIBRATED for one cycle, then goes
        OVERCONFIDENT again, must start a fresh streak (=1) — not resume
        counting from its pre-recovery history."""
        table = _FakeEodActionsTable()
        overconfident = {"bias": 0.45, "n": 30, "win_rate": 0.30,
                          "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        calibrated = {"bias": 0.02, "n": 30, "win_rate": 0.50,
                      "avg_pred": 0.52, "avg_pred_raw": 0.52, "status": "CALIBRATED"}

        _run_real_cycle(table, overconfident)
        _run_real_cycle(table, overconfident)
        bp_rows = [r for r in table.rows if r["action"] == "bayesian_penalty"]
        assert bp_rows[-1]["detail"]["stagnation_tier"] == 1  # streak=2, confirms it was accumulating

        _run_real_cycle(table, calibrated)  # recovery — should clear bayesian_penalty history
        assert not [r for r in table.rows if r["action"] == "bayesian_penalty"]

        _run_real_cycle(table, overconfident)  # stuck again — should be a FRESH streak
        bp_rows = [r for r in table.rows if r["action"] == "bayesian_penalty"]
        assert bp_rows[-1]["detail"]["stagnation_tier"] == 0, "streak must restart at 1 (tier 0) after recovery"
