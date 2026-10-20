"""W-C4 (2026-10-19 weaknesses evaluation, HIGH): cost-aware EV — spread +
slippage + entry-lag allowance charged ONCE per trade.

The defect: live EV charged expiry as zero PnL and included NO
spread/slippage/cost anywhere (only the SHADOW PBM posterior charged
``cost_atr``). A low-reachability, high-RR bucket could price "+EV"
(expire=0, no costs) and the only blocker was ``ev >= 0``.

The fix (LIVE, per the no-more-shadow mandate):
  * ``ev_costs`` flag block — per-class round-trip cost in ATR units
    (spread + slippage + entry-lag allowance), multiplied by the current
    ATR at the compute_ev call sites;
  * ``EVEngine.compute_ev(cost_price=...)`` — EV −= cost once per trade,
    in the same units as profit/stake (the same ``net = pnl − cost``
    convention barrier.py uses). Default 0.0 = the historical cost-blind
    EV, byte-identical for every caller that does not opt in;
  * ``EVFullResult.cost_price`` stamps the charge so post-hoc recomputes
    (apply_bracket_to_ev / recompute_ev_fields) carry it forward instead
    of silently dropping it;
  * BOTH call sites (orchestrator light + heavy_sweep) pass the per-class
    cost via the shared ``orchestrator._ev_cost_price`` helper.

Drives the REAL engine + the REAL helpers — no network, no flags mocked
except where a mode is explicitly under test.
"""
from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import (
    get_ev_cost_atr_for_asset_class,
    get_ev_cost_mode,
)
from regime_platform.services.statistics.ev_engine import EVEngine
from regime_platform.sweep_orchestrator.orchestrator import _ev_cost_price
from regime_platform.sweep_orchestrator.p_win_bracket import apply_bracket_to_ev


def _engine() -> EVEngine:
    return EVEngine(min_ev=0.0, min_rr=1.5)


def _ev(engine: EVEngine, p_win: float, cost: float = 0.0):
    """Deterministic EV: p_timesfm is the blend's sole source → p_win pinned."""
    return engine.compute_ev(
        trend_probs=None,
        direction="long",
        p_timesfm=p_win,
        tp_bricks=6,
        sl_bricks=3,
        brick_size=1.0,
        cost_price=cost,
    )


# ── the flag block ships live with a measured cost table ────────────────────


def test_flag_ships_live_with_measured_cost_table():
    """User mandate: LIVE, not shadow. Retail round-trip estimates in ATR
    units; "metals" aliases the canonical "metal" asset-class key; unknown
    classes fall back to the flat 0.05 default."""
    assert get_ev_cost_mode() == "live"
    table = {
        "forex": 0.04, "metals": 0.06, "metal": 0.06, "crypto": 0.10,
        "equity": 0.06, "index": 0.05, "unknown-class": 0.05, None: 0.05,
    }
    for asset_class, expected in table.items():
        assert get_ev_cost_atr_for_asset_class(asset_class) == pytest.approx(
            expected
        ), asset_class


def test_ev_cost_price_helper_is_class_times_atr():
    """The shared helper both call sites use: cost_atr(class) × current ATR,
    in PRICE units per 1 position unit. Missing/zero ATR → 0.0 (never
    crashes on a cold symbol); unknown class → the flat default."""
    assert _ev_cost_price("forex", 2.5) == pytest.approx(0.04 * 2.5)
    assert _ev_cost_price("crypto", 100.0) == pytest.approx(0.10 * 100.0)
    assert _ev_cost_price("metals", 30.0) == pytest.approx(0.06 * 30.0)
    assert _ev_cost_price("forex", None) == 0.0
    assert _ev_cost_price("forex", 0.0) == 0.0
    assert _ev_cost_price("some-new-class", 10.0) == pytest.approx(0.05 * 10.0)


# ── the engine charges cost once per trade ─────────────────────────────────


def test_cost_reduces_ev_by_exactly_the_cost():
    """EV with cost < EV without — and the delta is EXACTLY one cost: not
    two (per leg), not outcome-weighted, not scaled by p_win."""
    eng = _engine()
    base = _ev(eng, p_win=0.55)
    charged = _ev(eng, p_win=0.55, cost=0.35)
    assert charged.ev < base.ev
    assert base.ev - charged.ev == pytest.approx(0.35, abs=1e-9)
    # the stamp rides on the result
    assert base.cost_price == pytest.approx(0.0)
    assert charged.cost_price == pytest.approx(0.35)


@pytest.mark.parametrize("p_win", [0.20, 0.50, 0.80])
def test_cost_is_once_per_trade_independent_of_outcome(p_win):
    """The charge must not move with p_win (once per trade, not per leg):
    EV(0) − EV(c) == c for every probability — including deep-ITM and
    deep-OTM brackets where a per-leg or outcome-weighted charge would
    visibly diverge."""
    eng = _engine()
    without = _ev(eng, p_win=p_win)
    with_cost = _ev(eng, p_win=p_win, cost=0.25)
    assert without.ev - with_cost.ev == pytest.approx(0.25, abs=1e-9)


def test_default_call_is_byte_identical_to_pre_w_c4():
    """cost_price defaults to 0.0 — the historical cost-blind EV, unchanged
    for every existing caller (backward compatibility): the default call and
    the explicit-zero call are byte-identical, and any positive cost is
    EXACTLY additive on top of the legacy value (no hidden renormalisation
    of the blend, no interaction with the p_win pooling)."""
    eng = _engine()
    legacy = _ev(eng, p_win=0.55)                    # default (no param)
    explicit_zero = _ev(eng, p_win=0.55, cost=0.0)   # explicit zero
    assert legacy.ev == explicit_zero.ev
    assert legacy.cost_price == 0.0
    charged = _ev(eng, p_win=0.55, cost=0.35)
    assert charged.ev == pytest.approx(legacy.ev - 0.35, abs=1e-9)
    # and the blend itself is untouched by the charge
    assert charged.p_win == legacy.p_win


def test_cost_flips_the_allowed_gate():
    """A bracket that clears ev > 0 cost-blind must fail the gate once the
    round-trip cost is priced — the W-C4 corner the evaluation named
    ("one arithmetic assumption away from reopening")."""
    eng = EVEngine(min_ev=0.0, min_rr=1.5)
    blind = _ev(eng, p_win=0.55)          # rr = 2.0, ev > 0
    assert blind.ev > 0.0 and blind.allowed is True
    cost = blind.ev + 0.5                 # pushes the SAME bracket under 0
    aware = _ev(eng, p_win=0.55, cost=cost)
    assert aware.ev == pytest.approx(-0.5, abs=1e-9)
    assert aware.allowed is False


def test_recompute_ev_fields_charges_cost_once():
    """Post-hoc recompute (the p_win-adjustment path) honours the same
    convention: cost subtracted once, after the outcome terms; default
    None keeps the historical cost-blind recompute."""
    eng = _engine()
    out = eng.recompute_ev_fields(0.30, profit=4.6, stake=1.0, p_expire=0.2)
    out_cost = eng.recompute_ev_fields(
        0.30, profit=4.6, stake=1.0, p_expire=0.2, cost_price=0.4
    )
    # 3-outcome form: 0.30·4.6 − 0.50·1.0 = 0.88; cost once → 0.48
    assert out["ev"] == pytest.approx(0.88, abs=1e-9)
    assert out_cost["ev"] == pytest.approx(0.48, abs=1e-9)
    assert out["ev"] - out_cost["ev"] == pytest.approx(0.4, abs=1e-9)


def test_apply_bracket_to_ev_carries_the_stamped_cost():
    """W-C1 + W-C4 together — the SHIPPED-bracket EV stays cost-aware after
    the bracket substitution: with the cost priced into compute_ev,
    apply_bracket_to_ev reads the result's own stamp (cost_price=None) and
    the post-substitution EV is

        p_b·tp − (1 − p_b − p_e)·sl − cost

    — never silently dropping the charge back to cost-blind."""
    eng = _engine()
    ev = _ev(eng, p_win=0.55, cost=0.35)
    assert ev.cost_price == pytest.approx(0.35)
    applied = apply_bracket_to_ev(
        ev,
        p_win_bracket=0.20,
        p_resolve=0.80,     # p_expire = 0.2
        engine=eng,
        tp_dist_price=4.6,  # W-C1 shipped legs
        sl_dist_price=1.0,
        cost_price=None,    # ← reads the result's own stamp
    )
    assert applied is True
    # 0.20·4.6 − 0.60·1.0 − 0.35 = 0.92 − 0.60 − 0.35 = −0.03
    assert ev.ev == pytest.approx(-0.03, abs=1e-6)
    assert ev.cost_price == pytest.approx(0.35)  # stamp survives


def test_apply_bracket_to_ev_explicit_cost_overrides():
    """Explicit cost_price wins over the stamp (the caller knows better —
    e.g. a re-priced ATR at substitution time)."""
    eng = _engine()
    ev = _ev(eng, p_win=0.55, cost=0.35)
    apply_bracket_to_ev(
        ev,
        p_win_bracket=0.60,
        p_resolve=None,
        engine=eng,
        tp_dist_price=4.6,
        sl_dist_price=1.0,
        cost_price=0.10,
    )
    # 0.60·4.6 − 0.40·1.0 − 0.10 = 2.76 − 0.40 − 0.10 = 2.26
    assert ev.ev == pytest.approx(2.26, abs=1e-6)


def test_brick_fallback_path_stays_cost_aware():
    """Even on the legacy brick-legs path (no shipped distances), the cost
    stamped at compute_ev survives the substitution — the historical
    fail-open corner (cost dropped on recompute) is closed everywhere."""
    eng = _engine()
    ev = _ev(eng, p_win=0.55, cost=0.35)
    apply_bracket_to_ev(
        ev, p_win_bracket=0.20, p_resolve=None, engine=eng,
    )
    # brick legs: 0.20·6 − 0.80·3 − 0.35 = 1.2 − 2.4 − 0.35 = −1.55
    assert ev.ev == pytest.approx(-1.55, abs=1e-6)
