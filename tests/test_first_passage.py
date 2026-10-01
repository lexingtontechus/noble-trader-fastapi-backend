"""
Tests for regime_platform/sweep_orchestrator/first_passage.py — Phase 1b of
worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md.

``first_passage`` / ``transition_carry`` / ``first_passage_score`` /
``evaluate_rows`` are pure (no I/O, no clock) — tested against hand-computed
expected values.

Key contracts under test:
  * the first-touch rule matches tiered_atr.walk() (TP first on a same-bar tie)
  * expired paths are EXCLUDED from the denominator (platform-wide rule)
  * negative-skill baseline comparison is the caller's job, but the level must
    be honest about expiry rather than silently treating it as a loss
  * the D5 carry is multiplicative (fresh ∧ dir_flip strongest) and floored > 0
  * missing evidence returns None (blend renormalises), never a fabricated 0.5
"""

from __future__ import annotations

import numpy as np
import pytest

from regime_platform.sweep_orchestrator.first_passage import (
    FP_OVERLAY_W,
    FirstPassageRead,
    evaluate_rows,
    first_passage,
    first_passage_score,
    transition_carry,
)
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS


def _paths(rows: list[list[float]]) -> np.ndarray:
    """Pad rows to EXPIRY_BARS with their last value (a flat tail)."""
    out = []
    for r in rows:
        pad = EXPIRY_BARS - len(r)
        out.append(list(r) + ([r[-1]] * pad if pad > 0 else []))
    return np.asarray(out, dtype="float64")


# ─── first_passage: the first-touch rule ─────────────────────────────────


def test_all_tp_first():
    # 20 paths that hit +2.0 immediately, never touch -1.0.
    read = first_passage(_paths([[2.0, 2.0]] * 20), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(1.0)
    assert read.expiry_share == pytest.approx(0.0)
    assert read.n_paths == 20


def test_all_sl_first():
    read = first_passage(_paths([[-1.0, -1.0]] * 20), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(0.0)
    assert read.expiry_share == pytest.approx(0.0)


def test_tp_first_on_same_bar_tie():
    """A single path touching both levels on the SAME bar counts as TP —
    identical to tiered_atr.walk()'s `ti <= si` rule."""
    # bar 0 touches +2 (tp) and -1 (sl) simultaneously.
    read = first_passage(_paths([[2.0, -1.0]] * 20), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(1.0)


def test_sl_first_when_sl_touched_earlier():
    # bar 0: -1.0 (sl), bar 1: +2.0 (tp) -> SL first.
    read = first_passage(_paths([[-1.0, 2.0]] * 20), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(0.0)


# ─── expiry handling ─────────────────────────────────────────────────────


def test_expired_paths_excluded_from_denominator():
    """10 hit TP, 10 expire. Decided = 10, so p_tp_first = 1.0 and
    expiry_share = 0.5. Expired must NOT be counted as a loss."""
    rows = [[2.0, 2.0]] * 10 + [[0.1, 0.2]] * 10
    read = first_passage(_paths(rows), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(1.0)
    assert read.expiry_share == pytest.approx(0.5)
    assert read.n_paths == 20


def test_all_expired_reports_zero_level_and_full_expiry():
    """Every path expires -> the bracket is unreachable on this evidence.
    The read is returned (not None) because the expiry share IS the finding:
    a bracket that never resolves is a geometry problem, not a missing source."""
    read = first_passage(_paths([[0.1, 0.2]] * 20), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(0.0)
    assert read.expiry_share == pytest.approx(1.0)


def test_mixed_tp_sl_expiry_shares():
    rows = [[2.0]] * 30 + [[-1.0]] * 10 + [[0.0]] * 10
    read = first_passage(_paths(rows), tp_atr=2.0, sl_atr=1.0)
    assert read is not None
    assert read.p_tp_first == pytest.approx(30 / 40)   # decided = 40
    assert read.expiry_share == pytest.approx(10 / 50)


# ─── missing / degenerate evidence ───────────────────────────────────────


def test_none_paths_returns_none():
    assert first_passage(None, tp_atr=2.0, sl_atr=1.0) is None


def test_empty_paths_returns_none():
    assert first_passage(np.empty((0, EXPIRY_BARS)), tp_atr=2.0, sl_atr=1.0) is None


def test_below_min_paths_returns_none():
    """Thin evidence must not produce a confident read — the blend
    renormalises around a missing source rather than trusting 5 samples."""
    read = first_passage(_paths([[2.0]] * 5), tp_atr=2.0, sl_atr=1.0, min_paths=20)
    assert read is None


def test_min_paths_boundary_inclusive():
    read = first_passage(_paths([[2.0]] * 20), tp_atr=2.0, sl_atr=1.0, min_paths=20)
    assert read is not None


@pytest.mark.parametrize("tp,sl", [(0.0, 1.0), (2.0, 0.0), (-1.0, 1.0), (2.0, -1.0)])
def test_nonpositive_or_negative_bracket_returns_none(tp, sl):
    assert first_passage(_paths([[2.0]] * 20), tp_atr=tp, sl_atr=sl) is None


def test_nonfinite_bracket_returns_none():
    assert first_passage(_paths([[2.0]] * 20), tp_atr=float("nan"), sl_atr=1.0) is None
    assert first_passage(_paths([[2.0]] * 20), tp_atr=float("inf"), sl_atr=1.0) is None


def test_one_dimensional_input_returns_none():
    assert first_passage(np.asarray([1.0, 2.0, 3.0]), tp_atr=2.0, sl_atr=1.0) is None


# ─── D5: transition carry ────────────────────────────────────────────────


def test_carry_fresh_dir_flip_is_ceiling():
    """The supported Kairos cell: fresh ∧ dir_flip must carry at the ceiling."""
    assert transition_carry("fresh", "dir_flip") == pytest.approx(FP_OVERLAY_W["ceiling"])


def test_carry_deep_vol_contract_is_floor():
    """deep (freshness 0.0) x vol_contract (type 0.25) -> geometric mean 0 -> floor."""
    assert transition_carry("deep", "vol_contract") == pytest.approx(FP_OVERLAY_W["floor"])


def test_carry_is_never_zero():
    """The level stays evidence even under the weakest timing prior."""
    for w in ("deep", "stale", "pre", "warm", "fresh", None):
        for t in ("dir_flip", "both", "none", "vol_expand", "vol_contract", None):
            assert transition_carry(w, t) > 0.0, (w, t)


def test_carry_is_multiplicative_not_additive():
    """The combination is GEOMETRIC, not arithmetic: a strong freshness with a
    weak type must carry strictly less than averaging the two would.

    fresh ∧ dir_flip (1.0, 1.0) and deep ∧ vol_contract (0.0, 0.25) are the
    endpoints; fresh ∧ vol_contract is the mixed cell where the two disagree,
    and that is exactly where the geometric combination bites.
    """
    floor, ceiling = FP_OVERLAY_W["floor"], FP_OVERLAY_W["ceiling"]

    def arithmetic(window_score: float, type_score_: float) -> float:
        return floor + (ceiling - floor) * (window_score + type_score_) / 2

    # fresh(1.0) x vol_contract(0.25) — the cells disagree
    mixed = transition_carry("fresh", "vol_contract")
    mixed_arithmetic = arithmetic(1.0, 0.25)
    assert mixed < mixed_arithmetic

    # when both agree the two formulas coincide (sqrt(x*x) == x)
    assert transition_carry("fresh", "dir_flip") == pytest.approx(arithmetic(1.0, 1.0))
    assert transition_carry("deep", "vol_contract") == pytest.approx(
        arithmetic(0.0, 0.25), abs=0.2
    )

    # ordering still holds
    strong = transition_carry("fresh", "dir_flip")
    weak = transition_carry("deep", "vol_contract")
    assert weak < mixed < strong


def test_carry_monotone_in_freshness():
    vals = [transition_carry(w, "dir_flip")
            for w in ("deep", "stale", "pre", "warm", "fresh")]
    assert vals == sorted(vals)


def test_carry_monotone_in_type():
    vals = [transition_carry("fresh", t)
            for t in ("vol_contract", "vol_expand", "none", "both", "dir_flip")]
    assert vals == sorted(vals)


def test_carry_bounded():
    for w in ("deep", "stale", "pre", "warm", "fresh", None, "bogus"):
        for t in ("dir_flip", "both", "none", "vol_expand", "vol_contract", None, "bogus"):
            c = transition_carry(w, t)
            assert FP_OVERLAY_W["floor"] <= c <= FP_OVERLAY_W["ceiling"]


# ─── first_passage_score: the reinforced read ────────────────────────────


def test_score_none_read_returns_none():
    assert first_passage_score(None, "fresh", "dir_flip") is None


def test_score_shrinks_toward_half_by_carry():
    read = FirstPassageRead(p_tp_first=0.9, expiry_share=0.0, n_paths=100,
                            tp_atr=2.0, sl_atr=1.0)
    carry = transition_carry("warm", "both")
    expected = 0.5 + carry * (0.9 - 0.5)
    assert first_passage_score(read, "warm", "both") == pytest.approx(expected)


def test_score_strongest_cell_preserves_most_of_level():
    read = FirstPassageRead(p_tp_first=0.9, expiry_share=0.0, n_paths=100,
                            tp_atr=2.0, sl_atr=1.0)
    strong = first_passage_score(read, "fresh", "dir_flip")
    weak = first_passage_score(read, "deep", "vol_contract")
    assert strong > weak
    # ceiling carry = 1.0 -> the level passes through untouched
    assert strong == pytest.approx(0.9)


def test_score_weakest_cell_still_leans_to_the_level():
    """At the floor the read is damped, not erased — and it must stay on the
    same side of 0.5 (a strong level stays > 0.5)."""
    read = FirstPassageRead(p_tp_first=0.9, expiry_share=0.0, n_paths=100,
                            tp_atr=2.0, sl_atr=1.0)
    weak = first_passage_score(read, "deep", "vol_contract")
    assert 0.5 < weak < 0.9


def test_score_below_half_level_stays_below():
    read = FirstPassageRead(p_tp_first=0.1, expiry_share=0.0, n_paths=100,
                            tp_atr=2.0, sl_atr=1.0)
    assert first_passage_score(read, "fresh", "dir_flip") == pytest.approx(0.1)
    assert first_passage_score(read, "deep", "vol_contract") < 0.5


def test_score_symmetric_around_half_at_extremes():
    hi = FirstPassageRead(p_tp_first=0.8, expiry_share=0.0, n_paths=100, tp_atr=2.0, sl_atr=1.0)
    lo = FirstPassageRead(p_tp_first=0.2, expiry_share=0.0, n_paths=100, tp_atr=2.0, sl_atr=1.0)
    for w, t in (("fresh", "dir_flip"), ("warm", "both"), ("deep", "vol_contract")):
        a = first_passage_score(hi, w, t)
        b = first_passage_score(lo, w, t)
        assert (a - 0.5) == pytest.approx(-(b - 0.5))


# ─── evaluate_rows: offline Phase-2 helper ───────────────────────────────


def test_evaluate_rows_scores_rows_with_paths():
    paths = _paths([[2.0]] * 30 + [[-1.0]] * 10)
    rows = [{
        "signal_id": "s1", "symbol": "XAUUSD",
        "tp_distance_atr": 2.0, "sl_distance_atr": 1.0,
        "window": "fresh", "transition_type": "dir_flip",
    }]
    out = evaluate_rows(rows, paths_by_id={"s1": paths})
    assert len(out) == 1
    r = out[0]
    assert r["signal_id"] == "s1"
    assert r["p_tp_first"] == pytest.approx(0.75)
    assert r["fp_score"] == pytest.approx(0.75)   # fresh ∧ dir_flip = full carry
    assert r["carry"] == pytest.approx(FP_OVERLAY_W["ceiling"])


def test_evaluate_rows_skips_rows_without_paths():
    """No invented priors — a row with no path evidence is skipped, not imputed."""
    rows = [{
        "signal_id": "missing", "symbol": "AAPL",
        "tp_distance_atr": 2.0, "sl_distance_atr": 1.0,
        "window": "fresh", "transition_type": "dir_flip",
    }]
    assert evaluate_rows(rows, paths_by_id={}) == []


def test_evaluate_rows_skips_rows_without_bracket():
    paths = _paths([[2.0]] * 30)
    rows = [{"signal_id": "s1", "symbol": "AAPL", "window": "fresh",
             "transition_type": "dir_flip"}]
    assert evaluate_rows(rows, paths_by_id={"s1": paths}) == []


def test_evaluate_rows_reports_level_and_overlay_separately():
    """Phase 2 must be able to measure the overlay's increment alone (bar B5)
    — so both the bare level and the reinforced score are returned."""
    paths = _paths([[2.0]] * 30 + [[-1.0]] * 10)
    rows = [{"signal_id": "s1", "symbol": "XAUUSD", "tp_distance_atr": 2.0,
             "sl_distance_atr": 1.0, "window": "deep", "transition_type": "vol_contract"}]
    r = evaluate_rows(rows, paths_by_id={"s1": paths})[0]
    assert r["p_tp_first"] == pytest.approx(0.75)     # bare level
    assert r["fp_score"] < r["p_tp_first"]            # overlay damped it
    assert r["fp_score"] > 0.5                        # but not past neutral
