"""
Tests for the startup catch-up staleness check
(regime_platform/sweep_orchestrator/orchestrator.py::_is_stale), worklog/
20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md.

``_is_stale`` is a pure function (no I/O) — tested here against
hand-computed cases. The actual Supabase read
(``supabase_client.fetch_max_computed_at``) and the scheduler wiring
(``SweepOrchestrator._catch_up_stale_jobs``) are integration-level and not
covered here, matching this session's convention of unit-testing the pure
decision logic and verifying I/O-touching code via real-data runs instead.
"""

from datetime import datetime, timedelta, timezone

from regime_platform.sweep_orchestrator.orchestrator import (
    BRACKET_REACHABILITY_STALE_AFTER,
    EOD_LOOKBACK_STALE_AFTER,
    MACRO_REGIME_STALE_AFTER,
    _is_stale,
)

_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_is_stale_missing_value_is_stale():
    assert _is_stale(None, timedelta(hours=20), now=_NOW) is True


def test_is_stale_recent_value_is_not_stale():
    last = _NOW - timedelta(hours=1)
    assert _is_stale(last, timedelta(hours=20), now=_NOW) is False


def test_is_stale_exactly_at_threshold_is_not_stale():
    # Boundary: exactly the threshold age is NOT stale (strictly greater
    # than the threshold is what triggers staleness).
    last = _NOW - timedelta(hours=20)
    assert _is_stale(last, timedelta(hours=20), now=_NOW) is False


def test_is_stale_just_past_threshold_is_stale():
    last = _NOW - timedelta(hours=20, seconds=1)
    assert _is_stale(last, timedelta(hours=20), now=_NOW) is True


def test_is_stale_defaults_now_to_current_utc_time():
    # No `now` passed -> compares against the real current time. A value
    # from the far past must read as stale regardless.
    ancient = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert _is_stale(ancient, timedelta(hours=20)) is True


def test_macro_regime_stale_after_matches_documented_20_hours():
    assert MACRO_REGIME_STALE_AFTER == timedelta(hours=20)


def test_bracket_reachability_stale_after_matches_documented_6_5_days():
    assert BRACKET_REACHABILITY_STALE_AFTER == timedelta(hours=6.5 * 24)


def test_bracket_reachability_threshold_shorter_than_full_weekly_cycle():
    # The whole point is catching a miss before waiting a full 7 days.
    assert BRACKET_REACHABILITY_STALE_AFTER < timedelta(days=7)


def test_macro_regime_threshold_shorter_than_full_daily_cycle():
    assert MACRO_REGIME_STALE_AFTER < timedelta(hours=24)


def test_eod_lookback_stale_after_matches_documented_20_hours():
    assert EOD_LOOKBACK_STALE_AFTER == timedelta(hours=20)


def test_eod_lookback_threshold_shorter_than_full_daily_cycle():
    assert EOD_LOOKBACK_STALE_AFTER < timedelta(hours=24)
