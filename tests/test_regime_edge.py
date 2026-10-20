"""tests/test_regime_edge.py — the proof engine's own proofs.

Run: .venv/bin/python -m pytest tests/test_regime_edge.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from regime_edge import barrier as rbar  # noqa: E402
from regime_edge import brackets as rbr  # noqa: E402
from regime_edge import edge as redge  # noqa: E402
from regime_edge import features as rfeat  # noqa: E402
from regime_edge import values as rvalues  # noqa: E402
from regime_edge.brackets import resolve, slot_grid  # noqa: E402


def _rw(m=3000, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    steps = drift + rng.standard_normal(m) * 0.01
    close = 100.0 * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(rng.standard_normal(m)) * 0.003)
    low = close * (1 - np.abs(rng.standard_normal(m)) * 0.003)
    return high, low, close


# ── ER bounds ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_er_bounds_random_walk(seed):
    _, _, close = _rw(seed=seed)
    er, er_s = rfeat.kaufman_er(close, 24)
    body = er[24:]
    body_s = er_s[24:]
    assert np.all(np.isfinite(body)) and np.all(body >= 0) and np.all(body <= 1)
    assert np.all(body_s >= -1) and np.all(body_s <= 1)
    assert np.allclose(np.abs(body_s), body)


# ── NO-LOOKAHEAD proof ──────────────────────────────────────────────────────
@pytest.mark.parametrize("N", [1800, 2400])
def test_no_lookahead_features_values(N):
    high, low, close = _rw(m=3000, seed=5)
    feats_full = rfeat.compute_all(high, low, close)
    vals_full = rvalues.regime_values(feats_full)
    feats_tr = rfeat.compute_all(high[:N], low[:N], close[:N])
    vals_tr = rvalues.regime_values(feats_tr)
    for k in feats_full:
        a, b = np.asarray(feats_full[k], float), np.asarray(feats_tr[k], float)
        # compare where the truncated series has non-NaN values (warm-up region identical)
        ok = np.isfinite(b)
        assert ok.any(), k
        assert np.allclose(a[:N][ok], b[ok], equal_nan=False, atol=1e-12), f"lookahead in {k}"
    assert list(vals_full["regime"][:N]) == list(vals_tr["regime"])
    for k in ("trend", "chop", "vol"):
        a, b = np.asarray(vals_full[k], float), np.asarray(vals_tr[k], float)
        ok = np.isfinite(b)
        assert np.allclose(a[:N][ok], b[ok], atol=1e-12), f"lookahead in value {k}"


# ── bracket resolution hand-cases ───────────────────────────────────────────
def _mk_bars(o, h, l, c):
    return np.array(o, float), np.array(h, float), np.array(l, float), np.array(c, float)


def test_bracket_pure_tp():
    o, h, l, c = _mk_bars([100, 100, 100], [100, 103, 100], [100, 99.5, 99], [100, 102, 101])
    atr = np.full(3, 1.0)
    out, r, j = resolve(0, 1, tp_atr=2.0, sl_atr=1.0, expiry_bars=2, high=h, low=l, close=c, atr=atr)
    assert out == "tp" and r == 2.0 and j == 1


def test_bracket_pure_sl():
    o, h, l, c = _mk_bars([100, 100, 100], [100, 100.5, 100], [100, 98.9, 99], [100, 99, 99])
    atr = np.full(3, 1.0)
    out, r, j = resolve(0, 1, tp_atr=2.0, sl_atr=1.0, expiry_bars=2, high=h, low=l, close=c, atr=atr)
    assert out == "sl" and r == -1.0 and j == 1


def test_bracket_both_touch_sl_first():
    # one bar touches BOTH barriers -> SL first (repo intrabar basis)
    o, h, l, c = _mk_bars([100, 100], [100, 102.5], [100, 98.5], [100, 101])
    atr = np.full(2, 1.0)
    out, r, j = resolve(0, 1, tp_atr=2.0, sl_atr=1.0, expiry_bars=1, high=h, low=l, close=c, atr=atr)
    assert out == "sl" and r == -1.0


def test_bracket_expiry_mtm_sign():
    # no barrier touched: long expires above entry -> positive MTM R
    o, h, l, c = _mk_bars([100, 100, 100, 100], [100, 100.2, 100.2, 100.2],
                          [100, 99.8, 99.8, 99.8], [100, 100.1, 100.1, 100.5])
    atr = np.full(4, 1.0)
    out, r, j = resolve(0, 1, tp_atr=2.0, sl_atr=1.0, expiry_bars=3, high=h, low=l, close=c, atr=atr)
    assert out == "expire" and r == pytest.approx(0.5) and j == 3
    # long expires below entry -> negative MTM R
    c2 = np.array([100, 100.1, 100.1, 99.5], float)
    out, r, j = resolve(0, 1, tp_atr=2.0, sl_atr=1.0, expiry_bars=3, high=h, low=l, close=c2, atr=atr)
    assert out == "expire" and r == pytest.approx(-0.5)


def test_bracket_short_mirror():
    # short: SL above, TP below — mirror of the long cases
    o, h, l, c = _mk_bars([100, 100], [100, 101.5], [100, 97.5], [100, 99])
    atr = np.full(2, 1.0)
    out, r, j = resolve(0, -1, tp_atr=2.0, sl_atr=1.0, expiry_bars=1, high=h, low=l, close=c, atr=atr)
    assert out == "sl" and r == -1.0  # both touched -> SL first on the short side too
    o, h, l, c = _mk_bars([100, 100], [100, 100.2], [100, 97.9], [100, 98])
    out, r, j = resolve(0, -1, tp_atr=2.0, sl_atr=1.0, expiry_bars=1, high=h, low=l, close=c, atr=atr)
    assert out == "tp" and r == 2.0
    # short expiry MTM: price above entry -> loss in R
    o, h, l, c = _mk_bars([100, 100, 100], [100, 100.2, 100.2], [100, 99.9, 99.9], [100, 100.4, 100.4])
    out, r, j = resolve(0, -1, tp_atr=2.0, sl_atr=1.0, expiry_bars=2, high=h, low=l, close=c, atr=atr)
    assert out == "expire" and r == pytest.approx(-0.4)


def test_bracket_data_end_before_expiry():
    o, h, l, c = _mk_bars([100, 100], [100, 100.1], [100, 99.9], [100, 100.3])
    atr = np.full(2, 1.0)
    out, r, j = resolve(0, 1, tp_atr=2.0, sl_atr=1.0, expiry_bars=48, high=h, low=l, close=c, atr=atr)
    assert out == "expire" and j == 1 and r == pytest.approx(0.3)


# ── gambler's ruin ──────────────────────────────────────────────────────────
def test_gamblers_ruin_driftless_vs_mc():
    p_tp, _ = rbar.p_tp_first_closed(1.0, 2.0, mu=0.0)
    assert abs(p_tp - 1.0 / 3.0) < 1e-12
    # large T (deadline non-binding for barriers 2/1: sigma_T >> span, so
    # pi_expire ~ 0) at reporting-grade monitoring density (sub=64; the
    # discrete-monitoring overshoot bias is ~+0.005 there, +0.015 at sub=16,
    # +0.05 at hourly steps): MC matches the closed form within 0.01
    mc_tp, mc_sl, mc_e = rbar.mc_first_passage(2.0, 1.0, 100, mu=0.0, n_paths=20000, seed=7, sub=64)
    assert abs(mc_tp - p_tp) < 0.01, (mc_tp, p_tp)
    assert mc_e < 0.005, mc_e
    assert mc_tp + mc_sl + mc_e == pytest.approx(1.0, abs=1e-9)


def test_positive_drift_raises_tp_first():
    p0, _ = rbar.p_tp_first_closed(1.0, 2.0, mu=0.0)
    p1, _ = rbar.p_tp_first_closed(1.0, 2.0, mu=0.05)
    p2, _ = rbar.p_tp_first_closed(1.0, 2.0, mu=0.2)
    assert p1 > p0 and p2 > p1
    # and the closed form with drift matches an MC with drift at large T
    mc, _, _ = rbar.mc_first_passage(2.0, 1.0, 100, mu=0.05, n_paths=20000, seed=7, sub=64)
    assert abs(mc - p1) < 0.01, (mc, p1)


def test_barrier_crosscheck_against_repo():
    try:
        cc = rbar.crosscheck_repo()
    except Exception as e:  # repo import unavailable in this environment
        pytest.skip(f"repo barrier import failed: {e}")
    for c in cc:
        assert c["abs_diff"] < 0.02, c  # independent MC within ~0.02 of the repo's analytic null


# ── tri-state hysteresis ────────────────────────────────────────────────────
def test_tri_state_hysteresis():
    seq = [0.30, -0.30, 0.30, -0.30]            # whipsaw inside the dead band
    labels = rvalues.tri_state_hysteresis(np.array(seq))
    assert list(labels) == ["neutral"] * 4      # debounce-like: never enters
    seq2 = [0.30, 0.28, 0.50, 0.55, 0.10, 0.0, -0.50, -0.52]
    labels2 = list(rvalues.tri_state_hysteresis(np.array(seq2)))
    assert labels2[2] == "trend_up"             # enters TREND at the threshold
    assert labels2[3] == "trend_up"
    assert labels2[4] == "neutral"              # |er| < 0.25 -> exit
    assert labels2[6] == "trend_down"           # full opposite reading flips
    # asymmetric: from neutral, readings in (0.25, 0.35) stay neutral
    labels3 = list(rvalues.tri_state_hysteresis(np.array([0.26, 0.33, 0.34, 0.36])))
    assert labels3[:3] == ["neutral"] * 3 and labels3[3] == "trend_up"


# ── walk-forward lockbox ────────────────────────────────────────────────────
def test_walkforward_lockbox_disjoint():
    rng = np.random.default_rng(0)
    trades = []
    for slot in range(720, 6000, 24):
        trades.append({"slot": slot, "outcome": rng.choice(["tp", "sl", "expire"], p=[.3, .5, .2]),
                       "er_signed": rng.uniform(-1, 1), "vol": rng.uniform(0, 1), "r": rng.normal(0, 1)})
    wf = redge.walkforward_cells(trades, 6000, first_fit=3000, step=1000)
    assert wf["adaptive_note"] is None
    assert wf["segments"], "no segments produced"
    for seg in wf["segments"]:
        fit_set = set(seg["fit_slots"])
        pred_set = set(seg["predict_slots"])
        assert fit_set.isdisjoint(pred_set), "lockbox violated: a predicted slot was in its own fit set"
    # every predicted slot is strictly later than every fit slot in its segment
    for seg in wf["segments"]:
        if seg["fit_slots"] and seg["predict_slots"]:
            assert min(seg["predict_slots"]) > max(seg["fit_slots"])
    # predictions exist only for slots >= first fit
    assert all(s >= 3000 for s in wf["preds"])


# ── determinism of the books pipeline ───────────────────────────────────────
def test_books_pipeline_deterministic():
    from regime_edge import books as rbooks
    m = 2600
    high, low, close = _rw(m=m, seed=3, drift=0.0002)
    feats = rfeat.compute_all(high, low, close)
    vals = rvalues.regime_values(feats)
    from regime_edge import hmm_ref as rhmm
    hmm_out = rhmm.run_hmm_repro(close, refit_every=48, window=200)
    geo_cur = {"tp_atr": 4.52, "sl_atr": 2.75, "expiry": 48}
    geo_cor = {"tp_atr": 3.0, "sl_atr": 2.0, "expiry": 96}
    trades_cor = redge.build_trades(slot_grid(m), feats, vals, geo_cor, high, low, close, feats["atr"])
    wf_cor = redge.walkforward_cells(trades_cor, m)
    ohlc = {"high": high, "low": low, "close": close, "atr": feats["atr"], "n": m}
    r1 = rbooks.run_books("TEST", ohlc, feats, vals, hmm_out, geo_cur, geo_cor,
                          wf_cor["preds"], 0.04, seed=7)
    r2 = rbooks.run_books("TEST", ohlc, feats, vals, hmm_out, geo_cur, geo_cor,
                          wf_cor["preds"], 0.04, seed=7)
    for book in ("kairos", "corrected", "null"):
        assert r1["books"][book]["equity"] == r2["books"][book]["equity"], book
        assert r1["books"][book]["mean_r"] == r2["books"][book]["mean_r"]
        assert r1["books"][book]["n"] == r2["books"][book]["n"]
    assert r1["dm"] == r2["dm"]


# ── edge_lab.json schema smoke on synthetic data ────────────────────────────
def test_edge_lab_schema_smoke(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rng = np.random.default_rng(11)
    m = 1500
    ts = pd.date_range("2025-01-01", periods=m, freq="1h", tz="UTC")
    for sym in ("BTCUSD", "XAUUSD"):
        steps = rng.standard_normal(m) * 0.01
        close = 100.0 * np.exp(np.cumsum(steps))
        high = close * (1 + np.abs(rng.standard_normal(m)) * 0.004)
        low = close * (1 - np.abs(rng.standard_normal(m)) * 0.004)
        df = pd.DataFrame({
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "open": close, "high": high, "low": low, "close": close,
            "volume": np.zeros(m)})
        df.to_csv(data_dir / f"{sym}.csv", index=False)
    env = dict(os.environ, EDGE_LAB_FAST="1")
    out_dir = tmp_path / "reports"
    cmd = [sys.executable, os.path.join(REPO_ROOT, "scripts", "run_edge_lab.py"),
           "--symbols", "BTCUSD", "XAUUSD", "--data-dir", str(data_dir),
           "--out-dir", str(out_dir), "--bars-limit", str(m)]
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=900, cwd=REPO_ROOT)
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-4000:]
    runs = sorted((out_dir).iterdir())
    artifact = runs[-1] / "edge_lab.json"
    with open(artifact) as fh:
        doc = json.load(fh)
    top = ["meta", "verdicts", "hmm", "regime_values", "edge_surface", "gate_backtest",
           "calibration", "barrier", "tsfm_reframe", "fixes"]
    assert all(k in doc for k in top), [k for k in top if k not in doc]
    assert [v["id"] for v in doc["verdicts"]] == [
        "W-C1", "W-C2", "W-C3", "W-C4", "W-C5", "W-C6",
        "W-X1", "W-X2", "W-X3", "W-X4", "W-X5", "W-X6",
        "W-G1", "W-G2", "W-G3", "W-G4", "W-G5"]
    assert len(doc["verdicts"]) == 17

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
        elif isinstance(o, float):
            yield o
    bad = [x for x in walk(doc) if not np.isfinite(x)]
    assert not bad, bad[:5]
    assert os.path.getsize(artifact) < 3e6
    assert (runs[-1] / "summary.md").exists()
    assert (runs[-1] / "records.csv").exists()


# ── vol_percentile bounds ───────────────────────────────────────────────────
def test_vol_percentile_bounds():
    _, _, close = _rw(m=2000, seed=9)
    ap = rfeat.atr_pct(*_rw(m=2000, seed=9))
    vp = rfeat.vol_percentile(ap)
    body = vp[1000:]
    assert np.all(np.isfinite(body)) and np.all(body >= 0) and np.all(body <= 1)
