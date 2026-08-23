"""
Tests for scripts/drain_signal_backlog.py's control flow — specifically the
2026-08-23 change teaching it about the resolver's new `insufficient_bars`
outcome (worklog/20260823_CALIBRATION_BIAS_FIXES_APPLIED.md).

Before this fix, a fresh backlog (mostly signals too young to judge yet)
would trip the "3 consecutive zero-progress passes" retry-then-fail logic
and misreport a healthy, expected condition ("nothing is old enough to
resolve yet") as "no progress (errors) — stopping" with exit code 1.

Loads the script by file path (it's a standalone CLI tool, not a package)
so no scripts/__init__.py or sys.path change is needed.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "drain_signal_backlog.py"
_spec = importlib.util.spec_from_file_location("drain_signal_backlog", _SCRIPT_PATH)
drain = importlib.util.module_from_spec(_spec)
sys.modules["drain_signal_backlog"] = drain
_spec.loader.exec_module(drain)


def _run_main(argv=None):
    with patch.object(sys, "argv", ["drain_signal_backlog.py", *(argv or [])]):
        return asyncio.run(drain.main())


class TestZeroProgressDueToImmaturity:
    def test_insufficient_bars_only_stops_cleanly_no_sleep(self):
        """resolved=0, errors=0, insufficient_bars>0 -> exit 0 immediately,
        no retry sleep (nothing an immediate retry could fix)."""
        summary = {
            "resolved": 0, "by_outcome": {}, "windowed_out": 0,
            "insufficient_bars": 214, "errors": 0, "batches": 1,
        }
        with (
            patch.object(drain, "resolve_pending_signals", new=AsyncMock(return_value=summary)),
            patch.object(drain, "_count_pending", new=AsyncMock(side_effect=[214, 214])),
            patch.object(drain.asyncio, "sleep", new=AsyncMock()) as sleep_mock,
        ):
            rc = _run_main()
        assert rc == 0
        sleep_mock.assert_not_awaited()

    def test_mixed_windowed_out_and_insufficient_bars_stops_cleanly(self):
        summary = {
            "resolved": 0, "by_outcome": {}, "windowed_out": 5,
            "insufficient_bars": 100, "errors": 0, "batches": 1,
        }
        with (
            patch.object(drain, "resolve_pending_signals", new=AsyncMock(return_value=summary)),
            patch.object(drain, "_count_pending", new=AsyncMock(side_effect=[105, 105])),
        ):
            rc = _run_main()
        assert rc == 0

    def test_genuine_errors_still_retry_then_fail(self):
        """resolved=0 with real errors must still hit the existing 3-strike
        retry-then-fail path (regression guard: the immaturity fix must not
        swallow real failures)."""
        summary = {
            "resolved": 0, "by_outcome": {}, "windowed_out": 0,
            "insufficient_bars": 0, "errors": 7, "batches": 1,
        }
        with (
            patch.object(drain, "resolve_pending_signals", new=AsyncMock(return_value=summary)),
            patch.object(drain, "_count_pending", new=AsyncMock(return_value=50)),
            patch.object(drain.asyncio, "sleep", new=AsyncMock()) as sleep_mock,
        ):
            rc = _run_main()
        assert rc == 1
        assert sleep_mock.await_count == 2  # retries after strikes 1 and 2, fails on strike 3

    def test_backlog_cleared_returns_zero(self):
        summary = {
            "resolved": 10, "by_outcome": {"tp_hit": 6, "sl_hit": 4}, "windowed_out": 0,
            "insufficient_bars": 0, "errors": 0, "batches": 1,
        }
        with (
            patch.object(drain, "resolve_pending_signals", new=AsyncMock(return_value=summary)),
            patch.object(drain, "_count_pending", new=AsyncMock(side_effect=[10, 0])),
        ):
            rc = _run_main()
        assert rc == 0

    def test_nothing_pending_at_start_is_a_noop(self):
        with patch.object(drain, "_count_pending", new=AsyncMock(return_value=0)):
            rc = _run_main()
        assert rc == 0
