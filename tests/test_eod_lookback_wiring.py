"""
EOD lookback wiring tests (2026-08-12 DB-health build).

Covers:
  1. ``_run_eod_lookback`` invokes the new retention Part IV
     (``run_eod_retention``) after resolver/correction.
  2. The nightly resolver limit comes from ``settings.EOD_RESOLVE_LIMIT``
     (was hardcoded 200).
  3. ``NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY`` default is False and the
     capture guard skips ``log_signal_sim`` for unqualified signals when on.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.core.settings import settings


def _make_orchestrator():
    from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator

    orch = object.__new__(SweepOrchestrator)
    orch.run_heavy_sweep_for_symbol = AsyncMock(
        return_value=SimpleNamespace(skip_reason=None, error=None)
    )
    return orch


class TestEodLookbackWiring:
    def test_run_eod_lookback_calls_retention_part_iv(self):
        orch = _make_orchestrator()
        retention_mock = AsyncMock(
            return_value={"deleted_resolved": 0, "expired_stale": 0, "errors": 0}
        )

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_resolver.resolve_pending_signals",
                    new=AsyncMock(return_value={"resolved": 1, "by_outcome": {}, "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.paper_book.reconcile",
                    new=AsyncMock(return_value={"opened": 0, "closed": 0, "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": [], "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.eod_retention.run_eod_retention",
                    new=retention_mock,
                ),
            ):
                await orch._run_eod_lookback()

        asyncio.run(run())
        retention_mock.assert_awaited_once()

    def test_run_eod_lookback_passes_configured_resolve_limit(self):
        orch = _make_orchestrator()
        captured = {}

        async def fake_resolve(limit=200, **kwargs):
            captured["limit"] = limit
            return {"resolved": 0, "by_outcome": {}, "errors": 0}

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_resolver.resolve_pending_signals",
                    side_effect=fake_resolve,
                ),
                patch(
                    "regime_platform.sweep_orchestrator.paper_book.reconcile",
                    new=AsyncMock(return_value={"opened": 0, "closed": 0, "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": [], "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.eod_retention.run_eod_retention",
                    new=AsyncMock(return_value={"deleted_resolved": 0, "expired_stale": 0, "errors": 0}),
                ),
            ):
                await orch._run_eod_lookback()

        asyncio.run(run())
        assert captured.get("limit") == settings.EOD_RESOLVE_LIMIT


def _guard_should_capture(qualified: bool) -> bool:
    """Mirror of the capture-site guard (orchestrator §11a / heavy_sweep §10a):
    capture unless qualified-only mode is on AND the signal is unqualified."""
    if settings.NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY and not qualified:
        return False
    return True


class TestCaptureQualifiedOnly:
    def test_settings_default_is_false(self):
        # Default preserves current behavior: every non-neutral signal captured.
        assert settings.NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY is False

    def test_guard_skips_unqualified_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY", True)
        assert _guard_should_capture(qualified=True) is True
        assert _guard_should_capture(qualified=False) is False

    def test_guard_captures_everything_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY", False)
        assert _guard_should_capture(qualified=True) is True
        assert _guard_should_capture(qualified=False) is True


# ─── Intraday correction pass (2026-08-13 plan, B = every 4h) ─────────────


class TestSignalSimPayloadRawPWin:
    """_build_signal_sim_payload gating for predicted_p_win_raw (2026-08-13)."""

    def _payload(self, raw=None):
        from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload

        return _build_signal_sim_payload(
            symbol="EURUSD", venue="OANDA", ts="2026-08-13T12:00:00Z",
            signal="buy", entry_price=1.1000, stop_loss=1.0950, take_profit=1.1100,
            brick_size=0.001, sl_bricks=5, tp_bricks=10,
            p_win=0.62, ev=0.01, regime="trend_up", regime_conf=0.8,
            source="light", qualified=True,
            predicted_p_win_raw=raw,
        )

    def test_flag_off_omits_raw(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW", False)
        p = self._payload(raw=0.91)
        assert "predicted_p_win_raw" not in p
        assert p["p_win"] == 0.62

    def test_flag_on_includes_raw(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW", True)
        p = self._payload(raw=0.91)
        assert p["predicted_p_win_raw"] == 0.91

    def test_flag_on_but_raw_none_omits(self, monkeypatch):
        monkeypatch.setattr(settings, "NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW", True)
        p = self._payload(raw=None)
        assert "predicted_p_win_raw" not in p


class TestEodCorrectionIntraday:
    """2026-09-09 (worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md
    SS16): the cooldown moved from an in-memory ``orch._heavy_sweep_last``
    dict (emergency-sweeps-only, wiped on restart) to a durable,
    source-agnostic Supabase check (``fetch_last_full_heavy_sweep_ts``),
    applied uniformly inside ``_execute_emergency_heavy_sweeps`` for both
    the nightly and intraday callers. Tests patch that function directly
    instead of setting the (now-removed) dict.
    """

    def _orch(self):
        return _make_orchestrator()

    def _patch_sweep_sources(self, last_full_ts=None):
        return (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                new=AsyncMock(return_value=[{"symbol": "EURUSD"}, {"symbol": "BTCUSD"}]),
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.fetch_last_full_heavy_sweep_ts",
                new=AsyncMock(return_value=last_full_ts),
            ),
        )

    def test_runs_correction_and_sweeps_newly_flagged(self):
        orch = self._orch()
        actions = [
            {"symbol": "EURUSD", "action": "emergency_heavy_sweep"},
            {"symbol": "BTCUSD", "action": "bayesian_penalty"},  # not a sweep
        ]
        p1, p2 = self._patch_sweep_sources(last_full_ts=None)

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": actions, "errors": 0}),
                ),
                p1, p2,
            ):
                await orch._run_eod_correction_only()

        asyncio.run(run())
        assert orch.run_heavy_sweep_for_symbol.await_count == 1  # only EURUSD swept

    def test_no_actions_means_no_sweeps(self):
        orch = self._orch()

        async def run():
            with patch(
                "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                new=AsyncMock(return_value={"checked": 14, "actions": [], "errors": 0}),
            ):
                await orch._run_eod_correction_only()

        asyncio.run(run())
        orch.run_heavy_sweep_for_symbol.assert_not_awaited()

    def test_cooldown_skips_recently_swept_symbol(self):
        from datetime import datetime, timezone

        orch = self._orch()
        actions = [{"symbol": "EURUSD", "action": "emergency_heavy_sweep"}]
        # Last full sweep "just now" -> well within the cooldown window.
        p1, p2 = self._patch_sweep_sources(last_full_ts=datetime.now(timezone.utc))

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": actions, "errors": 0}),
                ),
                p1, p2,
            ):
                await orch._run_eod_correction_only()

        asyncio.run(run())
        orch.run_heavy_sweep_for_symbol.assert_not_awaited()

    def test_cooldown_expired_sweeps_again(self):
        from datetime import datetime, timedelta, timezone

        orch = self._orch()
        cooldown_h = float(getattr(settings, "EOD_HEAVY_SWEEP_COOLDOWN_HOURS", 12))
        actions = [{"symbol": "EURUSD", "action": "emergency_heavy_sweep"}]
        last_full_ts = datetime.now(timezone.utc) - timedelta(hours=cooldown_h, seconds=1)
        p1, p2 = self._patch_sweep_sources(last_full_ts=last_full_ts)

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": actions, "errors": 0}),
                ),
                p1, p2,
            ):
                await orch._run_eod_correction_only()

        asyncio.run(run())
        assert orch.run_heavy_sweep_for_symbol.await_count == 1

    # ─── L3 fix: degeneracy_breaker action triggers heavy sweep ──────────

    def test_degeneracy_breaker_action_triggers_sweep(self):
        """Symbols with p_win=0 and no calibration records should get emergency sweeps
        via the degeneracy_breaker action path."""
        orch = self._orch()
        actions = [
            {"symbol": "AUDUSD", "action": "degeneracy_breaker", "bias": None},
            {"symbol": "BTCUSD", "action": "bayesian_penalty"},  # not a sweep trigger
        ]

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": actions, "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                    new=AsyncMock(return_value=[{"symbol": "AUDUSD"}, {"symbol": "BTCUSD"}]),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.supabase_client.fetch_last_full_heavy_sweep_ts",
                    new=AsyncMock(return_value=None),
                ),
            ):
                await orch._run_eod_correction_only()

        asyncio.run(run())
        # Only AUDUSD should be swept (degeneracy_breaker triggers it,
        # bayesian_penalty does not)
        assert orch.run_heavy_sweep_for_symbol.await_count == 1
        # Verify the correct symbol was swept
        call_args = orch.run_heavy_sweep_for_symbol.call_args
        assert call_args[0][0].get("symbol") == "AUDUSD"

    def test_emergency_and_degeneracy_actions_both_trigger_sweeps(self):
        """Both emergency_heavy_sweep and degeneracy_breaker actions should
        trigger heavy sweeps."""
        orch = self._orch()
        actions = [
            {"symbol": "AUDUSD", "action": "degeneracy_breaker"},
            {"symbol": "EURUSD", "action": "emergency_heavy_sweep"},
            {"symbol": "BTCUSD", "action": "bayesian_penalty"},  # NOT a sweep trigger
        ]

        async def run():
            with (
                patch(
                    "regime_platform.sweep_orchestrator.eod_correction.run_eod_correction",
                    new=AsyncMock(return_value={"checked": 14, "actions": actions, "errors": 0}),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.supabase_client.fetch_last_full_heavy_sweep_ts",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
                    new=AsyncMock(return_value=[{"symbol": "AUDUSD"}, {"symbol": "EURUSD"}, {"symbol": "BTCUSD"}]),
                ),
            ):
                await orch._run_eod_correction_only()

        asyncio.run(run())
        assert orch.run_heavy_sweep_for_symbol.await_count == 2  # AUDUSD + EURUSD


class TestFetchLatestSweepColumn:
    """Regression: fetch_latest_sweep must use 'sweep_timestamp' not 'ts'.

    The nt_sweep_result table (originally sim_sweep_results) has a
    'sweep_timestamp' column — NOT 'ts'. Using 'ts.desc' as the order param
    causes a 400 Bad Request, silently defeating the L3 degeneracy_breaker
    path for symbols with no calibration records.
    """

    def test_uses_sweep_timestamp_column(self):
        """fetch_latest_sweep must order by 'sweep_timestamp', not 'ts'."""
        import inspect

        from regime_platform.sweep_orchestrator import supabase_client

        source = inspect.getsource(supabase_client.fetch_latest_sweep)
        assert "sweep_timestamp.desc" in source, (
            "fetch_latest_sweep must use order='sweep_timestamp.desc' — "
            "the nt_sweep_result table has no 'ts' column"
        )
        assert "ts.desc" not in source.replace("sweep_timestamp.desc", ""), (
            "fetch_latest_sweep must NOT use 'ts.desc' — that column does not "
            "exist on nt_sweep_result (the table uses sweep_timestamp)"
        )
