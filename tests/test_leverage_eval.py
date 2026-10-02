"""
Tests for regime_platform/sweep_orchestrator/leverage_eval.py — Track A.

Scope: docs/scopes/20261001_CALIBRATION_LEVERAGE_NEXT_STEPS.md §3.

The load-bearing test is ``test_identity_matches_the_engine``: the closed-form
withdrawal identity must reproduce ``EVEngine.compute_ev`` exactly. If that
drifts, every Track-A number is fiction — so it is asserted against the real
engine, not against a restatement of the formula.
"""

from __future__ import annotations

import math

import pytest

from regime_platform.services.statistics.ev_engine import (
    EVEngine,
    P_WIN_WEIGHTS_FULL,
    _inv_logit,
    _logit,
)
from regime_platform.sweep_orchestrator import leverage_eval as le


# ─── The identity vs the real engine ──────────────────────────────────────


@pytest.mark.parametrize("p_regime,p_markov,p_timesfm", [
    (0.60, 0.10, 0.60),
    (0.50, 0.10, 0.50),
    (0.40, 0.05, 0.40),
    (0.70, 0.20, 0.70),
    (0.55, 0.50, 0.55),
    (0.30, 0.01, 0.30),
    (0.90, 0.99, 0.90),
])
def test_identity_matches_the_engine(p_regime, p_markov, p_timesfm):
    """L_off = (L_on − w_m·logit(p_m)) / (1 − w_m) must equal the engine's own
    output when the source is simply not passed.

    Tolerance is the storage rounding, not 1e-9: ``EVFullResult.p_win`` is
    ``round(p_win, 4)``, so the identity is being applied to a rounded input.
    The exactness of the algebra is asserted separately, on unrounded values.
    """
    engine = EVEngine()
    common = dict(
        direction="long", p_timesfm=p_timesfm,
        tp_bricks=5, sl_bricks=3, brick_size=0.5,
    )
    trend = {"bull": p_regime, "bear": 0.0, "neutral": 1.0 - p_regime}

    on = engine.compute_ev(trend_probs=trend, p_markov_override=p_markov, **common)
    off = engine.compute_ev(trend_probs=trend, p_markov_override=None, **common)

    assert "p_markov_hold_n" in on.sources_used
    assert "p_markov_hold_n" not in off.sources_used

    predicted = le.p_win_without_source(on.p_win, p_markov, le.markov_weight())
    assert predicted == pytest.approx(off.p_win, abs=le._STORAGE_ROUNDING_TOL)


def test_identity_is_exact_on_unrounded_values():
    """The algebra itself is exact — no rounding involved. Constructed directly
    from the engine's own logit pool so the assertion is about the identity,
    not about storage."""
    w = le.markov_weight()
    p_regime, p_timesfm, p_markov = 0.62, 0.41, 0.13
    w_r, w_t = P_WIN_WEIGHTS_FULL["p_regime"], P_WIN_WEIGHTS_FULL["p_timesfm"]
    total = w_r + w_t + P_WIN_WEIGHTS_FULL["p_markov_hold_n"]
    L_on = (w_r / total) * _logit(p_regime) + (w / 1.0) * _logit(p_markov) \
        + (w_t / total) * _logit(p_timesfm)
    L_off_expected = ((w_r / total) * _logit(p_regime)
                      + (w_t / total) * _logit(p_timesfm)) / (1.0 - w)
    got = le.logit_without_source(L_on, p_markov, w)
    assert got == pytest.approx(L_off_expected, abs=1e-12)


def test_identity_is_exact_in_logit_space():
    """Same check one level down, so a p_win-space coincidence cannot pass."""
    engine = EVEngine()
    common = dict(direction="short", p_timesfm=0.4, tp_bricks=6, sl_bricks=3, brick_size=1.0)
    trend = {"bull": 0.2, "bear": 0.5, "neutral": 0.3}
    on = engine.compute_ev(trend_probs=trend, p_markov_override=0.15, **common)
    off = engine.compute_ev(trend_probs=trend, p_markov_override=None, **common)
    got = le.logit_without_source(_logit(on.p_win), 0.15, le.markov_weight())
    # logit amplifies the rounding near p≈0.5, hence the looser (but sized) tol
    assert got == pytest.approx(_logit(off.p_win), abs=1e-2)


def test_ev_scale_matches_the_engine():
    """The leverage formula must equal the engine's own compute_ev_scale."""
    for p in (0.2, 0.35, 0.45, 0.5, 0.6, 0.75, 0.9):
        for profit, stake in ((5.0, 3.0), (8.0, 1.0), (2.0, 4.0)):
            ev = p * profit - (1 - p) * stake
            ev_per_dollar = ev / stake
            engine_val = EVEngine.compute_ev_scale(ev_per_dollar)
            assert le.ev_scale(p, profit, stake) == pytest.approx(engine_val, abs=1e-12)


# ─── Weights ──────────────────────────────────────────────────────────────


def test_markov_weight_is_the_renormalised_5_4_5_share():
    assert le.markov_weight() == pytest.approx(4.0 / 14.0)


def test_markov_weight_matches_the_weight_table():
    w = P_WIN_WEIGHTS_FULL
    assert le.markov_weight() == pytest.approx(
        w["p_markov_hold_n"] / sum(w.values())
    )


# ─── Counterfactual direction and bounds ──────────────────────────────────


def test_low_p_markov_raises_p_win():
    """The whole point: a below-neutral source drags the pool down, so removing
    it RAISES p_win."""
    assert le.p_win_without_source(0.42, 0.10, le.markov_weight()) > 0.42


def test_high_p_markov_lowers_p_win():
    """A source above the pool's level drags it UP, so removing it lowers p_win.
    The effect is signed, not always positive."""
    assert le.p_win_without_source(0.55, 0.95, le.markov_weight()) < 0.55


def test_removing_a_neutral_source_still_renormalises():
    """A p_m = 0.5 source contributes logit(0.5) = 0 to the pool, but removing
    it is NOT inert: the surviving weights are rescaled by 1/(1−w_m), which
    pushes the remaining sources further from neutral.

    This is a real property of logit pooling and worth pinning — it means the
    withdrawal changes the blend even where the source said nothing.
    """
    w = le.markov_weight()
    p_on = 0.60
    got = le.p_win_without_source(p_on, 0.5, w)
    # L_off = L_on / (1-w), so the surviving sources get MORE extreme, not less
    assert got > p_on
    assert got == pytest.approx(_inv_logit(_logit(p_on) / (1.0 - w)), abs=1e-9)


def test_removing_a_neutral_source_leaves_a_balanced_pool_alone():
    """At exactly 0.5 the pool is already balanced, so renormalisation has
    nothing to amplify."""
    assert le.p_win_without_source(0.5, 0.5, le.markov_weight()) == pytest.approx(0.5, abs=1e-9)


def test_result_is_a_valid_probability():
    for p_on in (0.01, 0.2, 0.5, 0.8, 0.99):
        for p_m in (0.0, 0.001, 0.5, 0.999, 1.0):
            got = le.p_win_without_source(p_on, p_m, le.markov_weight())
            assert 0.0 <= got <= 1.0


def test_degenerate_weight_is_a_noop():
    assert le.logit_without_source(0.3, 0.1, 0.0) == pytest.approx(0.3)
    assert le.logit_without_source(0.3, 0.1, 1.0) == pytest.approx(0.3)


# ─── Leverage ─────────────────────────────────────────────────────────────


def test_zero_to_size_is_detected():
    """The risk case from the scope: a signal that was unsized becomes sized."""
    # Find the boundary empirically rather than assume one: a p_win just below
    # the RR 5:3 break-even, with a low p_markov, must cross it.
    breakeven = 3.0 / (5.0 + 3.0)     # 0.375 — ev = 0 exactly here
    p_on = breakeven - 0.01
    p_off = le.p_win_without_source(p_on, 0.02, le.markov_weight())
    s_on = le.ev_scale(p_on, 5.0, 3.0)
    s_off = le.ev_scale(p_off, 5.0, 3.0)
    assert s_on == 0.0, f"expected unsized at {p_on}"
    assert s_off > 0.0, f"expected sized at {p_off}"
    assert p_off > p_on


def test_scale_is_capped_at_one():
    assert le.ev_scale(0.99, 10.0, 1.0) == pytest.approx(1.0)


def test_scale_is_zero_on_nonpositive_stake():
    assert le.ev_scale(0.9, 5.0, 0.0) == 0.0


def test_scale_is_zero_below_break_even():
    # RR 1:1, p=0.4 -> ev negative -> scale 0
    assert le.ev_scale(0.4, 1.0, 1.0) == 0.0


# ─── Calibration ──────────────────────────────────────────────────────────


def test_brier_definition():
    assert le.brier(0.9, True) == pytest.approx(0.01)
    assert le.brier(0.9, False) == pytest.approx(0.81)
    assert le.brier(0.5, True) == pytest.approx(0.25)


def test_brier_none_is_not_imputed():
    """A missing prediction must stay missing — a fabricated 0.5 would flatter
    the score."""
    assert le.brier(None, True) is None


def test_reliability_table_bins_and_rates():
    pairs = [(0.05, False)] * 10 + [(0.95, True)] * 10
    table = le.reliability_table(pairs, n_bins=10)
    assert len(table) == 2
    assert table[0]["n"] == 10
    assert table[0]["realised"] == pytest.approx(0.0)
    assert table[1]["realised"] == pytest.approx(1.0)


def test_reliability_table_omits_empty_bins():
    table = le.reliability_table([(0.5, True)], n_bins=10)
    assert len(table) == 1     # only the 0.5-0.6 bin, not ten rows of zeros


def test_reliability_table_handles_p_equals_one():
    table = le.reliability_table([(1.0, True), (1.0, True)], n_bins=10)
    assert len(table) == 1
    assert table[0]["n"] == 2


def test_reliability_table_empty_input():
    assert le.reliability_table([]) == []


# ─── build_rows / summarize ───────────────────────────────────────────────


def _j(symbol="XAUUSD", p_win_on=0.42, p_m=0.10, realised=True,
       tp=5.0, sl=3.0, brick=0.5):
    return {"symbol": symbol, "p_win_on": p_win_on, "p_m": p_m,
            "realised": realised, "tp_bricks": tp, "sl_bricks": sl,
            "brick_size": brick}


def test_build_rows_replays_each_row():
    rows = le.build_rows([_j()])
    assert len(rows) == 1
    r = rows[0]
    assert r.p_win_off > r.p_win_on          # below-neutral source removed
    assert r.d_p_win == pytest.approx(r.p_win_off - r.p_win_on)


def test_build_rows_skips_missing_fields():
    assert le.build_rows([{"symbol": "X"}]) == []


def test_build_rows_skips_degenerate_probabilities():
    assert le.build_rows([_j(p_win_on=0.0)]) == []
    assert le.build_rows([_j(p_win_on=1.0)]) == []
    assert le.build_rows([_j(p_m=-0.1)]) == []


def test_build_rows_skips_nonpositive_geometry():
    assert le.build_rows([_j(tp=0.0)]) == []
    assert le.build_rows([_j(brick=0.0)]) == []


def test_summarize_counts_the_transitions():
    # RR 5:3 -> break-even p_win = 3/8 = 0.375.
    # row 1: 0.36 (unsized) -> ~0.68 (sized)  = zero-to-size
    # row 2: 0.70 (already full size) -> stays full size
    rows = le.build_rows([
        _j(p_win_on=0.36, p_m=0.02),
        _j(p_win_on=0.70, p_m=0.10),
    ])
    s = le.summarize(rows)
    assert s["n"] == 2
    assert s["n_zero_to_size"] == 1
    assert s["pct_zero_to_size"] == pytest.approx(50.0)
    assert s["n_unsized_on"] == 1


def test_summarize_empty():
    assert le.summarize([]) == {"n": 0}


def test_summarize_brier_delta_sign():
    """Removing a below-neutral source raises p_win; on a population that is
    mostly losing, that must WORSEN Brier. Sign asserted so the metric cannot
    silently invert."""
    rows = le.build_rows([_j(p_win_on=0.40, p_m=0.05, realised=False)] * 20)
    s = le.summarize(rows)
    assert s["brier_off"] > s["brier_on"]
    assert s["brier_delta"] > 0


def test_by_symbol_sorted_by_leverage():
    rows = le.build_rows([
        _j(symbol="A", p_win_on=0.40, p_m=0.01),   # big lift
        _j(symbol="B", p_win_on=0.55, p_m=0.50),   # ~none
    ])
    out = le.by_symbol(rows)
    assert [d["symbol"] for d in out] == ["A", "B"]
    assert out[0]["mean_d_scale"] > out[1]["mean_d_scale"]


# ─── Verdict ──────────────────────────────────────────────────────────────


def test_verdict_flags_risk_quadrant():
    """Worse calibration + higher size is the dangerous quadrant."""
    v = le.leverage_verdict({"n": 100, "brier_delta": 0.02, "mean_d_scale": 0.15,
                             "pct_zero_to_size": 12.0})
    assert v["calibration_better"] is False
    assert v["size_rose"] is True
    assert "RISK" in v["read"]


def test_verdict_flags_benign_quadrant():
    v = le.leverage_verdict({"n": 100, "brier_delta": -0.02, "mean_d_scale": 0.15,
                             "pct_zero_to_size": 12.0})
    assert "BENIGN" in v["read"]


def test_verdict_clean_and_inert():
    assert "CLEAN" in le.leverage_verdict(
        {"n": 10, "brier_delta": -0.01, "mean_d_scale": -0.01, "pct_zero_to_size": 0.0})["read"]
    assert "INERT" in le.leverage_verdict(
        {"n": 10, "brier_delta": 0.01, "mean_d_scale": -0.01, "pct_zero_to_size": 0.0})["read"]


def test_verdict_no_data():
    assert le.leverage_verdict({})["read"] == "no data"
