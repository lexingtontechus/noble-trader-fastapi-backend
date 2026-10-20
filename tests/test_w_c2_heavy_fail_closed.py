"""W-C2 (2026-10-19 weaknesses evaluation, P0-2): the heavy sweep must fail
closed on unscored brackets — no directional-EV leak.

The defect (verified at HEAD c7568f9, heavy_sweep.py:1579-1604 and
2386-2402): when the reachability bucket was unscored,
``apply_bracket_to_ev`` returned False → EV stayed the DIRECTIONAL blend;
``_evaluate_heavy_qualification`` then scored that directional p_win
(≈0.55+) against the global ``min_p_win_bracket`` 0.20 — trivially
passing. No unscored fail-closed, no EV floor, global (not per-class)
floors. The daily sweep could publish as "+EV, confidence 0.6" exactly
what the 15-min path suppresses.

The fix (all mirroring the light path's live gates):
  1. Unscored bracket + ``p_win_bracket_unscored_policy: fail_closed``
     (the same flag the light path honours) → NOT qualified.
  2. Post-substitution EV below the light path's ``ev_hard_floor`` →
     NOT qualified.
  3. Per-class ``min_p_win_bracket_by_asset_class`` floor instead of the
     global 0.20.

Drives the REAL ``HeavySweepMixin._evaluate_heavy_qualification`` on a
real ``EVFullResult`` — no network, no sweep run.
"""
from __future__ import annotations

from regime_platform.core.feature_flags import (
    get_ev_hard_floor,
    get_min_p_win_bracket_for_asset_class,
    get_p_win_bracket_unscored_policy,
    get_p_win_in_ev_mode,
)
from regime_platform.services.statistics.ev_engine import EVFullResult
from regime_platform.sweep_orchestrator.heavy_sweep import HeavySweepMixin


def _heavy() -> HeavySweepMixin:
    """Minimal instance — _evaluate_heavy_qualification needs no state."""
    return object.__new__(HeavySweepMixin)


def _ev_result(p_win: float, ev: float, rr: float = 2.0, allowed: bool = True) -> EVFullResult:
    return EVFullResult(
        p_win=p_win, ev=ev, ev_per_dollar=ev, rr=rr,
        profit=rr, stake=1.0, allowed=allowed,
        p_regime=p_win, p_historical=None, p_markov=None,
        aggression="mid", ev_scale=1.0, reason="test",
    )


def _qualify(ev, p_win_bracket=None, asset_class=None, **kw):
    return _heavy()._evaluate_heavy_qualification(
        ev,
        {"signal": "buy", "symbol": "TEST"},
        regime_conf=0.70,          # above MIN_REGIME_CONF (0.60)
        kelly_f=0.05,              # above MIN_KELLY (0.02)
        p_timesfm=0.60,
        p_win_bracket=p_win_bracket,
        p_resolve=None,
        asset_class=asset_class,
        symbol="TEST",
        **kw,
    )


def test_live_config_is_the_scenario_under_test():
    """These tests pin the SHIPPED posture: bracket EV in, fail_closed
    policy on — the exact configuration where W-C2 had teeth."""
    assert get_p_win_in_ev_mode() == "live"
    assert get_p_win_bracket_unscored_policy() == "fail_closed"
    assert get_ev_hard_floor() == 0.0


def test_unscored_bracket_directional_pwin_not_qualified():
    """(a) The exact W-C2 leak: unscored bucket + directional p_win 0.55
    (trivially above the old global 0.20 floor) + positive directional EV
    → the OLD code qualified it. Now: NOT qualified."""
    ev = _ev_result(p_win=0.55, ev=+1.10)   # directional blend, all green
    assert ev.allowed is True
    assert ev.p_win > 0.20                   # would have cleared the old gate
    assert _qualify(ev, p_win_bracket=None) is False, (
        "unscored bracket must fail closed on the heavy book — directional "
        "p_win/EV cannot stand in for a scored bracket probability"
    )


def test_scored_bucket_negative_ev_not_qualified():
    """(b) Scored bucket, post-substitution EV −0.05 → NOT qualified (the
    light path's ev_hard_floor gate, now mirrored on heavy). ev.allowed is
    deliberately True here so the floor — not the allowed gate — rejects."""
    ev = _ev_result(p_win=0.30, ev=-0.05, allowed=True)
    assert ev.ev < get_ev_hard_floor()
    assert _qualify(ev, p_win_bracket=0.30, asset_class="forex") is False


def test_scored_positive_ev_above_per_class_floor_qualified():
    """(c) Scored bucket, EV +0.2, p_win 0.35 ≥ forex per-class floor 0.30
    → qualified."""
    floor = get_min_p_win_bracket_for_asset_class("forex")
    assert floor == 0.30
    ev = _ev_result(p_win=0.35, ev=+0.20)
    assert _qualify(ev, p_win_bracket=0.35, asset_class="forex") is True


def test_per_class_floor_not_global():
    """The per-class floor actually binds: crypto's floor is 0.50, so a
    p_win of 0.35 that qualifies on forex must NOT qualify on crypto."""
    ev = _ev_result(p_win=0.35, ev=+0.20)
    assert _qualify(ev, p_win_bracket=0.35, asset_class="forex") is True
    assert _qualify(ev, p_win_bracket=0.35, asset_class="crypto") is False
    assert get_min_p_win_bracket_for_asset_class("crypto") == 0.50


def test_positive_ev_below_per_class_floor_not_qualified():
    """Positive EV alone is not enough on heavy anymore: a scored bracket
    p_win below the class floor is rejected (same soft-band retirement the
    light path applied via p_win_ok)."""
    ev = _ev_result(p_win=0.25, ev=+0.10)
    assert ev.ev > 0.0
    assert _qualify(ev, p_win_bracket=0.25, asset_class="forex") is False


def test_neutral_signal_never_qualifies():
    ev = _ev_result(p_win=0.60, ev=+0.30)
    out = _heavy()._evaluate_heavy_qualification(
        ev, {"signal": "neutral"}, regime_conf=0.9, kelly_f=0.5,
        p_timesfm=None, p_win_bracket=0.60, asset_class="forex",
    )
    assert out is False


def test_legacy_callers_without_new_kwargs_reject_unscored_too():
    """Backward-compat call shape (no kwargs): under the shipped
    fail_closed policy an unscored bracket is still not qualified — the
    policy flag, not the argument, carries the fix."""
    ev = _ev_result(p_win=0.55, ev=+1.10)
    assert _heavy()._evaluate_heavy_qualification(
        ev, {"signal": "buy"}, 0.70, 0.05, 0.60,
    ) is False
