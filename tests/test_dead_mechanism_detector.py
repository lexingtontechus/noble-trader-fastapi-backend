"""Tests for the dead-mechanism detector
(regime_platform/services/self_learning/dead_mechanism_detector.py),
Phase 2 detector 2, self_learning_phase_2_detectors (RM-37).
"""

from datetime import datetime, timedelta, timezone

from pytest import approx

from regime_platform.services.self_learning.dead_mechanism_detector import (
    detect_dead_mechanism,
)

_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
_MAX_STALE = timedelta(hours=26)


def test_off_mode_never_flagged_even_with_no_writes():
    assert detect_dead_mechanism("some_mode", "off", None, _NOW, _MAX_STALE) is None


def test_off_mode_never_flagged_even_if_stale():
    old = _NOW - timedelta(days=30)
    assert detect_dead_mechanism("some_mode", "off", old, _NOW, _MAX_STALE) is None


def test_shadow_mode_never_written_is_flagged():
    result = detect_dead_mechanism("some_mode", "shadow", None, _NOW, _MAX_STALE)
    assert result is not None
    assert result["reason"] == "never_written"
    assert result["mode"] == "shadow"


def test_live_mode_stale_is_flagged():
    stale_time = _NOW - timedelta(hours=48)
    result = detect_dead_mechanism("some_mode", "live", stale_time, _NOW, _MAX_STALE)
    assert result is not None
    assert result["reason"] == "stale"
    assert result["staleness_seconds"] == approx(48 * 3600)


def test_shadow_mode_fresh_write_is_healthy():
    fresh_time = _NOW - timedelta(hours=2)
    assert detect_dead_mechanism("some_mode", "shadow", fresh_time, _NOW, _MAX_STALE) is None


def test_exactly_at_the_staleness_boundary_is_not_flagged():
    boundary_time = _NOW - _MAX_STALE
    assert detect_dead_mechanism("some_mode", "shadow", boundary_time, _NOW, _MAX_STALE) is None


def test_one_second_past_the_boundary_is_flagged():
    just_over = _NOW - _MAX_STALE - timedelta(seconds=1)
    result = detect_dead_mechanism("some_mode", "shadow", just_over, _NOW, _MAX_STALE)
    assert result is not None
