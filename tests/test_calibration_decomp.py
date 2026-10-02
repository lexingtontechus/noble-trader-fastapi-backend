"""
Tests for regime_platform/sweep_orchestrator/calibration_decomp.py.

The load-bearing assertions are mathematical identities, not snapshots:
  * ``Brier = REL − RES + UNC`` to within binning residual
  * a constant forecast has REL = 0 and RES = 0, so its Brier IS UNC
  * AUC is invariant to monotone rescaling and tie-correct
"""

from __future__ import annotations

import math

import pytest

from regime_platform.sweep_orchestrator import calibration_decomp as cd


# ─── Reference lines ──────────────────────────────────────────────────────


def test_uncertainty_is_maximal_at_half():
    assert cd.uncertainty(0.5) == pytest.approx(0.25)
    assert cd.uncertainty(0.25) == pytest.approx(0.1875)
    assert cd.uncertainty(0.0) == pytest.approx(0.0)
    assert cd.uncertainty(1.0) == pytest.approx(0.0)


def test_uncertainty_is_symmetric():
    assert cd.uncertainty(0.3) == pytest.approx(cd.uncertainty(0.7))


def test_bss_zero_when_brier_equals_uncertainty():
    assert cd.brier_skill_score(0.25, 0.5) == pytest.approx(0.0)


def test_bss_positive_when_beating_the_constant():
    assert cd.brier_skill_score(0.20, 0.5) == pytest.approx(0.2)


def test_bss_negative_when_worse_than_the_constant():
    assert cd.brier_skill_score(0.30, 0.5) == pytest.approx(-0.2)


def test_bss_none_on_missing_inputs():
    assert cd.brier_skill_score(None, 0.5) is None
    assert cd.brier_skill_score(0.2, None) is None


def test_bss_none_on_degenerate_base_rate():
    """base_rate 0 or 1 -> UNC 0 -> the ratio is undefined, not infinite."""
    assert cd.brier_skill_score(0.1, 0.0) is None
    assert cd.brier_skill_score(0.1, 1.0) is None


# ─── The constant-forecast identity (the whole point of the module) ────────


def test_constant_forecast_has_zero_rel_and_res():
    """A constant at the base rate is perfectly calibrated (REL=0) and has no
    resolution (RES=0) — so its Brier is exactly UNC. This is the reference-line
    fact the previous pass missed."""
    pairs = [(0.3, True)] * 30 + [(0.3, False)] * 70
    d = cd.decompose(pairs, n_bins=10)
    assert d["base_rate"] == pytest.approx(0.3)
    assert d["rel"] == pytest.approx(0.0, abs=1e-9)
    assert d["res"] == pytest.approx(0.0, abs=1e-9)
    assert d["unc"] == pytest.approx(0.21)
    assert d["brier"] == pytest.approx(0.21, abs=1e-9)


def test_constant_away_from_base_rate_has_rel_but_no_res():
    """A constant at the WRONG level is miscalibrated but still has no
    resolution — REL > 0, RES = 0."""
    pairs = [(0.5, True)] * 30 + [(0.5, False)] * 70
    d = cd.decompose(pairs, n_bins=10)
    assert d["rel"] == pytest.approx(0.04, abs=1e-9)   # (0.5-0.3)^2
    assert d["res"] == pytest.approx(0.0, abs=1e-9)


def test_decomposition_identity_holds():
    """Brier = REL − RES + UNC, to within the binning residual."""
    pairs = [(0.1, False)] * 40 + [(0.4, False)] * 20 + [(0.8, True)] * 40
    d = cd.decompose(pairs, n_bins=10)
    lhs = d["rel"] - d["res"] + d["unc"]
    assert lhs == pytest.approx(d["brier"], abs=0.02)
    assert abs(d["residual"]) < 0.02


def test_perfect_forecast_scores_zero():
    pairs = [(1.0, True)] * 50 + [(0.0, False)] * 50
    d = cd.decompose(pairs, n_bins=10)
    assert d["brier"] == pytest.approx(0.0, abs=1e-9)
    assert d["bss"] == pytest.approx(1.0)


def test_inverted_forecast_is_worse_than_constant():
    pairs = [(1.0, False)] * 50 + [(0.0, True)] * 50
    d = cd.decompose(pairs, n_bins=10)
    assert d["brier"] == pytest.approx(1.0)
    assert d["bss"] < 0


def test_decompose_empty():
    assert cd.decompose([]) == {"n": 0}


def test_decompose_bins_are_reported():
    pairs = [(0.05, False)] * 10 + [(0.95, True)] * 10
    d = cd.decompose(pairs, n_bins=10)
    assert len(d["bins"]) == 2
    assert d["bins"][0]["realised"] == pytest.approx(0.0)
    assert d["bins"][1]["realised"] == pytest.approx(1.0)


def test_resolution_rewards_separation():
    """A forecast that separates the classes has RES > 0 — the property the
    constant lacks. Uses exact-level forecasts (0.0 / 1.0) so each bin is
    genuinely perfectly calibrated and REL is exactly zero."""
    pairs = [(0.0, False)] * 50 + [(1.0, True)] * 50
    d = cd.decompose(pairs, n_bins=10)
    assert d["res"] > 0
    assert d["rel"] == pytest.approx(0.0, abs=1e-9)   # each bin perfectly calibrated
    assert d["brier"] == pytest.approx(0.0, abs=1e-9)


def test_miscalibrated_bin_raises_rel_without_resolution():
    """A forecast at 0.2 for outcomes that never occur is miscalibrated:
    REL picks it up. RES stays 0 because the outcome frequency does not vary."""
    pairs = [(0.2, False)] * 50
    d = cd.decompose(pairs, n_bins=10)
    assert d["rel"] == pytest.approx(0.04, abs=1e-9)   # (0.2 - 0.0)^2
    assert d["res"] == pytest.approx(0.0, abs=1e-9)


# ─── AUC ──────────────────────────────────────────────────────────────────


def test_auc_perfect_ordering():
    pairs = [(0.9, True), (0.8, True), (0.2, False), (0.1, False)]
    assert cd.auc(pairs) == pytest.approx(1.0)


def test_auc_inverted_ordering():
    pairs = [(0.1, True), (0.2, True), (0.8, False), (0.9, False)]
    assert cd.auc(pairs) == pytest.approx(0.0)


def test_auc_chance_with_all_ties():
    """All forecasts identical -> no ordering information -> 0.5."""
    pairs = [(0.5, True), (0.5, False), (0.5, True), (0.5, False)]
    assert cd.auc(pairs) == pytest.approx(0.5)


def test_auc_handles_partial_ties():
    pairs = [(0.5, True), (0.5, False), (0.9, True), (0.1, False)]
    # pairs (p>n): 0.9>0.5 ✓, 0.9>0.1 ✓, 0.5>0.1 ✓, 0.5(True) vs 0.5(False) = tie .5
    # 2 positives, 2 negatives -> 4 pairs; wins = 1 + 1 + 1 + 0.5 = 3.5 -> 0.875
    assert cd.auc(pairs) == pytest.approx(0.875)


def test_auc_invariant_to_monotone_rescaling():
    pairs = [(0.2, False), (0.4, True), (0.6, False), (0.9, True)]
    scaled = [(p ** 2, y) for p, y in pairs]
    assert cd.auc(pairs) == pytest.approx(cd.auc(scaled))


def test_auc_none_with_one_class():
    assert cd.auc([(0.5, True), (0.7, True)]) is None
    assert cd.auc([(0.5, False)]) is None
    assert cd.auc([]) is None


def test_auc_is_symmetric_under_label_flip():
    pairs = [(0.2, False), (0.4, True), (0.6, False), (0.9, True)]
    flipped = [(p, not y) for p, y in pairs]
    assert cd.auc(pairs) == pytest.approx(1.0 - cd.auc(flipped))


# ─── Reliability ──────────────────────────────────────────────────────────


def test_reliability_gap_sign():
    """Overconfident -> negative gap."""
    pairs = [(0.9, True)] * 5 + [(0.9, False)] * 5      # realised 0.5 vs pred 0.9
    tab = cd.reliability(pairs, n_bins=10)
    assert len(tab) == 1
    assert tab[0]["gap"] == pytest.approx(-0.4)


def test_reliability_omits_empty_bins():
    assert len(cd.reliability([(0.55, True)], n_bins=10)) == 1


def test_reliability_empty():
    assert cd.reliability([]) == []


def test_reliability_includes_p_equals_one():
    tab = cd.reliability([(1.0, True), (1.0, True)], n_bins=10)
    assert len(tab) == 1
    assert tab[0]["n"] == 2


# ─── score_candidate / verdict ────────────────────────────────────────────


def test_score_candidate_shape():
    pairs = [(0.2, False)] * 50 + [(0.8, True)] * 50
    c = cd.score_candidate("p_win", pairs, target="dir")
    assert c["name"] == "p_win"
    assert c["target"] == "dir"
    assert c["n"] == 100
    assert c["auc"] == pytest.approx(1.0)
    assert c["bss"] > 0


def test_score_candidate_empty():
    assert cd.score_candidate("x", [], target="t")["n"] == 0


def test_verdict_separates_axes():
    """A sharp, well-calibrated forecast reads differently from a calibrated
    but uninformative one — the two axes must not collapse into one word."""
    sharp = cd.score_candidate("s", [(0.2, False)] * 50 + [(0.8, True)] * 50, target="t")
    flat = cd.score_candidate("f", [(0.5, True)] * 50 + [(0.5, False)] * 50, target="t")
    assert "beats the constant" in cd.verdict(sharp)
    assert "strong ordering" in cd.verdict(sharp)
    assert "no better than a constant" in cd.verdict(flat)
    assert "no ordering" in cd.verdict(flat)


def test_verdict_no_data():
    assert cd.verdict({}) == "no data"
