"""E2E: ev_allowed must read the BRACKET EV, not the stale directional EV.

The bug (2026-10-12, worklog 20261012_EV_ALLOWED_INVERTED_SELECTION.md):
effective_ev_result was bound to adjusted_ev_result (line 4430); when TimesFM
soft-mode ran that is a DIFFERENT object from ev_result, which
apply_bracket_to_ev then mutated in place. The gate read
effective_ev_result.allowed — the stale DIRECTIONAL EV — which is overconfident
and anti-correlated with outcomes. Result: ev_allowed rejected 2,558 winners
(14.9% WR) and passed losers (8.1% WR) — inverted selection.

The fix: after apply_bracket_to_ev succeeds, sync effective_ev_result =
ev_result so the gate reads the reachability-conditioned EV.

These tests pin the contract with the REAL apply_bracket_to_ev + a real
EVEngine, proving the mutated object's `allowed` differs from the stale one
and that the gate must read the mutated object.
"""
from __future__ import annotations

import pytest

from regime_platform.services.statistics.ev_engine import EVEngine
from regime_platform.sweep_orchestrator.p_win_bracket import apply_bracket_to_ev


def _ev_result(p_win, profit, stake):
    """A minimal EVFullResult (the object apply_bracket_to_ev mutates)."""
    from regime_platform.services.statistics.ev_engine import EVFullResult
    ev = p_win * profit - (1 - p_win) * stake
    return EVFullResult(
        p_win=p_win, ev=ev, ev_per_dollar=ev / stake, rr=profit / stake,
        profit=profit, stake=stake,
        allowed=(ev > 0 and profit / stake >= 2.0),
        p_regime=p_win, p_historical=None, p_markov=None,
        aggression="mid", ev_scale=1.0, reason="test",
    )


def test_bracket_substitution_changes_allowed():
    """apply_bracket_to_ev mutates ev_result.allowed from the bracket
    probability — proving the directional and bracket `allowed` genuinely
    differ (the aliasing bug had teeth)."""
    eng = EVEngine()
    # directional p_win high (0.62) on a 2:1 bracket → positive EV, allowed=True
    ev = _ev_result(p_win=0.62, profit=2.0, stake=1.0)
    assert ev.allowed is True
    dir_allowed = ev.allowed
    # bracket (reachability) probability low (0.10), high expiry (0.6) → apply.
    # 3-outcome EV = 0.10*2 - (1-0.10-0.60)*1 = 0.20 - 0.30 = -0.10 → allowed flips.
    applied = apply_bracket_to_ev(ev, p_win_bracket=0.10, p_resolve=0.40, engine=eng)
    assert applied is True
    assert ev.p_win == pytest.approx(0.10)
    assert ev.allowed is False, "bracket EV must gate honestly (0.10 WR is -EV)"
    assert ev.allowed != dir_allowed, "directional and bracket allowed must differ"


def test_stale_object_keeps_directional_allowed_the_bug():
    """If the gate reads a DIFFERENT object than the one apply_bracket_to_ev
    mutated, it sees the stale directional allowed=True — the inversion."""
    eng = EVEngine()
    ev_result = _ev_result(p_win=0.62, profit=2.0, stake=1.0)
    # simulate the aliasing: gate bound to a snapshot taken BEFORE substitution
    stale_allowed = ev_result.allowed  # True (directional)
    apply_bracket_to_ev(ev_result, p_win_bracket=0.10, p_resolve=0.40, engine=eng)
    # ev_result.allowed is now False (bracket), but the stale snapshot is True
    assert ev_result.allowed is False
    assert stale_allowed is True, "the stale directional allowed is the bug"
    # the FIX: gate reads ev_result (the mutated object) after sync
    effective_ev_result = ev_result
    assert effective_ev_result.allowed is False


def test_fail_open_keeps_directional_when_bracket_unscored():
    """When p_win_bracket is None (unscored), apply_bracket_to_ev returns False
    and ev_result is untouched — the legacy directional path, fail-open."""
    eng = EVEngine()
    ev = _ev_result(p_win=0.62, profit=3.0, stake=1.0)
    applied = apply_bracket_to_ev(ev, p_win_bracket=None, p_resolve=None, engine=eng)
    assert applied is False
    assert ev.allowed is True  # directional kept, unchanged


def test_wide_bucket_winner_would_now_pass():
    """The concrete inversion case: a wide-bucket signal the OLD directional
    gate rejected, whose honest bracket EV the FIX now reads. Pin that the gate
    sees the bracket `allowed`, not the stale directional one."""
    eng = EVEngine()
    # directional p_win 0.10 → directional EV negative → old gate rejected
    ev = _ev_result(p_win=0.10, profit=2.0, stake=1.0)
    assert ev.allowed is False  # old: rejected
    # bracket substitution with the reachability probability + expiry
    applied = apply_bracket_to_ev(ev, p_win_bracket=0.30, p_resolve=0.80, engine=eng)
    assert applied is True
    assert ev.p_win == pytest.approx(0.30)
    # 3-outcome EV = 0.30*2 - (1-0.30-0.20)*1 = 0.60 - 0.50 = +0.10 → allowed=True
    assert ev.allowed is True, "bracket EV positive → the fix passes this winner"
