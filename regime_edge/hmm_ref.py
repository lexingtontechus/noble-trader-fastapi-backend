"""hmm_ref.py — faithful offline reproduction of the live HMM direction stack.

Head-to-head proof target (W-X1/W-X2): the live chain is

    HMM fit (rolling 200x1h bars, refit from scratch every 15 min)
    -> 4-state trend model on [1-bar return, 10-bar cum return, 30-bar cum return]
    -> state->label by RANK of the 30-bar-cum-return component mean
       (regime_engine.py:1155-1156) => always exactly 2 bear + 2 bull states
    -> regime_label = "{vol_state}_vol_{trend_state}"
    -> 3-tick debouncer (regime_engine.py:182-197, n_confirm=3)
    -> direction = "short" if "bear" in label else "long"
    -> confidence = max(vol posteriors) * max(trend posteriors)

Copied semantics (repo: regime_platform/core/regime_engine.py @ c7568f9)
- features: _vol_features = [rv20, rv5, |ret|], _trend_features =
  [ret, cum10, cum30], trailing windows INCLUSIVE of the current bar;
- both models: hmm.GaussianHMM(n_components=4, covariance_type="diag",
  n_iter=100, random_state=42, min_covar=0.01) on per-column standardized
  features (mu/sigma of the FIT window; sigma floored at 1.0 when 0);
- means destandardized before labeling; degenerate params sanitized
  (startprob/transmat renormalised, bad covars -> 1e-3) as in _sanitize_model;
- labels: vol by feature-0 mean ascending [low, med_low, med_high, high];
  trend by feature-2 (cum30) mean ascending [strong_bear, bear, bull,
  strong_bull] — the rank assignment that guarantees no neutral state;
- predict(): posteriors = predict_proba(standardized window features)[-1],
  reordered into label order; conf = max(vol)*max(trend) rounded 4dp.

Cadence approximation (documented in meta.approximation_notes): the live
orchestrator refits BOTH HMMs from scratch on every 15-minute tick; we
approximate one tick = one hourly bar and refit every ``refit_every=24``
bars (24h), re-predicting the label EVERY bar from the most recent fit on
the current trailing window (repo predict() semantics: query features
standardized with the fit-time scale).  Deviation: no EM warm-start /
cross-tick label-continuity matching — each refit uses hmmlearn's own
auto-init and the rank-sort label map (the repo's first-fit behavior, and
the behavior the 2026-10-19 evaluation criticizes as rank noise).
"""
from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
from hmmlearn import hmm

# hmmlearn's ConvergenceMonitor logs "Model is not converging" through the
# logging module (not warnings) whenever n_iter is exhausted on a 199-sample
# window — routine on the live cadence too (the engine tracks convergence via
# monitor_.history instead). Silence at import so 600+ refits don't flood the
# run log; convergence state does not affect the direction repro.
logging.getLogger("hmmlearn").setLevel(logging.ERROR)

VOL_LABELS = ["low", "med_low", "med_high", "high"]
TREND_LABELS = ["strong_bear", "bear", "bull", "strong_bull"]


# ── feature engineering (verbatim semantics of regime_engine.py:882-903) ──
def _vol_features(returns: np.ndarray) -> np.ndarray:
    s = pd.Series(returns)
    rv_long = s.rolling(20, min_periods=1).std(ddof=0).to_numpy()
    rv_short = s.rolling(5, min_periods=1).std(ddof=0).to_numpy()
    return np.column_stack([rv_long, rv_short, np.abs(returns)])


def _trend_features(returns: np.ndarray) -> np.ndarray:
    s = pd.Series(returns)
    cum_short = s.rolling(10, min_periods=1).sum().to_numpy()
    cum_long = s.rolling(30, min_periods=1).sum().to_numpy()
    return np.column_stack([returns, cum_short, cum_long])


def _fit_scale(features: np.ndarray):
    mu = np.mean(features, axis=0)
    sigma = np.std(features, axis=0)
    sigma = np.where(sigma > 0, sigma, 1.0)
    return mu, sigma


def _sanitize(model) -> None:
    """regime_engine._sanitize_model (in-place degenerate-param repair)."""
    n = model.n_components
    sp = model.startprob_
    if not np.all(np.isfinite(sp)) or sp.sum() < 1e-8:
        model.startprob_ = np.full(n, 1.0 / n)
    else:
        model.startprob_ = sp / sp.sum()
    for i in range(n):
        row = model.transmat_[i]
        if not np.all(np.isfinite(row)) or row.sum() < 1e-8:
            model.transmat_[i] = np.full(n, 1.0 / n)
        else:
            model.transmat_[i] = row / row.sum()
    if not np.all(np.isfinite(model.means_)):
        model.means_ = np.where(np.isfinite(model.means_), model.means_, 0.0)
    if getattr(model, "_covars_", None) is not None:
        bad = ~np.isfinite(model._covars_) | (model._covars_ <= 0)
        if bad.any():
            model._covars_ = np.where(bad, 1e-3, model._covars_)


def _label_states_rank(means_col: np.ndarray, labels: list[str]) -> dict:
    """regime_engine._label_states rank branch (:1155-1156)."""
    order = np.argsort(means_col)
    return {int(order[i]): labels[i] for i in range(len(labels))}


class _Fit:
    __slots__ = ("vol_model", "trend_model", "vol_mu", "vol_sg", "tr_mu", "tr_sg",
                 "vol_map", "trend_map", "bar")


def _fit_models(closes_window: np.ndarray, bar: int) -> _Fit:
    prices = np.asarray(closes_window, float)
    returns = np.diff(np.log(prices))
    vfeat = _vol_features(returns)
    tfeat = _trend_features(returns)
    vmu, vsg = _fit_scale(vfeat)
    tmu, tsg = _fit_scale(tfeat)
    f = _Fit()
    f.vol_model = hmm.GaussianHMM(
        n_components=4, covariance_type="diag", n_iter=100,
        random_state=42, min_covar=0.01)
    f.trend_model = hmm.GaussianHMM(
        n_components=4, covariance_type="diag", n_iter=100,
        random_state=42, min_covar=0.01)
    with warnings.catch_warnings():
        # hmmlearn's non-convergence warnings fire routinely on 199-sample
        # windows (n_iter exhausted); the live engine tracks convergence via
        # monitor_.history — not needed for the direction repro.
        warnings.simplefilter("ignore")
        f.vol_model.fit((vfeat - vmu) / vsg)
        f.trend_model.fit((tfeat - tmu) / tsg)
    # destandardize means (regime_engine._destandardize_means) before labeling
    f.vol_model.means_ = f.vol_model.means_ * vsg + vmu
    f.trend_model.means_ = f.trend_model.means_ * tsg + tmu
    _sanitize(f.vol_model)
    _sanitize(f.trend_model)
    f.vol_map = _label_states_rank(f.vol_model.means_[:, 0], VOL_LABELS)
    f.trend_map = _label_states_rank(f.trend_model.means_[:, 2], TREND_LABELS)
    f.vol_mu, f.vol_sg, f.tr_mu, f.tr_sg, f.bar = vmu, vsg, tmu, tsg, bar
    return f


def _predict_bar(fit: _Fit, closes_window: np.ndarray):
    """One predict() call on the current window (fit-time standardization)."""
    prices = np.asarray(closes_window, float)
    returns = np.diff(np.log(prices))
    vfeat = _vol_features(returns)
    tfeat = _trend_features(returns)
    vol_post = fit.vol_model.predict_proba((vfeat - fit.vol_mu) / fit.vol_sg)[-1]
    tr_post = fit.trend_model.predict_proba((tfeat - fit.tr_mu) / fit.tr_sg)[-1]
    vol_probs = [float(vol_post[i]) for i in range(4)]
    tr_probs = [float(tr_post[i]) for i in range(4)]
    vs = sum(vol_probs)
    ts = sum(tr_probs)
    if vs > 0:
        vol_probs = [p / vs for p in vol_probs]
    if ts > 0:
        tr_probs = [p / ts for p in tr_probs]
    if not np.all(np.isfinite(vol_probs)):
        vol_probs = [0.25] * 4
    if not np.all(np.isfinite(tr_probs)):
        tr_probs = [0.25] * 4
    vol_state = VOL_LABELS[int(np.argmax(vol_probs))]
    trend_state = TREND_LABELS[int(np.argmax(tr_probs))]
    label = f"{vol_state}_vol_{trend_state}"
    conf = round(float(max(vol_probs) * max(tr_probs)), 4)
    return label, conf, trend_state, vol_state


class RegimeLabelDebouncer:
    """Verbatim semantics of regime_engine.RegimeLabelDebouncer (n_confirm=3)."""

    def __init__(self, n_confirm: int = 3):
        self.n_confirm = max(1, int(n_confirm))
        self._confirmed = None
        self._candidate = None
        self._candidate_count = 0

    def update(self, raw_label: str | None) -> str | None:
        if raw_label is None:
            return self._confirmed
        if self._confirmed is None:
            self._confirmed = raw_label
            self._candidate = raw_label
            self._candidate_count = 0
            return self._confirmed
        if raw_label == self._confirmed:
            self._candidate = self._confirmed
            self._candidate_count = 0
        else:
            if raw_label == self._candidate:
                self._candidate_count += 1
            else:
                self._candidate = raw_label
                self._candidate_count = 1
            if self._candidate_count >= self.n_confirm:
                self._confirmed = self._candidate
                self._candidate_count = 0
        return self._confirmed


def run_hmm_repro(closes: np.ndarray, refit_every: int = 24, window: int = 200) -> dict:
    """Per-bar raw label, debounced label, direction (+1 long/-1 short), conf.

    Bars before `window-1` carry no label (None / NaN).  Deterministic:
    every fit uses random_state=42; refit schedule is absolute in bar index,
    so truncating the series never changes earlier outputs (causal).
    """
    closes = np.asarray(closes, float)
    m = closes.size
    n_start = window - 1
    raw = np.full(m, None, dtype=object)
    conf = np.full(m, np.nan)
    trend_states = np.full(m, None, dtype=object)
    vol_states = np.full(m, None, dtype=object)
    current: _Fit | None = None
    for t in range(n_start, m):
        if current is None or (t - n_start) % refit_every == 0:
            current = _fit_models(closes[t - window + 1: t + 1], t)
        label, c, tstate, vstate = _predict_bar(current, closes[t - window + 1: t + 1])
        raw[t] = label
        conf[t] = c
        trend_states[t] = tstate
        vol_states[t] = vstate
    deb = RegimeLabelDebouncer(3)
    debounced = np.full(m, None, dtype=object)
    direction = np.zeros(m, dtype=int)
    for t in range(m):
        debounced[t] = deb.update(raw[t])
        if debounced[t] is not None:
            direction[t] = -1 if "bear" in debounced[t] else 1
    return {
        "raw_label": raw,
        "debounced_label": debounced,
        "direction": direction,
        "conf": conf,
        "trend_state": trend_states,
        "vol_state": vol_states,
        "n_fits": int((m - 1 - n_start) // refit_every) + 1 if m > n_start else 0,
        "refit_every": refit_every,
    }


def hmm_metrics(closes, hmm_out: dict, er: np.ndarray, horizon: int = 24) -> dict:
    """24h-ahead direction accuracy vs majority baseline + chop diagnostics."""
    closes = np.asarray(closes, float)
    m = closes.size
    d = hmm_out["direction"]
    raw = hmm_out["raw_label"]
    conf = hmm_out["conf"]
    idx = []
    for t in range(m):
        if d[t] == 0 or t + horizon >= m:
            continue
        ch = closes[t + horizon] - closes[t]
        if ch == 0:
            continue
        idx.append(t)
    idx = np.asarray(idx, dtype=int)
    if idx.size == 0:
        return {"n_decided": 0, "acc": float("nan"), "majority": float("nan"),
                "skill": float("nan"), "flip_rate_low_er": float("nan"),
                "flip_rate_high_er": float("nan"), "conf_low_er": float("nan")}
    signs = np.sign(closes[idx + horizon] - closes[idx])
    dirs = d[idx]
    acc = float(np.mean(signs == dirs))
    majority = float(max(np.mean(signs > 0), np.mean(signs < 0)))
    # raw-label hour-to-hour flip rate in bottom- vs top-quartile-ER windows
    lab = np.asarray([r is not None for r in raw])
    flips = np.zeros(m, dtype=bool)
    flips[1:] = [raw[t] is not None and raw[t - 1] is not None and raw[t] != raw[t - 1]
                 for t in range(1, m)]
    okf = lab & np.concatenate(([False], lab[:-1]))
    erf = np.where(np.isfinite(er), er, np.nan)
    q25, q75 = np.nanpercentile(erf[okf], [25, 75])
    low = okf & (erf <= q25)
    high = okf & (erf >= q75)
    flip_low = float(np.mean(flips[low])) if low.any() else float("nan")
    flip_high = float(np.mean(flips[high])) if high.any() else float("nan")
    conf_low = float(np.nanmean(conf[low])) if low.any() else float("nan")
    return {
        "n_decided": int(idx.size),
        "acc": acc,
        "majority": majority,
        "skill": acc - majority,
        "flip_rate_low_er": flip_low,
        "flip_rate_high_er": flip_high,
        "conf_low_er": conf_low,
    }
