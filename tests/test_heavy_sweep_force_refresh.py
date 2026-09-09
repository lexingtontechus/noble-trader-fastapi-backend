"""Tests for the heavy-sweep TimesFM-confidence-gate force-refresh
override (regime_platform/sweep_orchestrator/heavy_sweep.py::
_should_force_refresh). Worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md, P0 fix.

Confirmed live before this fix: EURUSD, GBPUSD, BTCUSD, USDJPY, and
XAUAUD all sat 2+ days stale on their pre-daily-cadence,
pre-deadline-aware-scoring heavy-sweep brackets because the TimesFM
confidence gate kept skipping every single daily attempt. This tests the
pure decision function in isolation (no mocking of the full heavy-sweep
pipeline needed).
"""

from datetime import datetime, timedelta, timezone

from regime_platform.sweep_orchestrator.heavy_sweep import _should_force_refresh


def test_no_prior_sweep_always_forces_refresh():
    now = datetime.now(timezone.utc)
    force, reason = _should_force_refresh(None, now, threshold_hours=72.0)
    assert force is True
    assert "no completed heavy sweep" in reason


def test_recent_sweep_does_not_force_refresh():
    now = datetime.now(timezone.utc)
    last_full_ts = now - timedelta(hours=4)
    force, reason = _should_force_refresh(last_full_ts, now, threshold_hours=72.0)
    assert force is False
    assert reason == ""


def test_stale_sweep_past_threshold_forces_refresh():
    """Confirmed live: EURUSD's last full heavy sweep was ~56.4h stale
    (2 days 08:25) at the exact moment this bug was caught, and STILL
    climbing since the gate kept skipping it — this test uses 80h (past
    the default 72h threshold) to represent what would have happened had
    it been left running a little longer, which is exactly what this fix
    prevents from ever occurring unbounded."""
    now = datetime.now(timezone.utc)
    last_full_ts = now - timedelta(hours=80)
    force, reason = _should_force_refresh(last_full_ts, now, threshold_hours=72.0)
    assert force is True
    assert "80.0h ago" in reason
    assert "72h threshold" in reason


def test_exactly_at_threshold_does_not_force():
    """Boundary: age == threshold should NOT force (only strictly older
    than the threshold does) -- avoids off-by-one flapping right at the
    boundary on every sweep tick."""
    now = datetime.now(timezone.utc)
    last_full_ts = now - timedelta(hours=72)
    force, _ = _should_force_refresh(last_full_ts, now, threshold_hours=72.0)
    assert force is False


def test_just_past_threshold_forces():
    now = datetime.now(timezone.utc)
    last_full_ts = now - timedelta(hours=72, minutes=1)
    force, _ = _should_force_refresh(last_full_ts, now, threshold_hours=72.0)
    assert force is True


def test_custom_threshold_is_respected():
    now = datetime.now(timezone.utc)
    last_full_ts = now - timedelta(hours=10)
    # A tight custom threshold (e.g. from feature_flags.yaml being tuned
    # down) should force refresh even though the default 72h would not.
    force, _ = _should_force_refresh(last_full_ts, now, threshold_hours=8.0)
    assert force is True
