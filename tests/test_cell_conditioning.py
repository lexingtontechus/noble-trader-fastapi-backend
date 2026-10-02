"""
Tests for regime_platform/sweep_orchestrator/cell_conditioning.py.

The load-bearing assertions are about **methodology**, not snapshots:
  * walk-forward splits are strictly time-ordered and non-overlapping
  * a row is never in its own training window (no leakage)
  * the ladder backs off from a sparse cell to its parent
  * shrinkage pulls a small cell toward the parent rate
  * adding conditioning cannot be tested in-sample — the API only exposes
    out-of-fold evaluation
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator import cell_conditioning as cc


def _row(symbol="EURUSD", regime="med_low_vol_strong_bull", signal="buy",
         atr=1.0, entry=100.0, tp=101.0, realised=True, p_win=0.6, ts=0):
    return {"symbol": symbol, "regime": regime, "signal": signal, "atr": atr,
            "entry_price": entry, "take_profit": tp, "realised": realised,
            "p_win": p_win, "ts": ts}


# ─── Keys ─────────────────────────────────────────────────────────────────


def test_cell_key_shape():
    k = cc.cell_key(_row())
    assert k is not None
    sym, vol, bucket = k
    assert sym == "EURUSD"
    assert vol == "med_low_vol"
    assert bucket == "1-2"        # |101-100| / 1.0 = 1.0


def test_cell_key_uses_distance_over_atr():
    # 2.0 ATR away -> bucket 2-4
    assert cc.cell_key(_row(tp=102.0))[2] == "2-4"
    # 0.4 ATR away -> bucket <0.5
    assert cc.cell_key(_row(tp=100.4))[2] == "<0.5"


def test_cell_key_none_when_atr_missing():
    assert cc.cell_key(_row(atr=None)) is None
    assert cc.cell_key(_row(atr=0.0)) is None


def test_cell_key_none_when_prices_missing():
    assert cc.cell_key(_row(tp=None)) is None
    assert cc.cell_key(_row(entry=None)) is None


def test_cell_key_none_when_symbol_missing():
    assert cc.cell_key(_row(symbol=None)) is None


def test_cell_key_short_uses_abs_distance():
    """A short's TP is below entry; the ratio must be positive."""
    k = cc.cell_key(_row(signal="sell", entry=100.0, tp=98.0, atr=1.0))
    assert k is not None
    assert k[2] == "2-4"


def test_trend_state_parsing():
    assert cc.trend_state("med_low_vol_strong_bear") == "strong_bear"
    assert cc.trend_state("high_vol_bull") == "bull"
    assert cc.trend_state("unknown") == "unknown"
    assert cc.trend_state(None) == "unknown"
    # no marker at all -> unknown (the parse cannot even start)
    assert cc.trend_state("no_vol_marker_here".replace("_vol_", "_x_")) == "unknown"


def test_trend_state_returns_empty_tail_as_unknown():
    """A label ending exactly at the marker has no trend half."""
    assert cc.trend_state("med_low_vol_") == "unknown"


def test_p_win_band_thresholds():
    assert cc.p_win_band(_row(p_win=0.85)) == "hi"
    assert cc.p_win_band(_row(p_win=0.65)) == "mid_hi"
    assert cc.p_win_band(_row(p_win=0.55)) == "mid"
    assert cc.p_win_band(_row(p_win=0.45)) == "mid_lo"
    assert cc.p_win_band(_row(p_win=0.10)) == "lo"
    assert cc.p_win_band(_row(p_win=None)) == "unknown"


# ─── Fit ──────────────────────────────────────────────────────────────────


def test_fit_counts_wins_and_n():
    rows = [_row(realised=True), _row(realised=True), _row(realised=False)]
    levels = cc.SPECS["cell"]
    tables = cc.fit(rows, levels)
    assert len(tables) == len(levels)
    # global level is last
    w, n = tables[-1]["global"]
    assert n == 3
    assert w == pytest.approx(2.0)


def test_fit_global_level_catches_unplaceable_rows():
    """An ATR-less row cannot enter any *cell* level, but the global level
    (the ladder's terminal key) legitimately catches it for the prior — while
    still being unable to score it. Both behaviours are asserted."""
    rows = [_row(), _row(atr=None)]
    tables = cc.fit(rows, cc.SPECS["cell"])
    # the cell level saw only the placeable row
    cell_tbl = tables[0]
    assert sum(n for _, n in cell_tbl.values()) == 1
    # the global level caught both (it is the backoff of last resort)
    _, n = tables[-1]["global"]
    assert n == 2
    # ...but the unplaceable row still cannot be PREDICTED
    assert cc._rate(tables, cc.SPECS["cell"], _row(atr=None), 10.0, 20) is None


# ─── Backoff and shrinkage ────────────────────────────────────────────────


def test_sparse_cell_backs_off_to_parent():
    """A cell with fewer than min_n training rows must not be used — the ladder
    falls back to the coarser level rather than trusting 2 observations."""
    # 100 rows in one cell (dense), 2 in another (sparse)
    dense = [_row(symbol="AAA", realised=True)] * 60 + [_row(symbol="AAA", realised=False)] * 40
    sparse = [_row(symbol="BBB", realised=True)] * 2
    rows = dense + sparse
    levels = cc.SPECS["cell"]
    tables = cc.fit(rows, levels, min_n=20)
    # the sparse symbol's cell key
    k = cc.cell_key(sparse[0])
    p = cc._rate(tables, levels, sparse[0], cc.DEFAULT_ALPHA, min_n=20)
    assert p is not None
    # the symbol-level key for BBB has only 2 rows -> below min_n, so the answer
    # is pulled to the global rate, not the cell's 1.0
    assert p < 0.9


def test_shrinkage_pulls_small_cell_toward_parent():
    """A cell with 1 win out of 1 must NOT be predicted at 1.0."""
    rows = [_row(symbol="AAA", realised=True)] * 50 + \
           [_row(symbol="AAA", realised=False)] * 50 + \
           [_row(symbol="BBB", tp=103.0, realised=True)]     # 1 row, distinct cell
    levels = cc.SPECS["cell"]
    tables = cc.fit(rows, levels)
    target = _row(symbol="BBB", tp=103.0, realised=True)
    p = cc._rate(tables, levels, target, cc.DEFAULT_ALPHA, min_n=20)
    assert p is not None
    assert 0.0 < p < 1.0


def test_more_shrinkage_pulls_further():
    rows = [_row(symbol="AAA", realised=True)] * 50 + \
           [_row(symbol="AAA", realised=False)] * 50 + \
           [_row(symbol="BBB", tp=103.0, realised=True)]
    levels = cc.SPECS["cell"]
    tables = cc.fit(rows, levels)
    target = _row(symbol="BBB", tp=103.0, realised=True)
    weak = cc._rate(tables, levels, target, 1.0, 20)
    strong = cc._rate(tables, levels, target, 100.0, 20)
    # higher alpha -> closer to the parent (0.5-ish), i.e. less extreme
    assert abs(strong - 0.5) < abs(weak - 0.5)


def test_global_level_is_beta_prior_when_empty():
    """With no training rows at all, the global rate is the Beta(1,1) mean."""
    p = cc._rate([{}], [lambda r: "global"], _row(), cc.DEFAULT_ALPHA, 20)
    assert p == pytest.approx(0.5)


# ─── Walk-forward ─────────────────────────────────────────────────────────


def test_walk_forward_splits_are_time_ordered_and_disjoint():
    rows = [_row(ts=i) for i in range(100)]
    splits = cc.walk_forward_splits(rows, n_folds=5, warmup_frac=0.5)
    assert len(splits) == 5
    for train, test in splits:
        assert train and test
        train_ts = [r["ts"] for r in train]
        test_ts = [r["ts"] for r in test]
        # every training row precedes every test row
        assert max(train_ts) < min(test_ts)


def test_no_row_is_in_its_own_training_window():
    """The leakage guard: the union of all test blocks is exactly the post-warmup
    rows, and no test row ever appears in its own train set."""
    rows = [_row(ts=i) for i in range(100)]
    splits = cc.walk_forward_splits(rows, n_folds=5, warmup_frac=0.5)
    seen_test = []
    for train, test in splits:
        train_ids = {id(r) for r in train}
        for r in test:
            assert id(r) not in train_ids
            seen_test.append(r["ts"])
    # test blocks tile the post-warmup range with no gaps or overlaps
    assert sorted(seen_test) == list(range(50, 100))


def test_walk_forward_empty_when_too_few_rows():
    assert cc.walk_forward_splits([_row()], n_folds=5) == []
    assert cc.walk_forward_splits([_row(ts=i) for i in range(4)], n_folds=5) == []


# ─── Evaluate / compare ───────────────────────────────────────────────────


def _synthetic(n=600, seed=7):
    """A population where the cell is genuinely informative: wider brackets
    (2-4 ATR) lose more often than tight ones (<0.5). Direction is pure noise."""
    import random
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        tight = rng.random() < 0.5
        tp = 100.4 if tight else 103.0
        p_tp = 0.75 if tight else 0.25
        rows.append(_row(ts=i, tp=tp, realised=rng.random() < p_tp,
                         signal=rng.choice(["buy", "sell"]),
                         regime=rng.choice(["med_low_vol_strong_bull",
                                            "med_low_vol_strong_bear"])))
    return rows


def test_evaluate_returns_out_of_fold_auc():
    rows = _synthetic()
    r = cc.evaluate(rows, cc.SPECS["cell"], n_folds=5)
    assert r["n"] > 0
    # the cell carries the signal by construction, so AUC should be well > 0.5
    assert r["auc"] > 0.6


def test_evaluate_empty_when_no_splits():
    assert cc.evaluate([_row()], cc.SPECS["cell"])["n"] == 0


def test_compare_sorted_by_auc():
    rows = _synthetic()
    out = cc.compare(rows, n_folds=5)
    aucs = [d["auc"] for d in out if d.get("auc") is not None]
    assert aucs == sorted(aucs, reverse=True)


def test_compare_includes_the_baseline():
    out = cc.compare(_synthetic(), n_folds=5)
    assert "cell" in [d["name"] for d in out]


def test_direction_conditioning_does_not_help_on_noise():
    """Direction is random in the synthetic population, so adding it must NOT
    clear the gain bar — the test that stops us adding noise to the model."""
    rows = _synthetic()
    out = {d["name"]: d for d in cc.compare(rows, n_folds=5)}
    base = out["cell"]
    for name in ("cell+dir", "cell+trend"):
        if name in out and out[name].get("auc") is not None:
            assert out[name]["auc"] - base["auc"] < 0.05


# ─── Verdict ──────────────────────────────────────────────────────────────


def test_verdict_adds():
    v = cc.verdict({"auc": 0.60}, {"auc": 0.65})
    assert v.startswith("ADDS")


def test_verdict_marginal():
    v = cc.verdict({"auc": 0.60}, {"auc": 0.61})
    assert v.startswith("MARGINAL")


def test_verdict_no_gain():
    v = cc.verdict({"auc": 0.60}, {"auc": 0.58})
    assert v.startswith("NO GAIN")


def test_verdict_within_noise_when_gain_below_fold_spread():
    """A +0.03 gain when the baseline's own folds swing ±0.07 is not a finding —
    the guard that stops noise being promoted to a model term."""
    base = {"auc": 0.50, "per_fold": [{"auc": a} for a in (0.44, 0.54, 0.48, 0.58, 0.47)]}
    cand = {"auc": 0.53}
    v = cc.verdict(base, cand)
    assert v.startswith("WITHIN NOISE")


def test_verdict_adds_when_gain_beats_fold_spread():
    """A large gain on stable folds IS a finding."""
    base = {"auc": 0.50, "per_fold": [{"auc": a} for a in (0.50, 0.51, 0.49, 0.50, 0.51)]}
    cand = {"auc": 0.60}
    assert cc.verdict(base, cand).startswith("ADDS")


# ─── fold_stats ───────────────────────────────────────────────────────────


def test_fold_stats_reports_spread():
    fs = cc.fold_stats([{"auc": a} for a in (0.40, 0.50, 0.60)])
    assert fs["mean"] == pytest.approx(0.50)
    assert fs["min"] == 0.40 and fs["max"] == 0.60
    assert fs["n_above_half"] == 1
    assert fs["sd"] > 0


def test_fold_stats_single_fold_has_zero_sd():
    fs = cc.fold_stats([{"auc": 0.55}])
    assert fs["sd"] == 0.0


def test_fold_stats_empty():
    assert cc.fold_stats([]) == {}
    assert cc.fold_stats([{"auc": None}]) == {}


def test_verdict_no_data():
    assert cc.verdict({}, {"auc": 0.6}) == "no data"
    assert cc.verdict({"auc": 0.6}, {}) == "no data"


# ─── In-sample ceiling / transfer verdict ─────────────────────────────────


def test_in_sample_ceiling_is_at_least_out_of_fold():
    """Memorising cannot score worse than transferring — the ceiling is an
    upper bound by construction. Both are computed on the same post-warmup
    population, so this is a like-for-like comparison."""
    rows = _synthetic()
    oof = cc.evaluate(rows, cc.SPECS["cell"], n_folds=5)
    start = int(len(rows) * 0.5)
    ins = cc.evaluate_in_sample(rows[start:], cc.SPECS["cell"])
    assert ins["n"] > 0
    assert ins["auc"] >= oof["auc"] - 0.02


def test_compare_includes_ceiling():
    out = cc.compare(_synthetic(), n_folds=5)
    assert all("in_sample_auc" in d for d in out)


def test_transfer_verdict_no_signal():
    v = cc.transfer_verdict({"auc": 0.50, "in_sample_auc": 0.51})
    assert v.startswith("NO SIGNAL")


def test_transfer_verdict_no_transfer():
    v = cc.transfer_verdict({"auc": 0.50, "in_sample_auc": 0.70})
    assert v.startswith("NO TRANSFER")


def test_transfer_verdict_transfers():
    v = cc.transfer_verdict({"auc": 0.62, "in_sample_auc": 0.70})
    assert v.startswith("TRANSFERS")


def test_transfer_verdict_no_data():
    assert cc.transfer_verdict({}) == "no data"
    assert cc.transfer_verdict({"auc": 0.6}) == "no data"
