"""edge.py — the conditional-edge model ("predict the edge").

1.  Per-slot trades under a geometry with direction = sign(er_signed).
2.  Edge surface: decile buckets of each feature -> outcome rates + mean R
    with bootstrap 95% CIs.
3.  Edge predictability: Spearman rho(feature, R) with permutation p-values —
    the headline question: does the regime value carry edge information?
4.  Repo-style bucket calibration (symbol, vol_tier, distance bucket) ->
    cell-mean p_win; AUC vs TP outcome (level-only, no discrimination).
5.  Regime-conditional model (the fix): trend quintile x vol tercile cells,
    3-outcome rates with shrinkage (n0=30), WALK-FORWARD lockbox — Brier /
    ECE / AUC vs the trailing flat base rate.
6.  Reliability curves for both models.
"""
from __future__ import annotations

import numpy as np
from scipy import stats
from sklearn.metrics import roc_auc_score

from .brackets import resolve

TREND_EDGES = (-1.0, -0.6, -0.2, 0.2, 0.6, 1.0)   # fixed quintile edges (no data-derived edges -> no lookahead)
VOL_EDGES = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)      # fixed tercile edges
N0 = 30
FEATURE_KEYS = ("er_abs", "chop", "vol", "hurst", "adx")


def build_trades(slots, features, values, geometry, high, low, close, atr):
    """Per-slot bracket trades (direction = sign(er_signed)) under `geometry`."""
    er_s = np.asarray(features["er_signed"], float)
    er = np.asarray(features["er"], float)
    trades = []
    for i in slots:
        v = er_s[i]
        if not np.isfinite(v) or v == 0:
            continue
        d = 1 if v > 0 else -1
        outcome, r, end = resolve(i, d, geometry["tp_atr"], geometry["sl_atr"],
                                  geometry["expiry"], high, low, close, atr)
        trades.append({
            "slot": int(i), "direction": d, "outcome": outcome, "r": float(r), "end": int(end),
            "er_signed": float(v), "er_abs": float(abs(v)) if np.isfinite(v) else float("nan"),
            "er": float(er[i]) if np.isfinite(er[i]) else float("nan"),
            "chop": float(features["chop"][i]), "vol": float(features["vol_pct"][i]),
            "hurst": float(features["hurst"][i]), "adx": float(features["adx"][i]),
            "regime": values["regime"][i],
        })
    return trades


# ── 2. edge surface ────────────────────────────────────────────────────────
def _bootstrap_ci(r: np.ndarray, n_boot=2000, seed=7):
    if r.size == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, r.size, size=(n_boot, r.size))
    means = r[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def edge_surface(trades):
    by_feature = {}
    for k in FEATURE_KEYS:
        x = np.array([t[k] for t in trades], dtype=float)
        r = np.array([t["r"] for t in trades], dtype=float)
        out_c = np.array([t["outcome"] for t in trades])
        ok = np.isfinite(x) & np.isfinite(r)
        x, r, out_c = x[ok], r[ok], out_c[ok]
        n = x.size
        buckets = []
        if n >= 10:
            order = np.argsort(x, kind="stable")
            qs = np.array_split(order, 10)
            for grp in qs:
                if grp.size == 0:
                    continue
                rr = r[grp]
                lo, hi = _bootstrap_ci(rr)
                buckets.append({
                    "bin_lo": round(float(x[grp].min()), 4),
                    "bin_hi": round(float(x[grp].max()), 4),
                    "n": int(grp.size),
                    "tp_rate": round(float(np.mean(out_c[grp] == "tp")), 4),
                    "sl_rate": round(float(np.mean(out_c[grp] == "sl")), 4),
                    "exp_rate": round(float(np.mean(out_c[grp] == "expire")), 4),
                    "mean_r": round(float(rr.mean()), 4),
                    "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                })
        by_feature[k] = buckets
    return by_feature


# ── 3. edge predictability ─────────────────────────────────────────────────
def spearman_tests(trades, n_perm=2000, seed=7):
    out = {}
    rng = np.random.default_rng(seed)
    r = np.array([t["r"] for t in trades], dtype=float)
    for k in FEATURE_KEYS:
        x = np.array([t[k] for t in trades], dtype=float)
        ok = np.isfinite(x) & np.isfinite(r)
        x, rr = x[ok], r[ok]
        if x.size < 10 or np.all(x == x[0]) or np.all(rr == rr[0]):
            out[k] = {"rho": 0.0, "p": 1.0}
            continue
        rho, _ = stats.spearmanr(x, rr)
        rho = float(rho)
        count = 0
        for _ in range(n_perm):
            xp = rng.permutation(x)
            rp, _ = stats.spearmanr(xp, rr)
            if abs(rp) >= abs(rho):
                count += 1
        out[k] = {"rho": round(rho, 4), "p": round((1 + count) / (1 + n_perm), 4)}
    return out


# ── 4. repo-style bucket calibration (level-only) ──────────────────────────
def repo_bucket_calibration(trades, tp_atr):
    """Cells (symbol-implicit, vol_tier tercile, distance bucket=floor(tp_atr))
    -> cell mean p_win; AUC of cell-mean vs TP outcome.  Full-sample measured
    table (the repo's approximation): level information only."""
    vol = np.array([t["vol"] for t in trades], dtype=float)
    y = np.array([1.0 if t["outcome"] == "tp" else 0.0 for t in trades])
    bucket = int(np.floor(tp_atr))
    ok = np.isfinite(vol)
    vol, y = vol[ok], y[ok]
    cells = {}
    for v_, y_ in zip(vol, y):
        tier = min(2, max(0, int(v_ * 3)))
        key = (tier, bucket)
        cells.setdefault(key, []).append(y_)
    pred = np.empty(vol.size)
    for i, v_ in enumerate(vol):
        tier = min(2, max(0, int(v_ * 3)))
        pred[i] = float(np.mean(cells[(tier, bucket)]))
    if len(np.unique(y)) < 2:
        return {"auc": 0.5, "n_cells": len(cells)}
    return {"auc": round(float(roc_auc_score(y, pred)), 4), "n_cells": len(cells)}


# ── 5. walk-forward regime-conditional cells (lockbox) ──────────────────────
def _cells(trades_subset):
    """Fit 15 trend-quintile x vol-tercile cells with shrinkage to base rate."""
    base = {
        "p_tp": float(np.mean([t["outcome"] == "tp" for t in trades_subset])) if trades_subset else 0.0,
        "p_sl": float(np.mean([t["outcome"] == "sl" for t in trades_subset])) if trades_subset else 0.0,
        "p_exp": float(np.mean([t["outcome"] == "expire" for t in trades_subset])) if trades_subset else 0.0,
        "n": len(trades_subset),
    }
    cells = {}
    for t in trades_subset:
        key = (_trend_bin(t["er_signed"]), _vol_bin(t["vol"]))
        c = cells.setdefault(key, {"n": 0, "tp": 0, "sl": 0, "expire": 0})
        c["n"] += 1
        c[t["outcome"]] += 1
    fitted = {}
    for key, c in cells.items():
        n = c["n"] + N0
        fitted[key] = {
            "p_tp": (c["tp"] + N0 * base["p_tp"]) / n,
            "p_sl": (c["sl"] + N0 * base["p_sl"]) / n,
            "p_exp": (c["expire"] + N0 * base["p_exp"]) / n,
            "n": c["n"],
        }
    return {"base": base, "cells": fitted}


def _trend_bin(v):
    if not np.isfinite(v):
        return 2
    return int(min(4, max(0, np.digitize(v, TREND_EDGES[1:-1], right=False))))


def _vol_bin(v):
    if not np.isfinite(v):
        return 1
    return int(min(2, max(0, np.digitize(v, VOL_EDGES[1:-1], right=False))))


def walkforward_cells(trades, n_bars, first_fit=3000, step=1000):
    """Refit on trailing data every `step` bars, predict the next `step`
    (first fit at `first_fit`).  LOCKBOX: a slot is only ever scored by a model
    fit exclusively on strictly-earlier slots.  Returns per-slot predictions,
    per-slot base-rate predictions, and (fit_slots, predict_slots) segments
    for the disjointness test.  Adaptive schedule for short smoke runs."""
    adaptive_note = None
    if n_bars < first_fit + step:
        first_fit = max(1200, n_bars // 2)
        step = 500
        adaptive_note = f"adaptive walk-forward (first_fit={first_fit}, step={step}) for short series"
    slots = sorted(t["slot"] for t in trades)
    fit_bars = list(range(first_fit, n_bars + 1, step))
    segments = []
    preds = {}
    base_preds = {}
    for k, fb in enumerate(fit_bars):
        next_fb = fit_bars[k + 1] if k + 1 < len(fit_bars) else n_bars + 1
        fit_trades = [t for t in trades if t["slot"] < fb]
        pred_trades = [t for t in trades if fb <= t["slot"] < next_fb]
        if not pred_trades:
            continue
        model = _cells(fit_trades)
        for t in pred_trades:
            key = (_trend_bin(t["er_signed"]), _vol_bin(t["vol"]))
            cell = model["cells"].get(key)
            if cell is None:
                preds[t["slot"]] = (model["base"]["p_tp"], model["base"]["p_sl"], model["base"]["p_exp"])
            else:
                preds[t["slot"]] = (cell["p_tp"], cell["p_sl"], cell["p_exp"])
            base_preds[t["slot"]] = (model["base"]["p_tp"], model["base"]["p_sl"], model["base"]["p_exp"])
        segments.append({
            "fit_slots": sorted(t["slot"] for t in fit_trades),
            "predict_slots": sorted(t["slot"] for t in pred_trades),
        })
    return {"preds": preds, "base_preds": base_preds, "segments": segments,
            "adaptive_note": adaptive_note}


def calibration_metrics(trades, wf):
    """Brier / ECE / AUC on lockbox rows for the regime model and base rate."""
    lock = [t for t in trades if t["slot"] in wf["preds"]]
    if not lock:
        return {"regime_conditional": _empty_metrics(), "base_rate": _empty_metrics(),
                "n_lockbox": 0, "reliability_regime": [], "reliability_base": []}
    y = np.array([1.0 if t["outcome"] == "tp" else 0.0 for t in lock])
    p_reg = np.array([wf["preds"][t["slot"]][0] for t in lock])
    p_base = np.array([wf["base_preds"][t["slot"]][0] for t in lock])
    return {
        "regime_conditional": _metrics(y, p_reg),
        "base_rate": _metrics(y, p_base),
        "n_lockbox": len(lock),
        "reliability_regime": reliability(p_reg, y),
        "reliability_base": reliability(p_base, y),
    }


def _empty_metrics():
    return {"auc": 0.5, "brier": 0.25, "ece": 0.0}


def _metrics(y, p):
    brier = float(np.mean((p - y) ** 2))
    if len(np.unique(y)) < 2 or np.all(p == p[0]):
        auc = 0.5
    else:
        auc = float(roc_auc_score(y, p))
    ece = 0.0
    order = np.argsort(p, kind="stable")
    bins = np.array_split(order, 10)
    for b in bins:
        if b.size == 0:
            continue
        ece += b.size / y.size * abs(p[b].mean() - y[b].mean())
    return {"auc": round(auc, 4), "brier": round(brier, 4), "ece": round(float(ece), 4)}


def reliability(p, y, n_bins=10):
    order = np.argsort(p, kind="stable")
    bins = np.array_split(order, n_bins)
    out = []
    for b in bins:
        if b.size == 0:
            continue
        out.append({"pred": round(float(p[b].mean()), 4),
                    "emp": round(float(y[b].mean()), 4),
                    "n": int(b.size)})
    return out
