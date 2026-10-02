"""
Tests for regime_platform/sweep_orchestrator/common_factor.py.

Load-bearing assertions are the decomposition's mathematical properties:
  * a matrix with only a cell effect -> share_cell ~ 1
  * a matrix with only a window effect -> share_time ~ 1
  * pure noise -> share_resid dominates
  * the three shares sum to ~1
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator import common_factor as cf

DAY = 86_400.0
W = 3 * DAY


def _rows(cell_rates_by_window, min_n=20):
    """Build rows from ``{(cell, window): rate}`` with ``min_n`` rows each."""
    rows = []
    for (c, w), rate in cell_rates_by_window.items():
        n = min_n
        wins = round(rate * n)
        for i in range(n):
            rows.append({"_ts": w * W + i, "_cell": c, "realised": i < wins})
    return rows


# ─── build_matrix ─────────────────────────────────────────────────────────


def test_build_matrix_shapes():
    m = cf.build_matrix(_rows({("A", 0): 0.5, ("B", 0): 0.5, ("A", 1): 0.5}))
    assert ("A", 0) in m["rates"]
    assert m["cells"] == ["A", "B"]
    assert m["windows"] == [0, 1]


def test_build_matrix_respects_min_n():
    """A window with fewer than min_n rows for a cell must not enter the matrix."""
    rows = [{"_ts": i, "_cell": "A", "realised": True} for i in range(5)]
    m = cf.build_matrix(rows, min_n=20)
    assert m["rates"] == {}


def test_build_matrix_empty():
    m = cf.build_matrix([])
    assert m["rates"] == {}


def test_build_matrix_window_assignment():
    """Rows are bucketed by elapsed time from the earliest row."""
    rows = [{"_ts": 0, "_cell": "A", "realised": True}] * 25 + \
           [{"_ts": W, "_cell": "A", "realised": False}] * 25
    m = cf.build_matrix(rows, window_seconds=W, min_n=20)
    assert m["windows"] == [0, 1]


# ─── decompose_matrix ─────────────────────────────────────────────────────


def test_pure_cell_effect_loads_on_cell():
    """A high cell and a low cell, constant across windows -> cell dominates."""
    rates = {}
    for w in range(5):
        rates[("HIGH", w)] = 0.80
        rates[("LOW", w)] = 0.20
    d = cf.decompose_matrix(cf.build_matrix(_rows(rates)))
    assert d["share_cell"] == pytest.approx(1.0, abs=0.02)
    assert d["share_time"] == pytest.approx(0.0, abs=0.02)
    assert d["share_resid"] == pytest.approx(0.0, abs=0.02)


def test_pure_time_effect_loads_on_time():
    """Every cell moves together -> the common factor dominates."""
    rates = {}
    for w, level in enumerate([0.20, 0.40, 0.60, 0.80]):
        rates[("A", w)] = level
        rates[("B", w)] = level
    d = cf.decompose_matrix(cf.build_matrix(_rows(rates)))
    assert d["share_time"] == pytest.approx(1.0, abs=0.02)
    assert d["share_cell"] == pytest.approx(0.0, abs=0.02)


def test_noise_loads_on_residual():
    """Cells and windows with no consistent structure -> residual dominates."""
    import random
    rng = random.Random(11)
    rates = {(c, w): rng.uniform(0.2, 0.8)
             for c in "ABCDEF" for w in range(6)}
    d = cf.decompose_matrix(cf.build_matrix(_rows(rates)))
    assert d["share_resid"] > d["share_cell"]
    assert d["share_resid"] > d["share_time"]


def test_shares_sum_to_one():
    import random
    rng = random.Random(5)
    rates = {(c, w): rng.uniform(0.1, 0.9) for c in "ABCD" for w in range(5)}
    d = cf.decompose_matrix(cf.build_matrix(_rows(rates)))
    total = d["share_cell"] + d["share_time"] + d["share_resid"]
    assert total == pytest.approx(1.0, abs=0.01)


def test_shares_sum_to_one_on_unbalanced_matrix():
    """The real matrix is unbalanced (cells appear in different numbers of
    windows). Shares must still sum to 1 — normalised by the component sum, not
    by var_total, which would push the total above 1."""
    import random
    rng = random.Random(9)
    rates = {}
    for i, c in enumerate("ABCDE"):
        for w in range(5 - (i % 3)):        # ragged: different window counts
            rates[(c, w)] = rng.uniform(0.1, 0.9)
    d = cf.decompose_matrix(cf.build_matrix(_rows(rates)))
    total = d["share_cell"] + d["share_time"] + d["share_resid"]
    assert total == pytest.approx(1.0, abs=0.01)


def test_decompose_empty():
    assert cf.decompose_matrix({"rates": {}})["n_obs"] == 0


def test_cell_effects_ordered_descending():
    rates = {}
    for w in range(4):
        rates[("HI", w)] = 0.8
        rates[("LO", w)] = 0.2
    d = cf.decompose_matrix(cf.build_matrix(_rows(rates)))
    keys = list(d["cell_effects"].keys())
    assert keys[0] == "HI"


# ─── Verdicts ─────────────────────────────────────────────────────────────


def test_factor_verdict_common_factor():
    v = cf.factor_verdict({"n_obs": 10, "share_cell": 0.1, "share_time": 0.8,
                           "share_resid": 0.1})
    assert v.startswith("COMMON FACTOR DOMINATES")


def test_factor_verdict_cross_sectional():
    v = cf.factor_verdict({"n_obs": 10, "share_cell": 0.8, "share_time": 0.1,
                           "share_resid": 0.1})
    assert v.startswith("CROSS-SECTIONAL")


def test_factor_verdict_noise():
    v = cf.factor_verdict({"n_obs": 10, "share_cell": 0.1, "share_time": 0.1,
                           "share_resid": 0.8})
    assert v.startswith("MOSTLY NOISE")


def test_factor_verdict_mixed_and_nodata():
    assert cf.factor_verdict({"n_obs": 10, "share_cell": 0.4, "share_time": 0.3,
                              "share_resid": 0.3}).startswith("MIXED")
    assert cf.factor_verdict({}) == "no data"


# ─── Scores ───────────────────────────────────────────────────────────────


def test_raw_scores_uses_training_windows_only():
    m = cf.build_matrix(_rows({("A", 0): 1.0, ("A", 1): 0.0}))
    s = cf.raw_scores(m, [0])
    assert s["A"] == pytest.approx(1.0)


def test_residual_scores_removes_the_window_level():
    """De-factoring must separate cells that a raw average would conflate.

    A cell seen only in high windows and a cell seen only in low windows have the
    same *true* quality; the raw rate ranks them apart, the de-factored rate does not.
    """
    rates = {}
    # "LUCKY" appears only in the high window; "UNLUCKY" only in the low one.
    rates[("LUCKY", 0)] = 0.70
    rates[("UNLUCKY", 1)] = 0.30
    # both also appear in a neutral window at the same rate
    rates[("LUCKY", 2)] = 0.50
    rates[("UNLUCKY", 2)] = 0.50
    m = cf.build_matrix(_rows(rates))
    raw = cf.raw_scores(m, [0, 1, 2])
    res = cf.residual_scores(m, [0, 1, 2])
    # raw separates them (0.60 vs 0.40); de-factored brings them together
    assert raw["LUCKY"] > raw["UNLUCKY"]
    assert abs(res["LUCKY"] - res["UNLUCKY"]) < abs(raw["LUCKY"] - raw["UNLUCKY"])


def test_residual_scores_equals_raw_on_a_balanced_matrix():
    """On a perfectly balanced matrix the two coincide — that is correct math, not
    a bug: every cell sees the same window mix, so there is no period-selection to
    correct for."""
    rates = {}
    for w in range(4):
        rates[("A", w)] = 0.6 + 0.1 * w
        rates[("B", w)] = 0.4 + 0.1 * w
    m = cf.build_matrix(_rows(rates))
    raw = cf.raw_scores(m, [0, 1, 2, 3])
    res = cf.residual_scores(m, [0, 1, 2, 3])
    for c in raw:
        assert res[c] == pytest.approx(raw[c])


def test_residual_scores_differs_from_raw_on_a_ragged_matrix():
    """The real matrix is ragged, and that is exactly when de-factoring matters.

    Guards the bug where residual was computed as ``mu + a[c]`` — which IS the raw
    cell mean, making the whole comparison vacuous regardless of balance.
    """
    rates = {}
    # A appears in the high windows only; B in the low windows only; C in all.
    rates[("A", 2)] = 0.80
    rates[("A", 3)] = 0.80
    rates[("B", 0)] = 0.20
    rates[("B", 1)] = 0.20
    rates[("C", 0)] = 0.50
    rates[("C", 1)] = 0.50
    rates[("C", 2)] = 0.50
    rates[("C", 3)] = 0.50
    m = cf.build_matrix(_rows(rates))
    raw = cf.raw_scores(m, [0, 1, 2, 3])
    res = cf.residual_scores(m, [0, 1, 2, 3])
    assert raw != res
    # de-factoring shrinks the A-vs-B gap that period selection created
    assert abs(res["A"] - res["B"]) < abs(raw["A"] - raw["B"])


def test_score_matrix_filters_by_window():
    rows = _rows({("A", 0): 1.0, ("A", 1): 0.0})
    m = cf.build_matrix(rows)
    pairs = cf.score_matrix(m, rows, {"A": 0.5}, [0])
    assert len(pairs) == 20        # only window 0
    assert all(p == 0.5 for p, _ in pairs)


# ─── compare_predictions ──────────────────────────────────────────────────


def _panel(n_windows=8, cell_effect=True, factor=True, seed=2):
    """A panel with a controllable cell effect and common factor."""
    import random
    rng = random.Random(seed)
    rates = {}
    for w in range(n_windows):
        level = 0.5 + (0.05 * w if factor else 0.0)
        for c in ("A", "B", "C", "D"):
            base = level
            if cell_effect:
                base += {"A": 0.12, "B": 0.04, "C": -0.04, "D": -0.12}[c]
            rates[(c, w)] = min(0.95, max(0.05, base + rng.uniform(-0.01, 0.01)))
    return _rows(rates)


def test_compare_predictions_returns_both_aucs():
    rows = _panel()
    r = cf.compare_predictions(rows, n_folds=3)
    assert r["raw_auc"] is not None
    assert r["residual_auc"] is not None


def test_compare_predictions_needs_enough_windows():
    rows = _panel(n_windows=3)
    r = cf.compare_predictions(rows, n_folds=3)
    assert r["raw"] is None


def test_predictive_verdict_residual_wins():
    v = cf.predictive_verdict({"raw_auc": 0.52, "residual_auc": 0.60})
    assert v.startswith("RESIDUAL WINS")


def test_predictive_verdict_no_edge():
    v = cf.predictive_verdict({"raw_auc": 0.51, "residual_auc": 0.50})
    assert v.startswith("NO CROSS-SECTIONAL EDGE")


def test_predictive_verdict_no_data():
    assert cf.predictive_verdict({}) == "no data"
