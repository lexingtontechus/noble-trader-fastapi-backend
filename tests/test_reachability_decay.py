"""
Tests for regime_platform/sweep_orchestrator/reachability_decay.py.

Load-bearing assertions:
  * a trailing window never sees rows at or after its test block (no leakage)
  * ``window_days=None`` reproduces the expanding baseline exactly
  * a SHORTER window yields a SMALLER training set (the window actually binds)
  * stability metrics behave correctly on a hand-built stable vs unstable series
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator import reachability_decay as rd
from regime_platform.sweep_orchestrator import cell_conditioning as cc

DAY = rd.DAY


def _row(ts, symbol="EURUSD", tp=101.0, realised=True, regime="med_low_vol_strong_bull"):
    r = {"symbol": symbol, "regime": regime, "signal": "buy", "atr": 1.0,
         "entry_price": 100.0, "take_profit": tp, "realised": realised,
         "p_win": 0.6, "_ts": ts}
    r["_cell"] = cc.cell_key(r)
    return r


# ─── Trailing splits ──────────────────────────────────────────────────────


def test_trailing_splits_are_time_ordered():
    rows = [_row(ts=i * DAY) for i in range(100)]
    for train, test in rd.trailing_splits(rows, n_folds=5, window_days=10.0):
        if not train:
            continue
        assert max(r["_ts"] for r in train) < min(r["_ts"] for r in test)


def test_trailing_window_actually_binds():
    """A shorter window must produce a strictly smaller training set."""
    rows = [_row(ts=i * DAY) for i in range(200)]
    wide = rd.trailing_splits(rows, n_folds=4, window_days=100.0)
    narrow = rd.trailing_splits(rows, n_folds=4, window_days=10.0)
    for (tw, _), (tn, _) in zip(wide, narrow):
        assert len(tn) < len(tw)


def test_expanding_window_is_all_history():
    rows = [_row(ts=i * DAY) for i in range(100)]
    for train, test in rd.trailing_splits(rows, n_folds=5, window_days=None):
        # train is everything before the test block
        assert max(r["_ts"] for r in train) < min(r["_ts"] for r in test)
        assert train == rows[:len(train)]


def test_trailing_splits_empty_when_too_few():
    assert rd.trailing_splits([_row(ts=0)], n_folds=5) == []


def test_test_blocks_tile_the_post_warmup_range():
    rows = [_row(ts=i * DAY) for i in range(100)]
    splits = rd.trailing_splits(rows, n_folds=5, window_days=10.0)
    seen = []
    for _, test in splits:
        seen.extend(r["_ts"] for r in test)
    assert sorted(seen) == [i * DAY for i in range(50, 100)]


# ─── Evaluate trailing ────────────────────────────────────────────────────


def _synthetic(n=400, seed=3, drift=False):
    """Tight brackets win more than wide ones. With ``drift``, the relationship
    INVERTS halfway through — the non-stationarity this module is built to find."""
    import random
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        tight = rng.random() < 0.5
        tp = 100.4 if tight else 103.0
        p_tp = 0.75 if tight else 0.25
        if drift and i > n // 2:
            p_tp = 1.0 - p_tp          # inverted in the second half
        rows.append(_row(ts=i * DAY, tp=tp, realised=rng.random() < p_tp))
    return rows


def test_evaluate_trailing_reports_window():
    r = rd.evaluate_trailing(_synthetic(), cc.SPECS["cell"], window_days=10.0)
    assert r["window_days"] == 10.0
    assert r["n"] > 0
    assert "auc" in r


def test_evaluate_trailing_expanding_matches_baseline():
    """window_days=None must reproduce cell_conditioning's expanding result."""
    rows = _synthetic()
    a = rd.evaluate_trailing(rows, cc.SPECS["cell"], window_days=None, n_folds=5)
    b = cc.evaluate(rows, cc.SPECS["cell"], n_folds=5)
    assert a["n"] == b["n"]
    assert a["auc"] == pytest.approx(b["auc"], abs=1e-9)


def test_window_sweep_returns_one_row_per_window():
    rows = _synthetic()
    out = rd.window_sweep(rows, cc.SPECS["cell"], windows=(7.0, 14.0, None), n_folds=4)
    assert [r["window_days"] for r in out] == [7.0, 14.0, None]


def test_best_window_picks_the_max():
    results = [{"window_days": 7.0, "auc": 0.51},
               {"window_days": 14.0, "auc": 0.58},
               {"window_days": None, "auc": 0.50}]
    assert rd.best_window(results)["window_days"] == 14.0


def test_best_window_none_when_unscored():
    assert rd.best_window([{"window_days": 7.0, "auc": None}]) is None
    assert rd.best_window([]) is None


# ─── Recency verdict ──────────────────────────────────────────────────────


def test_recency_verdict_helps():
    results = [{"window_days": 14.0, "auc": 0.60}, {"window_days": None, "auc": 0.50}]
    assert rd.recency_verdict(results).startswith("RECENCY HELPS")


def test_recency_verdict_does_not_fix():
    results = [{"window_days": 14.0, "auc": 0.52}, {"window_days": None, "auc": 0.50}]
    assert rd.recency_verdict(results).startswith("RECENCY DOES NOT FIX IT")


def test_recency_verdict_no_data():
    assert rd.recency_verdict([]) == "no data"
    assert rd.recency_verdict([{"window_days": None, "auc": 0.5}]) == "no data"


# ─── Era rates + stability ────────────────────────────────────────────────


def test_era_rates_split_into_equal_time_chunks():
    rows = [_row(ts=i * DAY) for i in range(100)]
    rates, counts = rd.era_rates(rows, n_eras=4, min_n=1)
    assert len(rates) == 4
    assert len(counts) == 4


def test_era_rates_respect_min_n():
    rows = [_row(ts=i * DAY) for i in range(20)]
    rates, _ = rd.era_rates(rows, n_eras=4, min_n=100)   # impossibly high
    assert all(not r for r in rates)


def test_stability_stable_series_scores_low_drift():
    """A cell whose rate is identical in every era has zero drift.

    Three cells (the correlation needs >= 3 to be defined at all) with distinct
    rates that repeat exactly across eras -> perfect split-half correlation.
    """
    rates = [{"A": 0.30, "B": 0.50, "C": 0.70}] * 4
    counts = [{"A": 100, "B": 100, "C": 100}] * 4
    s = rd.stability(rates, counts)
    assert s["mean_abs_drift"] == pytest.approx(0.0)
    assert s["mean_range"] == pytest.approx(0.0)
    assert s["sign_flip_frac"] == pytest.approx(0.0)
    assert s["split_half_corr"] == pytest.approx(1.0)


def test_stability_unstable_series_scores_high_drift():
    """A cell that swings between eras registers drift and sign flips."""
    rates = [{"A": 0.10}, {"A": 0.90}, {"A": 0.10}, {"A": 0.90}]
    counts = [{"A": 100}] * 4
    s = rd.stability(rates, counts)
    assert s["mean_abs_drift"] == pytest.approx(0.8)
    assert s["mean_range"] == pytest.approx(0.8)
    assert s["sign_flip_frac"] == pytest.approx(1.0)


def test_stability_split_half_corr_negative_when_inverted():
    """A rate that inverts between halves must show negative correlation.

    Needs >= 3 cells with varying rates: with only two identical-valued cells the
    x-variance is zero and the correlation is undefined (None), which is a
    different case (see the test below).
    """
    rates = [{"A": 0.9, "B": 0.5, "C": 0.1}, {"A": 0.9, "B": 0.5, "C": 0.1},
             {"A": 0.1, "B": 0.5, "C": 0.9}, {"A": 0.1, "B": 0.5, "C": 0.9}]
    counts = [{"A": 100, "B": 100, "C": 100}] * 4
    s = rd.stability(rates, counts)
    assert s["split_half_corr"] == pytest.approx(-1.0)


def test_stability_split_half_corr_none_when_no_variance():
    """With fewer than 3 cells the correlation is undefined — it must be None,
    not a fabricated 0 or 1."""
    rates = [{"A": 0.9, "B": 0.8}, {"A": 0.9, "B": 0.8},
             {"A": 0.1, "B": 0.2}, {"A": 0.1, "B": 0.2}]
    counts = [{"A": 100, "B": 100}] * 4
    s = rd.stability(rates, counts)
    assert s["split_half_corr"] is None


def test_stability_handles_empty():
    s = rd.stability([], [])
    assert s["mean_abs_drift"] is None
    assert s["n_cells"] == 0


def test_stability_ignores_cells_absent_from_a_pair():
    """Drift is only computed where a cell appears in BOTH consecutive eras."""
    rates = [{"A": 0.5}, {"B": 0.9}]
    counts = [{"A": 10}, {"B": 10}]
    s = rd.stability(rates, counts)
    assert s["mean_abs_drift"] is None      # no shared cell
    assert s["n_consecutive_pairs"] == 0


# ─── Stability verdict ────────────────────────────────────────────────────


def test_stability_verdict_unstable():
    v = rd.stability_verdict({"mean_abs_drift": 0.2, "split_half_corr": 0.1})
    assert v.startswith("UNSTABLE")


def test_stability_verdict_stable():
    v = rd.stability_verdict({"mean_abs_drift": 0.02, "split_half_corr": 0.9})
    assert v.startswith("STABLE")


def test_stability_verdict_no_data():
    assert rd.stability_verdict({}) == "no data"
    assert rd.stability_verdict({"mean_abs_drift": None}) == "no data"
