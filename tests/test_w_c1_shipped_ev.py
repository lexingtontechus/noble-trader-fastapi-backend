"""W-C1 (2026-10-19 weaknesses evaluation, P0-1): the EV gate must price the
SHIPPED bracket, not the brick.

The defect (verified at HEAD c7568f9): ``compute_ev`` was fed the BRICK
geometry (orchestrator.py ``brick_size/sl_bricks/tp_bricks``) →
``profit = tp_bricks·brick_size``, ``stake = sl_bricks·brick_size``
(ev_engine.py Step 4). The shipped TP/SL are then overridden to
``entry ± tp_atr·ATR(14)`` and ``p_win_bracket`` is looked up by the
SHIPPED distance bucket — but ``apply_bracket_to_ev`` substituted the
ATR-bucket probability while recomputing payoff with the BRICK
profit/stake. Published EV = P(win | ATR bracket) × payoff(brick bracket),
and the ``rr ≥ 1.5`` gate tested ``tp_bricks/sl_bricks``, not the shipped
``tp_atr/sl_atr``.

The fix: ``apply_bracket_to_ev`` accepts the shipped TP/SL distances
(``tp_dist_price``/``sl_dist_price``) and rebuilds the payoff legs from
them — profit = tp_dist, stake = sl_dist — updating ``ev_result.profit``/
``stake`` so the recomputed ``rr``/``allowed`` gate the real bracket.
The brick legs remain the explicit fallback (backward compat).

Pure/deterministic — a real EVEngine + a real EVFullResult, no network.
"""
from __future__ import annotations

import pytest

from regime_platform.services.statistics.ev_engine import (
    EVEngine,
    EVFullResult,
)
from regime_platform.sweep_orchestrator.p_win_bracket import apply_bracket_to_ev


def _brick_result(engine: EVEngine, p_win: float, tp_bricks: int, sl_bricks: int, brick_size: float) -> EVFullResult:
    """A brick-geometry EVFullResult, as compute_ev builds it."""
    return engine.compute_ev(
        trend_probs=None,
        direction="long",
        p_timesfm=p_win,          # pin the blend: p_timesfm is the sole source
        tp_bricks=tp_bricks,
        sl_bricks=sl_bricks,
        brick_size=brick_size,
    )


def test_shipped_geometry_prices_the_ev_not_the_brick():
    """brick (sl 3, tp 6, brick 1.0); shipped tp_atr 4.6 / sl_atr 1.0;
    bucket p_win 0.20 → EV must be 0.20·4.6 − 0.80·1.0 = 0.12 (price units
    per 1 position unit), NOT the brick payoff 0.20·6 − 0.80·3 = −1.2."""
    eng = EVEngine(min_ev=0.0, min_rr=1.5)
    ev = _brick_result(eng, p_win=0.55, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    assert ev.profit == pytest.approx(6.0)
    assert ev.stake == pytest.approx(3.0)

    applied = apply_bracket_to_ev(
        ev,
        p_win_bracket=0.20,
        p_resolve=None,          # unscored resolve → p_expire = 0
        engine=eng,
        tp_dist_price=4.6,
        sl_dist_price=1.0,
    )
    assert applied is True
    # the shipped EV: 0.20·4.6 − 0.80·1.0 = 0.12
    assert ev.ev == pytest.approx(0.12, abs=1e-6)
    # NOT the brick EV: 0.20·6 − 0.80·3 = −1.2
    assert ev.ev != pytest.approx(-1.2)
    assert ev.ev > 0.0
    # payoff legs updated to the shipped bracket
    assert ev.profit == pytest.approx(4.6)
    assert ev.stake == pytest.approx(1.0)
    assert ev.rr == pytest.approx(4.6)
    assert ev.p_win == pytest.approx(0.20)


def test_brick_fallback_preserved_when_shipped_geometry_absent():
    """Old callers (and the brick-fallback path) pass no shipped distances —
    the payoff must stay the brick legs, byte-identical to pre-W-C1."""
    eng = EVEngine(min_ev=0.0, min_rr=1.5)
    ev = _brick_result(eng, p_win=0.55, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    applied = apply_bracket_to_ev(
        ev,
        p_win_bracket=0.20,
        p_resolve=None,
        engine=eng,
    )
    assert applied is True
    assert ev.profit == pytest.approx(6.0)
    assert ev.stake == pytest.approx(3.0)
    # brick EV: 0.20·6 − 0.80·3 = −1.2
    assert ev.ev == pytest.approx(-1.2, abs=1e-6)
    assert ev.rr == pytest.approx(2.0)


def test_degenerate_shipped_geometry_falls_back_to_brick():
    """Zero/negative shipped distances (bad prices) must not poison the EV —
    the function falls back to the brick legs."""
    eng = EVEngine(min_ev=0.0, min_rr=1.5)
    ev = _brick_result(eng, p_win=0.55, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    applied = apply_bracket_to_ev(
        ev,
        p_win_bracket=0.20,
        p_resolve=None,
        engine=eng,
        tp_dist_price=0.0,
        sl_dist_price=1.0,
    )
    assert applied is True
    assert ev.profit == pytest.approx(6.0)
    assert ev.stake == pytest.approx(3.0)


def test_rr_gate_tests_the_shipped_ratio():
    """The rr ≥ 1.5 gate must test the SHIPPED tp/sl ratio. Shipped
    tp 4.6 / sl 4.0 → rr 1.15 < 1.5 → allowed=False, even though the brick
    ratio 6/3 = 2.0 passes. (EV kept positive via a 0.60 p_win so the rr
    test is isolated: shipped EV = 0.60·4.6 − 0.40·4.0 = +1.16 > 0.)"""
    eng = EVEngine(min_ev=0.0, min_rr=1.5)

    # brick path: rr = 6/3 = 2.0 ≥ 1.5, ev = 0.60·6 − 0.40·3 = +2.4 > 0
    ev_brick = _brick_result(eng, p_win=0.55, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    apply_bracket_to_ev(
        ev_brick, p_win_bracket=0.60, p_resolve=None, engine=eng,
    )
    assert ev_brick.rr == pytest.approx(2.0)
    assert ev_brick.allowed is True, "brick ratio 2.0 passes the 1.5 gate"

    # shipped path: same engine result, shipped legs 4.6/4.0 → rr = 1.15
    ev_shipped = _brick_result(eng, p_win=0.55, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    apply_bracket_to_ev(
        ev_shipped,
        p_win_bracket=0.60,
        p_resolve=None,
        engine=eng,
        tp_dist_price=4.6,
        sl_dist_price=4.0,
    )
    assert ev_shipped.rr == pytest.approx(4.6 / 4.0)
    assert ev_shipped.ev == pytest.approx(0.60 * 4.6 - 0.40 * 4.0, abs=1e-6)
    assert ev_shipped.ev > 0.0
    assert ev_shipped.allowed is False, (
        "shipped rr 1.15 < 1.5 must reject even when the brick ratio passes"
    )


def test_expire_term_still_not_charged_as_loss_on_shipped_legs():
    """3-outcome contract on the shipped legs: p_expire=0.2 →
    EV = 0.2·4.6 − (1−0.2−0.2)·1.0 = 0.92 − 0.60 = 0.32."""
    eng = EVEngine(min_ev=0.0, min_rr=1.5)
    ev = _brick_result(eng, p_win=0.55, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    apply_bracket_to_ev(
        ev,
        p_win_bracket=0.20,
        p_resolve=0.80,          # p_expire = 0.2
        engine=eng,
        tp_dist_price=4.6,
        sl_dist_price=1.0,
    )
    assert ev.ev == pytest.approx(0.32, abs=1e-6)
