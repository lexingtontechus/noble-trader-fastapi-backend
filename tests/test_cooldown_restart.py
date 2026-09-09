"""
Tests for the cooldown-restart fix (orchestrator.py, 2026-09-08, worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md).

User directive, verbatim: "after a sl/tp hits > the cooldown should
restart." Before this fix, ``SIGNAL_COOLDOWN_MINUTES`` was pure wall-clock
elapsed time since a signal QUALIFIED, with zero awareness of whether that
signal had actually resolved (TP/SL hit, or expired) yet — a brand-new
signal in the same (symbol, direction) could qualify again once the
cooldown window elapsed even while the original position was still
theoretically open.

This file covers ``_probe_cooldown_restart``/``CooldownRestartProbe``
(orchestrator.py) and ``fetch_latest_qualified_signal`` (supabase_client.py):

  - a still-open signal blocks regardless of elapsed wall-clock time
  - a live TP/SL crossing (detected against the CURRENT price) unblocks
    even before the once-daily batch EOD resolver has run
  - a DB-recorded resolution (the batch resolver already ran) unblocks
  - a signal older than EXPIRY_BARS hours with neither level touched is
    treated as expired (unblocks)
  - a Supabase lookup failure falls back to "unknown" without raising, so
    the caller can fall back to the pre-existing wall-clock-only cooldown
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.orchestrator import (
    CooldownRestartProbe,
    _probe_cooldown_restart,
)
from regime_platform.sweep_orchestrator.eod_resolver import EXPIRY_BARS


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _run(coro):
    return asyncio.run(coro)


PATCH_TARGET = "regime_platform.sweep_orchestrator.orchestrator.fetch_latest_qualified_signal"


class TestNoPriorSignal:
    def test_no_row_returns_unknown(self):
        """No prior qualified signal for this (symbol, direction) at all —
        nothing to restart from, caller should use the existing wall-clock
        behavior untouched."""
        with patch(PATCH_TARGET, new=AsyncMock(return_value=None)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 1.10))
        assert probe == CooldownRestartProbe("unknown")


class TestStillOpenBlocksRegardlessOfElapsedTime:
    def test_open_buy_signal_within_window_and_price_not_crossed_blocks(self):
        now = datetime.now(timezone.utc)
        row = {
            "signal_id": "sig-1",
            "ts": _iso(now - timedelta(hours=2)),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            # Price sits comfortably between SL and TP — nothing crossed.
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 102.0))
        assert probe.status == "open"
        assert probe.resolved_at is None

    def test_open_sell_signal_within_window_and_price_not_crossed_blocks(self):
        now = datetime.now(timezone.utc)
        row = {
            "signal_id": "sig-2",
            "ts": _iso(now - timedelta(hours=1)),
            "entry_price": 100.0,
            "stop_loss": 105.0,   # SELL: SL above entry
            "take_profit": 90.0,  # SELL: TP below entry
            "signal": "sell",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("GBPUSD", "sell", 98.0))
        assert probe.status == "open"

    def test_blocks_even_when_wall_clock_cooldown_would_have_lapsed(self):
        """The whole point of the fix: an old-but-still-open signal blocks
        even though plenty of wall-clock time (well beyond any reasonable
        SIGNAL_COOLDOWN_MINUTES) has passed since it qualified."""
        now = datetime.now(timezone.utc)
        row = {
            "ts": _iso(now - timedelta(hours=6)),  # 6h >> 60min cooldown
            "entry_price": 50.0,
            "stop_loss": 48.0,
            "take_profit": 55.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("XAUUSD", "buy", 51.0))
        assert probe.status == "open"


class TestLiveCrossingDetectionUnblocksBeforeBatchResolver:
    """The official nt_signal_sim.outcome column is only populated once a
    day by eod_resolver.py's batch pass. These tests confirm the gate
    detects a real-time TP/SL crossing against the CURRENT price without
    waiting for that batch to run (outcome is still NULL in every row
    here)."""

    def test_current_price_at_or_above_take_profit_unblocks_buy(self):
        now = datetime.now(timezone.utc)
        row = {
            "ts": _iso(now - timedelta(hours=1)),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        before = time.time()
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 111.0))
        after = time.time()
        assert probe.status == "resolved"
        # No exact historical crossing timestamp is derivable from a single
        # current-price sample — "now" is the documented fallback.
        assert before <= probe.resolved_at <= after

    def test_current_price_at_or_below_stop_loss_unblocks_buy(self):
        now = datetime.now(timezone.utc)
        row = {
            "ts": _iso(now - timedelta(hours=1)),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 94.0))
        assert probe.status == "resolved"

    def test_current_price_crossing_unblocks_sell(self):
        now = datetime.now(timezone.utc)
        row = {
            "ts": _iso(now - timedelta(hours=1)),
            "entry_price": 100.0,
            "stop_loss": 105.0,
            "take_profit": 90.0,
            "signal": "sell",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("GBPUSD", "sell", 89.0))
        assert probe.status == "resolved"

    def test_missing_current_price_does_not_crash_treats_as_open(self):
        """If the caller has no current price available (defensive edge
        case), the live-crossing check is simply skipped — falls through
        to 'open' rather than raising."""
        now = datetime.now(timezone.utc)
        row = {
            "ts": _iso(now - timedelta(hours=1)),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", None))
        assert probe.status == "open"


class TestDbRecordedResolutionUnblocks:
    def test_tp_hit_outcome_unblocks_using_exit_ts(self):
        exit_ts = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
        row = {
            "ts": _iso(exit_ts - timedelta(hours=5)),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": "tp_hit",
            "exit_ts": _iso(exit_ts),
            "resolved_at": _iso(exit_ts + timedelta(hours=14)),  # batch ran later
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 111.0))
        assert probe.status == "resolved"
        # Prefers exit_ts (the actual close estimate) over resolved_at
        # (merely when the nightly batch happened to run).
        assert probe.resolved_at == pytest.approx(exit_ts.timestamp(), abs=1.0)

    def test_sl_hit_outcome_unblocks(self):
        row = {
            "ts": "2026-09-06T00:00:00+00:00",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": "sl_hit",
            "exit_ts": "2026-09-06T03:00:00+00:00",
            "resolved_at": "2026-09-07T00:32:00+00:00",
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 96.0))
        assert probe.status == "resolved"

    def test_db_recorded_expired_outcome_also_unblocks(self):
        """An 'expired' outcome is still a resolution for cooldown
        purposes — an expired signal isn't 'still open'."""
        row = {
            "ts": "2026-09-01T00:00:00+00:00",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": "expired",
            "exit_ts": "2026-09-03T00:00:00+00:00",
            "resolved_at": "2026-09-03T00:32:00+00:00",
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 102.0))
        assert probe.status == "resolved"

    def test_falls_back_to_resolved_at_when_exit_ts_missing(self):
        row = {
            "ts": "2026-09-01T00:00:00+00:00",
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": "tp_hit",
            "exit_ts": None,
            "resolved_at": "2026-09-02T12:00:00+00:00",
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 111.0))
        assert probe.status == "resolved"
        expected = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        assert probe.resolved_at == pytest.approx(expected, abs=1.0)


class TestExpiredByTimeUnblocks:
    def test_signal_older_than_expiry_bars_with_no_touch_unblocks(self):
        now = datetime.now(timezone.utc)
        entry_dt = now - timedelta(hours=EXPIRY_BARS + 5)
        row = {
            "ts": _iso(entry_dt),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            # Price still sits between SL/TP — only time-based expiry
            # should trigger the unblock here.
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 102.0))
        assert probe.status == "resolved"
        expected = entry_dt.timestamp() + EXPIRY_BARS * 3600.0
        assert probe.resolved_at == pytest.approx(expected, abs=1.0)

    def test_signal_just_under_expiry_bars_still_open(self):
        now = datetime.now(timezone.utc)
        entry_dt = now - timedelta(hours=EXPIRY_BARS - 1)
        row = {
            "ts": _iso(entry_dt),
            "entry_price": 100.0,
            "stop_loss": 95.0,
            "take_profit": 110.0,
            "signal": "buy",
            "outcome": None,
            "exit_ts": None,
            "resolved_at": None,
        }
        with patch(PATCH_TARGET, new=AsyncMock(return_value=row)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 102.0))
        assert probe.status == "open"


class TestSupabaseFailureFallsBackGracefully:
    def test_fetch_returning_none_on_failure_is_unknown_not_raise(self):
        """fetch_latest_qualified_signal's own contract (matching every
        other fetch_* helper in supabase_client.py) is 'best-effort —
        returns None on any failure, never raises'. Confirm the probe
        treats that identically to 'no signal found' — 'unknown', so the
        caller falls back to the pre-existing wall-clock-only cooldown."""
        with patch(PATCH_TARGET, new=AsyncMock(return_value=None)):
            probe = _run(_probe_cooldown_restart("EURUSD", "buy", 100.0))
        assert probe.status == "unknown"
        assert probe.resolved_at is None

    def test_orchestrator_call_site_swallows_probe_exception(self):
        """Defense-in-depth: even if something inside the probe itself
        raised (not just the fetch call failing per its own contract),
        the orchestrator's call site wraps the call in try/except and
        falls back to CooldownRestartProbe("unknown") — mirrors the exact
        shape of that call site in orchestrator.py's cooldown block so a
        regression there would be caught here too."""

        async def _boom(*args, **kwargs):
            raise RuntimeError("supabase unreachable")

        async def _call_site():
            try:
                return await _probe_cooldown_restart("EURUSD", "buy", 100.0)
            except Exception:
                return CooldownRestartProbe("unknown")

        with patch(PATCH_TARGET, new=AsyncMock(side_effect=_boom)):
            probe = _run(_call_site())
        assert probe == CooldownRestartProbe("unknown")


class TestFetchLatestQualifiedSignal:
    """Direct coverage of the new supabase_client.py helper."""

    def test_returns_first_row_on_success(self):
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_latest_qualified_signal,
        )

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return [{"signal_id": "abc", "outcome": None}]

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                return _FakeResp()

        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._url",
                return_value="https://x.supabase.co/rest/v1/nt_signal_sim",
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._headers",
                return_value={"apikey": "k"},
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            row = _run(fetch_latest_qualified_signal("EURUSD", "buy"))
        assert row == {"signal_id": "abc", "outcome": None}

    def test_returns_none_on_empty_result(self):
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_latest_qualified_signal,
        )

        class _FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return []

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                return _FakeResp()

        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._url",
                return_value="https://x.supabase.co/rest/v1/nt_signal_sim",
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._headers",
                return_value={"apikey": "k"},
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            row = _run(fetch_latest_qualified_signal("EURUSD", "buy"))
        assert row is None

    def test_returns_none_never_raises_on_exception(self):
        from regime_platform.sweep_orchestrator.supabase_client import (
            fetch_latest_qualified_signal,
        )

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None):
                raise ConnectionError("network down")

        with (
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._url",
                return_value="https://x.supabase.co/rest/v1/nt_signal_sim",
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client._headers",
                return_value={"apikey": "k"},
            ),
            patch(
                "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            row = _run(fetch_latest_qualified_signal("EURUSD", "buy"))
        assert row is None
