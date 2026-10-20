"""W-G2/G4 (2026-10-19 weaknesses evaluation, P2→LIVE): a barrier-physics
floor on the PUBLISHED bracket geometry — the SL-farm killer.

The defect: tight-SL bands are SL-farms by physics. Computed with the
repo's OWN ``barrier.barrier_null`` (σ = 1 ATR/bar, T = 48 bars):

    XAUUSD band centre 4.94 / 1.00  → π_L = P(SL first) = 0.831
    BTCUSD band centre 8.07 / 1.77  → π_L = 0.791
    every configured band-centre has EV ≤ −cost under the driftless null

and NOTHING physics- or reachability-based constrained the published
bracket (the whole barrier machinery was shadow/off/dead — W-G4).

The fix (LIVE, per the no-more-shadow mandate): ``barrier_floor.py``
  * ``pi_first_passage`` — the closed-form gambler's-ruin twin (driftless
    ``tp/(tp+sl)``; drift via ``κ = 2μ/σ²``), cross-checked here against
    ``barrier_null`` (the repo's physics authority);
  * ``barrier_probs_mc`` — the vectorized Monte-Carlo cross-check;
  * ``apply_barrier_floor(pick, cfg)`` — stamps pi_tp/pi_sl/pi_e ALWAYS,
    adjusts WITHIN the band (widen sl to band max, then narrow tp) when
    π_SL is over cap, rejects with reason ``barrier_floor_sl_cap`` when
    unachievable; wired at the tiered_atr read-side clamp + walked pick.

These tests pin the physics numbers the evaluation measured, the
closed-form/analytic/MC agreement, and the three gate outcomes
(adjust / reject / pass-with-stamps) on real band configs.
"""
from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import (
    get_barrier_floor_mode,
    get_barrier_floor_pi_e_cap,
    get_barrier_floor_pi_sl_cap,
)
from regime_platform.sweep_orchestrator.barrier import barrier_null
from regime_platform.sweep_orchestrator.barrier_floor import (
    apply_barrier_floor,
    barrier_probs_mc,
    pi_first_passage,
)
from regime_platform.sweep_orchestrator.tiered_atr import DynamicAtrPick

# Band configs mirrored from config/feature_flags.yaml dynamic_atr.by_symbol.
FOREX_CFG = {
    "tp_atr": [4.27, 4.52, 4.77],
    "sl_atr": [2.63, 2.75, 2.87],
    "tp_atr_min": 4.02,
    "tp_atr_max": 5.02,
    "sl_atr_min": 2.51,
    "sl_atr_max": 2.99,
}
XAUUSD_CFG = {
    "tp_atr": [4.80, 4.94, 5.10],
    "sl_atr": [0.95, 1.00, 1.10],
    "tp_atr_min": 4.60,
    "tp_atr_max": 5.30,
    "sl_atr_min": 0.90,
    "sl_atr_max": 1.20,
}
LIVE_FLAGS = {"mode": "live", "pi_sl_cap": 0.62, "pi_e_cap": 0.25}


def _pick(tp: float, sl: float, n: int = 120) -> DynamicAtrPick:
    return DynamicAtrPick(
        tp_atr=tp, sl_atr=sl, exp_atr=0.10, exp_h1=0.05, exp_h2=0.15,
        expiry=0.10, n=n,
    )


# ── (a) the physics: closed form vs the repo's barrier_null ─────────────────


def test_flag_ships_live_with_measured_caps():
    """User mandate: LIVE, not shadow. 0.62 = the measured floor — the FOREX
    band centre 4.5/2.75 sits at π_L = 0.614, the last geometry family that
    clears 0.62 without starvation. pi_e_cap stays stamp+warn while the
    deadline is globally fixed at 48h (US500 π_E = 0.70 — a hard cap would
    starve the wide equity bands outright)."""
    assert get_barrier_floor_mode() == "live"
    assert get_barrier_floor_pi_sl_cap() == 0.62
    assert get_barrier_floor_pi_e_cap() == 0.25


@pytest.mark.parametrize(
    "tp,sl,pi_sl_expected",
    [(4.94, 1.00, 0.831), (8.07, 1.77, 0.791)],  # XAUUSD, BTCUSD band centres
)
def test_closed_form_matches_barrier_null_within_002(tp, sl, pi_sl_expected):
    """The evaluation's headline numbers, recomputed two independent ways:

    1. ``pi_first_passage(sl, tp, expiry_bars=48)`` — the deadline-truncated
       closed form the floor actually enforces (driftless → delegates to
       barrier_null's method-of-images);
    2. ``pi_first_passage(sl, tp)`` — the pure no-deadline gambler's ruin.

    Both must sit within 0.02 of the repo's ``barrier_null`` at T=48 (the
    physics authority) and within 0.02 of the measured π_SL (XAUUSD ≈ 0.83,
    BTCUSD ≈ 0.79). The ruin form's distance to the T=48 null IS the expiry
    mass (BTCUSD 5.9% — documented in barrier_floor.py's docstring); at the
    non-binding horizon T=480 the two must agree to <0.01 (the real
    cross-check of the images math against the ruin limit)."""
    null48 = barrier_null(tp, sl, t_bars=48)
    # headline anchors: the null and the evaluation agree
    assert null48.p_loss == pytest.approx(pi_sl_expected, abs=0.02)

    truncated = pi_first_passage(sl, tp, expiry_bars=48)
    assert truncated == pytest.approx(null48.p_loss, abs=0.02)

    ruin = pi_first_passage(sl, tp)
    assert ruin == pytest.approx(tp / (tp + sl), abs=1e-9)  # ruin form is exact
    # deadline non-binding → images converge to the ruin form
    null480 = barrier_null(tp, sl, t_bars=480)
    assert ruin == pytest.approx(null480.p_loss, abs=0.01)
    # and the shipped truncated form is within 0.02 of the authority
    assert truncated == pytest.approx(pi_sl_expected, abs=0.02)


def test_drift_moves_pi_sl_the_right_way():
    """κ = 2μ/σ²: positive (favourable) drift must LOWER P(SL first),
    negative drift must raise it — and the ruin limit must be recovered at
    κ → 0. (The spec's literal drift formula ``(1 − e^{−κa})/(1 − e^{−κ(a+b)})``
    is the complement — P(TP first); barrier_floor implements the correct
    P(SL first) leg, pinned here.)"""
    up = pi_first_passage(1.00, 4.94, drift_atr_bar=+0.02)
    flat = pi_first_passage(1.00, 4.94)
    down = pi_first_passage(1.00, 4.94, drift_atr_bar=-0.02)
    assert up < flat < down
    assert flat == pytest.approx(4.94 / 5.94, abs=1e-12)


def test_mc_agrees_with_barrier_null():
    """barrier_probs_mc (vectorized, hourly steps, seeded) vs barrier_null on
    the XAUUSD band centre. Discrete hourly monitoring misses intrabar
    touches — strongest for the tight barrier — so pi_sl reads a few points
    BELOW the continuous null (0.77 vs 0.83 here); that monitoring bias is
    documented, bounded, and direction-checked, and pi_tp+pi_sl+pi_e == 1
    exactly (the same 3-outcome contract the null uses)."""
    pi_tp, pi_sl, pi_e = barrier_probs_mc(1.00, 4.94, expiry_bars=48, n_paths=20000)
    null = barrier_null(4.94, 1.00, t_bars=48)
    assert pi_tp + pi_sl + pi_e == pytest.approx(1.0, abs=1e-9)
    assert pi_sl == pytest.approx(null.p_loss, abs=0.08)
    assert pi_tp == pytest.approx(null.p_win, abs=0.08)
    # SL-farm signature survives the coarser monitoring basis
    assert pi_sl > 3 * pi_tp
    # determinism: same seed → same numbers
    again = barrier_probs_mc(1.00, 4.94, expiry_bars=48, n_paths=20000)
    assert (pi_tp, pi_sl, pi_e) == again


# ── (b) over-cap geometry: adjusted or rejected, never shipped as-is ────────


def test_xauusd_sl_farm_pick_never_ships_as_is():
    """tp 4.94 / sl 1.00 with cap 0.62: π_SL = 0.831 — no in-band adjustment
    can reach 0.62 (sl → 1.20/1.10 grid max still leaves π_SL ≈ 0.79-0.82),
    so the pick is REJECTED (None — the existing no-pick fallback path
    handles absence). Whatever comes back, it is never the original geometry
    unstamped."""
    pick = _pick(4.94, 1.00)
    out = apply_barrier_floor(pick, XAUUSD_CFG, flags=LIVE_FLAGS, symbol="XAUUSD")
    # the general contract: adjusted (π_SL ≤ cap) or rejected (None)
    assert out is None or out.pi_sl <= LIVE_FLAGS["pi_sl_cap"]
    if out is not None:
        assert (out.tp_atr, out.sl_atr) != (4.94, 1.00)
    # and the specific verdict at the real XAUUSD band: REJECT
    assert out is None, (
        "XAUUSD 4.94/1.00 is the evaluation's SL-farm (π_L=0.831) — the "
        "band offers no in-band escape, so the floor must reject it"
    )


def test_over_cap_pick_adjusts_within_band_when_possible():
    """When the band CAN absorb it, the adjustment is minimal and in-band:
    tp 4.5 / sl 1.00 (π_SL ≈ 0.79 over the 0.62 cap) with a FOREX-width SL
    band (2.51-2.99) widens sl to the band max 2.99 → π_SL ≈ 0.59 ≤ 0.62."""
    cfg = dict(FOREX_CFG, sl_atr=[2.51, 2.75, 2.99])
    pick = _pick(4.5, 1.00)
    out = apply_barrier_floor(pick, cfg, flags=LIVE_FLAGS, symbol="TEST")
    assert out is not None, "an in-band escape exists — must adjust, not reject"
    assert out.sl_atr == pytest.approx(2.99)
    assert out.tp_atr == pytest.approx(4.5)      # SL widened first, TP untouched
    assert out.pi_sl is not None and out.pi_sl < 0.62   # re-stamped under the cap


def test_shadow_mode_stamps_but_never_adjusts():
    """shadow keeps the geometry (reported, not enforced); live is the
    shipped posture — pinned above and by the flag test."""
    pick = _pick(4.94, 1.00)
    out = apply_barrier_floor(
        pick, XAUUSD_CFG,
        flags={"mode": "shadow", "pi_sl_cap": 0.62, "pi_e_cap": 0.25},
        symbol="XAUUSD",
    )
    assert out is not None
    assert (out.tp_atr, out.sl_atr) == (4.94, 1.00)
    assert out.pi_sl == pytest.approx(0.8313, abs=0.01)  # the honest stamp


def test_off_mode_is_a_passthrough_without_stamps():
    pick = _pick(4.94, 1.00)
    out = apply_barrier_floor(pick, XAUUSD_CFG, flags={"mode": "off"}, symbol="XAUUSD")
    assert out is not None
    assert (out.tp_atr, out.sl_atr) == (4.94, 1.00)
    assert out.pi_tp is None and out.pi_sl is None and out.pi_e is None


def test_none_pick_passthrough():
    assert apply_barrier_floor(None, XAUUSD_CFG, flags=LIVE_FLAGS) is None


# ── (c) honest geometry passes with stamps ─────────────────────────────────


def test_forex_pick_passes_with_stamps():
    """FOREX band centre 4.5 / 2.75 → π_SL = 0.614 ≤ 0.62 cap: geometry
    unchanged, π stamps attached (π_TP ≈ 0.373, π_SL ≈ 0.614, π_E ≈ 0.013).
    Runs with flags=None so the SHIPPED yaml posture (live, 0.62) is the
    thing under test."""
    pick = _pick(4.5, 2.75)
    out = apply_barrier_floor(pick, FOREX_CFG, flags=None, symbol="EURUSD")
    assert out is not None
    assert (out.tp_atr, out.sl_atr) == (4.5, 2.75)   # unchanged — honest geometry
    null = barrier_null(4.5, 2.75, t_bars=48)
    assert out.pi_tp == pytest.approx(null.p_win, abs=1e-4)
    assert out.pi_sl == pytest.approx(null.p_loss, abs=1e-4)
    assert out.pi_e == pytest.approx(null.p_expire, abs=1e-4)
    assert out.pi_sl == pytest.approx(0.614, abs=0.01)
    assert out.pi_sl <= get_barrier_floor_pi_sl_cap()


def test_us500_expiry_farm_warns_but_is_not_rejected():
    """US500 band centre 13.23 / 8.09 → π_E = 0.70 > pi_e_cap 0.25, but the
    π_E cap is STAMP+WARN ONLY while settlement expiry is globally fixed at
    48h (enforcing it would starve the wide equity bands outright —
    documented starvation risk). π_SL = 0.24 is comfortably under the cap, so
    the pick passes WITH its stamps."""
    pick = _pick(13.23, 8.09)
    out = apply_barrier_floor(pick, {
        "tp_atr": [12.90, 13.23, 13.60], "sl_atr": [7.80, 8.09, 8.40],
        "tp_atr_min": 12.5, "tp_atr_max": 14.0,
        "sl_atr_min": 7.5, "sl_atr_max": 8.8,
    }, flags=LIVE_FLAGS, symbol="US500")
    assert out is not None, "pi_e_cap is warn-only by design"
    assert (out.tp_atr, out.sl_atr) == (13.23, 8.09)
    assert out.pi_e == pytest.approx(0.7009, abs=0.01)
    assert out.pi_e > LIVE_FLAGS["pi_e_cap"]


def test_stamps_survive_the_param_store_round_trip():
    """DynamicAtrPick.to_json → from_json keeps the π audit trail (and
    tolerates pre-40b rows without it)."""
    stamped = apply_barrier_floor(_pick(4.5, 2.75), FOREX_CFG, flags=None)
    assert stamped is not None and stamped.pi_sl is not None
    js = stamped.to_json()
    assert js["pi_tp"] == pytest.approx(stamped.pi_tp)
    assert js["pi_sl"] == pytest.approx(stamped.pi_sl)
    assert js["pi_e"] == pytest.approx(stamped.pi_e)
    back = DynamicAtrPick.from_json(js)
    assert back is not None
    assert (back.pi_tp, back.pi_sl, back.pi_e) == (
        pytest.approx(stamped.pi_tp), pytest.approx(stamped.pi_sl),
        pytest.approx(stamped.pi_e),
    )
    legacy = DynamicAtrPick.from_json({
        "tp_atr": 4.5, "sl_atr": 2.75, "exp_atr": 0.1, "exp_h1": 0.05,
        "exp_h2": 0.15, "expiry": 0.1, "n": 10,
    })
    assert legacy is not None
    assert legacy.pi_tp is None and legacy.pi_sl is None and legacy.pi_e is None
