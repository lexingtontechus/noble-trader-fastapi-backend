"""
Tests for SweepOrchestrator._smoothed_markov_hold_prob() — fix #3a
(worklog/20260822_EV_CALIBRATION_INDEPENDENT_AUDIT.md §2 Mechanism 1 / §4).

Live evidence (2026-08-22, SSH + Supabase): AAPL's HMM current_state
flipped UP -> FLAT between two consecutive 5-minute ticks with no price
move, swinging the raw P_markov_hold_N estimate from ~0.93 to ~0.0 and
p_win from ~0.85 to ~0.47. EMA-smoothing the derived probability per
symbol dampens this without needing to know whether the root cause is
current_state flipping or transition-matrix re-estimation noise.
"""

from types import SimpleNamespace

from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator


UP_ROW_MATRIX = {
    "UP": {"UP": 0.928571, "DOWN": 0.0, "FLAT": 0.071429},
    "DOWN": {"UP": 0.0, "DOWN": 0.9, "FLAT": 0.1},
    "FLAT": {"UP": 0.33, "DOWN": 0.33, "FLAT": 0.34},
}
FLAT_ROW_MATRIX = {
    "UP": {"UP": 0.928571, "DOWN": 0.0, "FLAT": 0.071429},
    "DOWN": {"UP": 0.0, "DOWN": 0.9, "FLAT": 0.1},
    "FLAT": {"UP": 0.0, "DOWN": 0.006, "FLAT": 0.994},
}


def _fake_orchestrator() -> SimpleNamespace:
    """A minimal stand-in with just the state _smoothed_markov_hold_prob
    needs, so we don't have to construct a full SweepOrchestrator (Redis,
    HMM model, price fetchers, etc.)."""
    return SimpleNamespace(_markov_ema={})


def test_first_tick_seeds_with_raw_value_unsmoothed():
    """No prior EMA state -> the first observation IS the smoothed value
    (nothing to blend against yet)."""
    fake_self = _fake_orchestrator()
    smoothed = SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", UP_ROW_MATRIX, "UP", "long", 1,
    )
    assert smoothed == fake_self._markov_ema["AAPL"]
    assert smoothed > 0.9  # matches the near-deterministic UP row


def test_state_flip_is_dampened_not_instant(monkeypatch):
    """Reproduces the live AAPL sequence: current_state UP (p_markov~0.93)
    then flips to FLAT (p_markov~0.0) with no price move. The smoothed
    value after the flip must land BETWEEN the two raw extremes, not jump
    straight to the new raw value."""
    from regime_platform.core.settings import settings
    monkeypatch.setattr(settings, "MARKOV_EMA_ALPHA", 0.3)

    fake_self = _fake_orchestrator()
    first = SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", UP_ROW_MATRIX, "UP", "long", 1,
    )
    second = SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", FLAT_ROW_MATRIX, "FLAT", "long", 1,
    )

    assert first > 0.9
    # Raw value at the FLAT tick would be ~0.0 (target UP is unreachable
    # from an absorbing-ish FLAT row) — the smoothed value must sit well
    # above that raw value, dampened by the EMA.
    assert second > 0.3
    assert second < first


def test_alpha_one_disables_smoothing_entirely(monkeypatch):
    """alpha=1.0 must reproduce the OLD (pre-fix) unsmoothed behaviour
    exactly — an escape hatch for a quick rollback without a code change."""
    from regime_platform.core.settings import settings
    monkeypatch.setattr(settings, "MARKOV_EMA_ALPHA", 1.0)

    fake_self = _fake_orchestrator()
    SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", UP_ROW_MATRIX, "UP", "long", 1,
    )
    second = SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", FLAT_ROW_MATRIX, "FLAT", "long", 1,
    )
    # With alpha=1.0, smoothed == raw every time (no memory of history).
    assert second < 0.05


def test_missing_transition_matrix_returns_none_and_does_not_poison_cache():
    fake_self = _fake_orchestrator()
    SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", UP_ROW_MATRIX, "UP", "long", 1,
    )
    result = SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", None, None, "long", 1,
    )
    assert result is None
    # Cache is left untouched (not overwritten with a bogus value) so the
    # next good tick resumes smoothing from where it left off.
    assert fake_self._markov_ema["AAPL"] > 0.9


def test_per_symbol_isolation():
    """One symbol's EMA state must not leak into another's."""
    fake_self = _fake_orchestrator()
    SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "AAPL", UP_ROW_MATRIX, "UP", "long", 1,
    )
    us500 = SweepOrchestrator._smoothed_markov_hold_prob(
        fake_self, "US500", FLAT_ROW_MATRIX, "FLAT", "short", 1,
    )
    assert set(fake_self._markov_ema.keys()) == {"AAPL", "US500"}
    assert us500 == fake_self._markov_ema["US500"]
