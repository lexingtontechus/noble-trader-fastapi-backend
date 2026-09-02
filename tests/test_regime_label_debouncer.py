"""
Tests for RegimeLabelDebouncer (regime_platform/core/regime_engine.py),
worklog/20260901_REGIME_LABEL_STABILITY_SCOPE.md "Fix 2" — hysteresis on
RegimeSnapshot.regime_label. Pure state-machine logic, no HMM involved —
mirrors the exact online state machine validated offline (batch
hysteresis_filter) against 500 ticks of real price data before being
ported into production code.
"""

from regime_platform.core.regime_engine import RegimeLabelDebouncer


def _run(labels, n_confirm=3):
    d = RegimeLabelDebouncer(n_confirm=n_confirm)
    return [d.update(lbl) for lbl in labels]


def test_first_tick_accepted_immediately_no_lag():
    d = RegimeLabelDebouncer(n_confirm=3)
    assert d.update("bull") == "bull"


def test_single_disagreeing_tick_does_not_flip():
    out = _run(["bull", "bull", "bear", "bull", "bull"], n_confirm=3)
    assert out == ["bull", "bull", "bull", "bull", "bull"]


def test_n_consecutive_agreeing_ticks_promotes_new_label():
    # n_confirm=3: bear needs 3 in a row before it's confirmed.
    out = _run(["bull", "bear", "bear", "bear", "bear"], n_confirm=3)
    assert out == ["bull", "bull", "bull", "bear", "bear"]


def test_challenger_reset_when_it_changes_again_before_confirming():
    # bear appears twice, then flat appears once (resets the challenger
    # count), then bear resumes — needs a full fresh run of 3.
    out = _run(["bull", "bear", "bear", "flat", "bear", "bear", "bear"], n_confirm=3)
    assert out == ["bull", "bull", "bull", "bull", "bull", "bull", "bear"]


def test_n_confirm_1_behaves_like_no_debounce():
    labels = ["bull", "bear", "bull", "bear", "bear"]
    assert _run(labels, n_confirm=1) == labels


def test_n_confirm_clamped_to_at_least_1():
    d = RegimeLabelDebouncer(n_confirm=0)
    assert d.n_confirm == 1


def test_returning_to_confirmed_label_resets_pending_challenger():
    # bear gets 2 ticks (not yet enough to confirm at n=3), then bull
    # (the currently-confirmed label) reappears, then bear needs a full
    # fresh run of 3 again — the 2 earlier bear ticks don't carry over.
    out = _run(["bull", "bear", "bear", "bull", "bear", "bear", "bear"], n_confirm=3)
    assert out == ["bull", "bull", "bull", "bull", "bull", "bull", "bear"]
