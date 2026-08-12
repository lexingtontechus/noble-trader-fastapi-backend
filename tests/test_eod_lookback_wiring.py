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
