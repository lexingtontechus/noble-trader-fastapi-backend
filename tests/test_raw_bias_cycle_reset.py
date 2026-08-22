"""Tests for raw-bias cycle reset (2026-08-22 plan).

Covers:
  1. ``fetch_calibration_bias_raw`` — reads predicted_p_win_raw (pre-enforcement),
     falls back to predicted_p_win when raw IS NULL, returns avg_pred_raw.
  2. ``_classify_bias_raw`` — wider thresholds (0.30 / -0.20) for raw model output.
  3. ``run_eod_correction(raw_bias_mode=True)`` — dispatches to the raw fetcher,
     clears stale penalty actions for CALIBRATED symbols, writes correction
     actions for over/underconfident ones using raw p_win.
  4. ``_clear_stale_eod_actions`` — DELETEs penalty actions, preserves audit-only
     rows, best-effort (never raises).
  5. Orchestrator wiring: ``_run_eod_correction_only`` calls
     ``run_eod_correction(raw_bias_mode=True)``.
  6. Settings: EOD_BIAS_RAW_OVERCONFIDENT / EOD_BIAS_RAW_UNDERCONFIDENT defaults.
"""
import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_calibration_bias_raw,
    _clear_stale_eod_actions,
)
from regime_platform.sweep_orchestrator.eod_correction import (
    run_eod_correction,
    _classify_bias_raw,
    _classify_bias,
    DEFAULT_RAW_OVERCONFIDENT,
    DEFAULT_RAW_UNDERCONFIDENT,
    OVERCONFIDENT_BIAS,
    UNDERCONFIDENT_BIAS,
)


# ─── Fake HTTP layer (mirrors test_calibration_bias_window.py pattern) ──

class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload) if isinstance(payload, (list, dict)) else str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    @property
    def is_success(self):
        return self.status_code < 400


class _FakeDeleteResp:
    def __init__(self, status=204):
        self.status_code = status
        self.text = ""
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeAsyncClient:
    """Records calls and returns pre-seeded responses."""
    def __init__(self, get_rows=None):
        self.get_rows = get_rows or []
        self.get_calls = []
        self.post_calls = []
        self.delete_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.get_calls.append({"url": url, "params": params})
        return FakeResp(self.get_rows)

    async def post(self, url, headers=None, json=None):
        self.post_calls.append({"url": url, "json": json})
        return FakeResp([], status=201)

    async def delete(self, url, headers=None, params=None):
        self.delete_calls.append({"url": url, "params": params})
        return _FakeDeleteResp(status=204)


# ─── Row builders ────────────────────────────────────────────────────────

def _rows_with_raw(n, wins, raw=0.90, enforced=0.55):
    """Build n calibration records with both raw and enforced p_win."""
    out = []
    for i in range(n):
        out.append({
            "predicted_p_win": enforced,
            "predicted_p_win_raw": raw,
            "outcome": "win" if i < wins else "loss",
        })
    return out


def _rows_raw_null(n, wins, enforced=0.50):
    """Records where predicted_p_win_raw IS NULL (fallback path)."""
    out = []
    for i in range(n):
        out.append({
            "predicted_p_win": enforced,
            "predicted_p_win_raw": None,
            "outcome": "win" if i < wins else "loss",
        })
    return out


# ─── 1. fetch_calibration_bias_raw ──────────────────────────────────────

def _run_raw(symbol="EURUSD", rows=None, **kwargs):
    client = FakeAsyncClient(get_rows=rows or [])
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._url",
            return_value="https://x.supabase.co/rest/v1/nt_calibration_records",
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._headers",
            return_value={"apikey": "k", "Authorization": "Bearer k"},
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        result = asyncio.run(fetch_calibration_bias_raw(symbol, **kwargs))
    return result, client


class TestFetchCalibrationBiasRaw:
    def test_raw_bias_uses_raw_pwin_not_enforced(self):
        """The core fix: bias is avg(raw) - win_rate, not avg(enforced) - win_rate."""
        rows = _rows_with_raw(30, 10, raw=0.90, enforced=0.55)
        result, _ = _run_raw(rows=rows)
        assert result is not None
        assert result["bias"] == pytest.approx(0.5667, abs=1e-3)
        assert result["avg_pred"] == pytest.approx(0.55, abs=1e-4)
        assert result["avg_pred_raw"] == pytest.approx(0.90, abs=1e-4)

    def test_fallback_to_enforced_when_raw_is_null(self):
        """When predicted_p_win_raw IS NULL, bias falls back to enforced p_win."""
        rows = _rows_raw_null(30, 15, enforced=0.80)
        result, _ = _run_raw(rows=rows)
        assert result is not None
        assert result["bias"] == pytest.approx(0.30, abs=1e-3)
        assert result["avg_pred"] == pytest.approx(0.80, abs=1e-4)
        assert result["avg_pred_raw"] == pytest.approx(0.80, abs=1e-4)

    def test_select_includes_raw_column(self):
        """The GET request must request predicted_p_win_raw in the select."""
        rows = _rows_with_raw(25, 20)
        _, client = _run_raw(rows=rows)
        params = client.get_calls[0]["params"]
        assert "predicted_p_win_raw" in params["select"]
        assert "predicted_p_win" in params["select"]
        assert "outcome" in params["select"]

    def test_below_min_records_returns_none(self):
        rows = _rows_with_raw(19, 10)
        result, _ = _run_raw(rows=rows)
        assert result is None

    def test_default_min_records_from_settings(self):
        rows = _rows_with_raw(19, 10)
        result, _ = _run_raw(rows=rows)
        assert result is None

    def test_status_classification(self):
        """Raw bias classifies correctly using the wider thresholds."""
        rows = _rows_with_raw(30, 9, raw=0.95)
        result, _ = _run_raw(rows=rows)
        assert result["status"] == "OVERCONFIDENT"

        rows = _rows_with_raw(30, 23, raw=0.80)
        result, _ = _run_raw(rows=rows)
        assert result["status"] == "CALIBRATED"

        rows = _rows_with_raw(30, 15, raw=0.30)
        result, _ = _run_raw(rows=rows)
        assert result["status"] == "UNDERCONFIDENT"

    def test_venue_filter_preserved(self):
        rows = _rows_with_raw(25, 20)
        _, client = _run_raw(rows=rows, venue="OANDA")
        assert client.get_calls[0]["params"]["venue"] == "eq.OANDA"

    def test_suppress_url_returns_none(self):
        """When SUPABASE_URL is unset, returns None without making a request."""
        mock_s = AsyncMock()
        mock_s.SUPABASE_URL = ""
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.settings",
            mock_s,
        ):
            result = asyncio.run(fetch_calibration_bias_raw("EURUSD"))
        assert result is None


# ─── 2. _classify_bias_raw ──────────────────────────────────────────────

class TestClassifyBiasRaw:
    def test_default_thresholds_match_plan(self):
        assert DEFAULT_RAW_OVERCONFIDENT == 0.30
        assert DEFAULT_RAW_UNDERCONFIDENT == -0.20

    def test_overconfident_at_threshold(self):
        assert _classify_bias_raw(0.30) == "OVERCONFIDENT"

    def test_just_below_overconfident_threshold_is_calibrated(self):
        assert _classify_bias_raw(0.29) == "CALIBRATED"

    def test_underconfident_at_threshold(self):
        assert _classify_bias_raw(-0.20) == "UNDERCONFIDENT"

    def test_just_above_underconfident_threshold_is_calibrated(self):
        assert _classify_bias_raw(-0.19) == "CALIBRATED"

    def test_user_band_boundary(self):
        """The user's range -0.2 < bias < 0.7 — 0.7 is deep overconfident."""
        assert _classify_bias_raw(0.7) == "OVERCONFIDENT"
        assert _classify_bias_raw(-0.2) == "UNDERCONFIDENT"
        assert _classify_bias_raw(0.0) == "CALIBRATED"

    def test_settings_override(self):
        """Thresholds can be overridden via settings."""
        orig_oc = settings.EOD_BIAS_RAW_OVERCONFIDENT
        orig_uc = settings.EOD_BIAS_RAW_UNDERCONFIDENT
        settings.EOD_BIAS_RAW_OVERCONFIDENT = 0.50
        settings.EOD_BIAS_RAW_UNDERCONFIDENT = -0.30
        try:
            assert _classify_bias_raw(0.40) == "CALIBRATED"
            assert _classify_bias_raw(0.50) == "OVERCONFIDENT"
            assert _classify_bias_raw(-0.30) == "UNDERCONFIDENT"
        finally:
            settings.EOD_BIAS_RAW_OVERCONFIDENT = orig_oc
            settings.EOD_BIAS_RAW_UNDERCONFIDENT = orig_uc

    def test_raw_thresholds_wider_than_enforced(self):
        """Raw mode uses wider band than enforced mode (0.10 vs 0.30)."""
        assert _classify_bias(0.20) == "OVERCONFIDENT"
        assert _classify_bias_raw(0.20) == "CALIBRATED"


# ─── 3. run_eod_correction(raw_bias_mode=True) ──────────────────────────
#
# eod_correction.py imports Supabase helpers LAZILY inside run_eod_correction(),
# so we patch them at their SOURCE module (supabase_client module level)
# to intercept the lazy import.

def _run_eod_correction_mocked(
    raw_bias_mode,
    cal_raw=None,
    cal_enforced=None,
    insert_fn=None,
    clear_fn=None,
    win_rates=None,
    symbols=None,
    raw_oc=0.30,
    raw_uc=-0.20,
):
    """Run run_eod_correction with all Supabase calls mocked.

    Returns (result, insert_mock, clear_mock, raw_fetch_mock, enforced_fetch_mock).
    """
    raw_fetch_mock = AsyncMock(return_value=cal_raw)
    enforced_fetch_mock = AsyncMock(return_value=cal_enforced)
    insert_mock = insert_fn if insert_fn else AsyncMock(return_value=True)
    clear_mock = clear_fn if clear_fn else AsyncMock()
    win_rates_mock = AsyncMock(
        return_value=win_rates if win_rates else {"recent_3d_wr": None, "prior_3d_wr": None}
    )
    fetch_latest_sweep_mock = AsyncMock(return_value=None)
    active_symbols_mock = AsyncMock(
        return_value=symbols if symbols else [{"symbol": "EURUSD"}]
    )
    # Stagnation breaker (fix #3c, 2026-08-22) added a new dependency —
    # fetch_recent_action_bias — to run_eod_correction's OVERCONFIDENT/
    # UNDERCONFIDENT branch. Mock it to no-history (streak=1, Tier 0) so
    # these pre-existing tests keep asserting the un-escalated 3-action
    # behaviour; see tests/test_stagnation_breaker.py for tier coverage.
    action_bias_mock = AsyncMock(return_value=[])

    mock_settings = AsyncMock()
    # Explicit (not auto-mocked) so _stagnation_tier's settings reads resolve
    # to real numbers rather than MagicMock's default __float__ (1.0).
    mock_settings.STAGNATION_EPSILON = 0.02
    mock_settings.STAGNATION_TIER1_CYCLES = 2
    mock_settings.STAGNATION_TIER2_CYCLES = 4
    mock_settings.STAGNATION_RESET_CYCLES = 8
    mock_settings.STAGNATION_KELLY_REDUCTION_TIER1 = 0.25
    mock_settings.EOD_BIAS_RAW_OVERCONFIDENT = raw_oc
    mock_settings.EOD_BIAS_RAW_UNDERCONFIDENT = raw_uc

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
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_latest_sweep",
              new=fetch_latest_sweep_mock),
        patch("regime_platform.sweep_orchestrator.supabase_client.fetch_recent_action_bias",
              new=action_bias_mock),
        patch("regime_platform.sweep_orchestrator.eod_correction._fetch_recent_win_rates",
              new=win_rates_mock),
        patch("regime_platform.sweep_orchestrator.orchestrator._derive_venue",
              return_value=None),
        patch("regime_platform.core.settings.settings", mock_settings),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = asyncio.run(run_eod_correction(raw_bias_mode=raw_bias_mode))

    return result, insert_mock, clear_mock, raw_fetch_mock, enforced_fetch_mock


class TestRunEodCorrectionRawMode:
    def test_uses_raw_fetcher_in_raw_mode(self):
        """run_eod_correction(raw_bias_mode=True) calls fetch_calibration_bias_raw."""
        cal_raw = {"bias": 0.50, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        result, insert_mock, clear_mock, raw_mock, enforced_mock = _run_eod_correction_mocked(
            raw_bias_mode=True,
            cal_raw=cal_raw,
            symbols=[{"symbol": "EURUSD"}],
        )
        raw_mock.assert_awaited_once()
        enforced_mock.assert_not_awaited()

    def test_does_not_use_enforced_fetcher_in_raw_mode(self):
        """In raw mode, fetch_calibration_bias (enforced) must NOT be called."""
        cal_raw = {"bias": 0.50, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        _, _, _, _, enforced_mock = _run_eod_correction_mocked(
            raw_bias_mode=True,
            cal_raw=cal_raw,
            symbols=[{"symbol": "EURUSD"}],
        )
        enforced_mock.assert_not_awaited()

    def test_calibrated_symbol_clears_stale_actions(self):
        """CALIBRATED symbols trigger _clear_stale_eod_actions (the cycle reset)."""
        cal_raw = {"bias": 0.05, "n": 30, "win_rate": 0.80,
                   "avg_pred": 0.82, "avg_pred_raw": 0.85, "status": "CALIBRATED"}
        insert_mock = AsyncMock(return_value=True)
        clear_mock = AsyncMock()
        result, _, clear_mock_ret, _, _ = _run_eod_correction_mocked(
            raw_bias_mode=True,
            cal_raw=cal_raw,
            symbols=[{"symbol": "AUDUSD"}],
            insert_fn=insert_mock,
            clear_fn=clear_mock,
            win_rates={"recent_3d_wr": 0.80, "prior_3d_wr": 0.80},
        )
        # With unconditional 4h cycle reset, clearing is called once unconditionally
        # (the CALIBRATED path no longer calls it separately since it's already cleared)
        clear_mock_ret.assert_awaited_once_with("AUDUSD")
        insert_mock.assert_not_awaited()  # CALIBRATED → no penalty actions written

    def test_overconfident_writes_three_actions(self):
        """Overconfident raw bias → 3 penalty actions + stale actions cleared first."""
        cal_raw = {"bias": 0.50, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        result, insert_mock, clear_mock, _, _ = _run_eod_correction_mocked(
            raw_bias_mode=True,
            cal_raw=cal_raw,
            symbols=[{"symbol": "EURUSD"}],
        )
        actions = [a for a in result["actions"] if a["symbol"] == "EURUSD"]
        assert len(actions) == 3
        action_names = {a["action"] for a in actions}
        assert action_names == {"emergency_heavy_sweep", "bayesian_penalty", "kelly_reduction"}
        # Stale actions cleared unconditionally at 4h boundary BEFORE new actions written
        clear_mock.assert_awaited_once_with("EURUSD")

    def test_bayesian_shrink_uses_raw_pwin(self):
        """In raw mode, the Bayesian penalty is applied to raw p_win, not enforced."""
        cal_raw = {"bias": 0.50, "n": 30, "win_rate": 0.30,
                   "avg_pred": 0.55, "avg_pred_raw": 0.90, "status": "OVERCONFIDENT"}
        captured = {}

        async def capture_insert(table, payload):
            captured["table"] = table
            captured["payload"] = payload
            return True

        result, insert_mock, _, _, _ = _run_eod_correction_mocked(
            raw_bias_mode=True,
            cal_raw=cal_raw,
            symbols=[{"symbol": "EURUSD"}],
            insert_fn=capture_insert,
        )

        # _insert_row is called with ("nt_eod_actions", row_dict)
        # The row dict has a "detail" key containing the detail dict
        payload = captured["payload"]
        detail = payload["detail"]
        # detail["avg_pred"] should be the RAW p_win (0.90), not enforced (0.55)
        assert detail["avg_pred"] == 0.90
        # Bayesian shrink of 0.90 with n=30: (0.90*30 + 0.5*10) / 40 = 0.80
        assert detail["adjusted_p_win"] == pytest.approx(0.80, abs=1e-3)

    def test_standard_mode_uses_enforced_fetcher(self):
        """In standard (nightly) mode, fetch_calibration_bias (enforced) is used."""
        cal_enforced = {"bias": 0.18, "n": 30, "win_rate": 0.50, "avg_pred": 0.68, "status": "OVERCONFIDENT"}
        _, _, _, _, enforced_mock = _run_eod_correction_mocked(
            raw_bias_mode=False,
            cal_enforced=cal_enforced,
            symbols=[{"symbol": "EURUSD"}],
        )
        enforced_mock.assert_awaited_once()


# ─── 4. _clear_stale_eod_actions ────────────────────────────────────────

def _run_clear(symbol="EURUSD"):
    client = FakeAsyncClient()
    mock_s = AsyncMock()
    mock_s.SUPABASE_URL = "https://x.supabase.co"
    mock_s.SUPABASE_KEY = "k"
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._url",
            return_value="https://x.supabase.co/rest/v1/nt_eod_actions",
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._headers",
            return_value={"apikey": "k", "Authorization": "Bearer k"},
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
            return_value=client,
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.settings",
            mock_s,
        ),
    ):
        asyncio.run(_clear_stale_eod_actions(symbol))
    return client


class TestClearStaleEodActions:
    def test_deletes_penalty_actions_for_symbol(self):
        client = _run_clear("EURUSD")
        assert len(client.delete_calls) == 1
        params = client.delete_calls[0]["params"]
        assert params["symbol"] == "eq.EURUSD"
        assert "action" in params
        actions_str = params["action"]
        for a in ("bayesian_penalty", "kelly_reduction", "emergency_heavy_sweep"):
            assert a in actions_str

    def test_best_effort_never_raises(self):
        """A 500 from Supabase must not raise."""
        client = FakeAsyncClient()
        client.delete = AsyncMock(return_value=_FakeDeleteResp(status=500))
        mock_s = AsyncMock()
        mock_s.SUPABASE_URL = "https://x.supabase.co"
        mock_s.SUPABASE_KEY = "k"
        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._url",
                return_value="https://x.supabase.co/rest/v1/nt_eod_actions",
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._headers",
                return_value={"apikey": "k", "Authorization": "Bearer k"},
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.settings",
                mock_s,
            ),
        ):
            asyncio.run(_clear_stale_eod_actions("EURUSD"))


# ─── 5. Orchestrator wiring ─────────────────────────────────────────────

class TestOrchestratorWiring:
    def test_intraday_pass_calls_raw_mode(self):
        """_run_eod_correction_only must call run_eod_correction(raw_bias_mode=True)."""
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator

        orch = object.__new__(SweepOrchestrator)
        orch._heavy_sweep_last = {}
        correction_mock = AsyncMock(return_value={"checked": 14, "actions": [], "errors": 0})

        with (
            patch(
                "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                new=correction_mock,
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                new=AsyncMock(return_value=[]),
            ),
        ):
            asyncio.run(orch._run_eod_correction_only())

        correction_mock.assert_awaited_once()
        call_kwargs = correction_mock.call_args
        assert call_kwargs.kwargs.get("raw_bias_mode") is True

    def test_nightly_pass_calls_standard_mode(self):
        """_run_eod_lookback must call run_eod_correction without raw_bias_mode."""
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator

        orch = object.__new__(SweepOrchestrator)
        orch._heavy_sweep_last = {}
        correction_mock = AsyncMock(return_value={"checked": 14, "actions": [], "errors": 0})

        with (
            patch(
                "regime_platform.sweep_orchestrator.eod_resolver.resolve_pending_signals",
                new=AsyncMock(return_value={"resolved": 0, "by_outcome": {}, "errors": 0}),
            ),
            patch(
                "regime_platform.sweep_orchestrator.paper_book.reconcile",
                new=AsyncMock(return_value={"opened": 0, "closed": 0, "errors": 0}),
            ),
            patch(
                "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                new=correction_mock,
            ),
            patch(
                "regime_platform.sweep_orchestrator.eod_retention.run_eod_retention",
                new=AsyncMock(return_value={"deleted_resolved": 0, "expired_stale": 0, "errors": 0}),
            ),
        ):
            asyncio.run(orch._run_eod_lookback())

        correction_mock.assert_awaited_once()
        call_kwargs = correction_mock.call_args
        # Nightly pass calls without raw_bias_mode or with False
        assert not call_kwargs.kwargs.get("raw_bias_mode", False)


# ─── 6. Settings defaults ───────────────────────────────────────────────

class TestSettingsDefaults:
    def test_raw_overconfident_default_0_30(self):
        assert settings.EOD_BIAS_RAW_OVERCONFIDENT == 0.30

    def test_raw_underconfident_default_neg_0_20(self):
        assert settings.EOD_BIAS_RAW_UNDERCONFIDENT == -0.20

    def test_enforced_thresholds_unchanged(self):
        assert OVERCONFIDENT_BIAS == 0.10
        assert UNDERCONFIDENT_BIAS == -0.10


# ─── 7. Integration: raw bias vs enforced bias divergence ──────────────

class TestRawBiasCompounding:
    """The core assertion: raw bias diverges from enforced bias because
    enforcement shrinks the stored p_win, masking true model miscalibration."""

    def test_raw_bias_higher_than_enforced(self):
        """When enforcement has shrunk p_win from 0.95→0.65 and win_rate is 0.50,
        raw bias (0.95-0.50=0.45) > enforced bias (0.65-0.50=0.15).
        The enforced bias is artificially LOW because enforcement shrunk p_win."""
        rows = []
        for i in range(30):
            rows.append({
                "predicted_p_win": 0.65,       # enforced (shrunk)
                "predicted_p_win_raw": 0.95,   # raw model output
                "outcome": "win" if i < 15 else "loss",
            })

        client = FakeAsyncClient(get_rows=rows)
        with (
            patch("regime_platform.sweep_orchestrator.supabase_client._url",
                  return_value="https://x.supabase.co/rest/v1/nt_calibration_records"),
            patch("regime_platform.sweep_orchestrator.supabase_client._headers",
                  return_value={"apikey": "k", "Authorization": "Bearer k"}),
            patch("regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
                  return_value=client),
        ):
            raw_result = asyncio.run(fetch_calibration_bias_raw("EURUSD"))

        assert raw_result["bias"] == pytest.approx(0.45, abs=1e-3)
        assert raw_result["avg_pred_raw"] == pytest.approx(0.95, abs=1e-4)
        assert raw_result["avg_pred"] == pytest.approx(0.65, abs=1e-4)
        # The enforced computation would give 0.15 — this proves the masking effect.
        enforced_bias = raw_result["avg_pred"] - raw_result["win_rate"]
        assert enforced_bias == pytest.approx(0.15, abs=1e-3)
        # Raw bias is 3x the enforced bias — masking confirmed
        assert raw_result["bias"] > enforced_bias * 2
