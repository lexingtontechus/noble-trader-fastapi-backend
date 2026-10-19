"""metrics.py — forecast losses and scores for realized-volatility targets.

Choices (corrected methodology, 2026-10-18 review + Brini 2026, arXiv 2607.05291):

  * **QLIKE** is the primary loss — robust to near-zero realized vol
    (weekends, dead hours), standard in the RV literature. MAPE is NOT used.
  * MAE / RMSE are reported on **log(RV)** scale so pooled cross-symbol
    comparisons are not dominated by BTC's ~5x vol scale.
  * Directional accuracy is reported **against the persistence rule**
    ("vol tomorrow = vol today"), NOT against a coin flip — realized vol is
    highly persistent and the coin-flip null overstates skill.
  * 80% central interval coverage: acceptance band [0.80, 0.95] (thesis).

All functions take 1-D float arrays of forecasts (F) and realized values (RV)
on the per-horizon sqrt(sum r^2) scale, plus an optional eps floor.
"""
from __future__ import annotations

import numpy as np

EPS = 1e-16


def _clean(f: np.ndarray, rv: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    f = np.asarray(f, dtype=np.float64)
    rv = np.asarray(rv, dtype=np.float64)
    ok = np.isfinite(f) & np.isfinite(rv) & (f > 0) & (rv >= 0)
    return f[ok], rv[ok]


def qlike(f: np.ndarray, rv: np.ndarray, eps: float = EPS) -> float:
    """Mean QLIKE loss: rv/F - log(rv/F) - 1  (>= 0, equal-zero iff F == rv).

    Symmetric-ish in ratio space, penalizes under-forecasting of vol more
    (rv/F term), which is the risk-relevant direction.
    """
    f, rv = _clean(f, rv)
    if len(f) == 0:
        return float("nan")
    r = np.maximum(rv, eps) / np.maximum(f, eps)
    return float(np.mean(r - np.log(r) - 1.0))


def mae_log(f: np.ndarray, rv: np.ndarray, eps: float = EPS) -> float:
    """MAE on log(RV) scale."""
    f, rv = _clean(f, rv)
    if len(f) == 0:
        return float("nan")
    return float(np.mean(np.abs(np.log(np.maximum(f, eps)) - np.log(np.maximum(rv, eps)))))


def rmse_log(f: np.ndarray, rv: np.ndarray, eps: float = EPS) -> float:
    """RMSE on log(RV) scale."""
    f, rv = _clean(f, rv)
    if len(f) == 0:
        return float("nan")
    d = np.log(np.maximum(f, eps)) - np.log(np.maximum(rv, eps))
    return float(np.sqrt(np.mean(d * d)))


def mae(f: np.ndarray, rv: np.ndarray) -> float:
    """MAE on the raw RV scale (per-symbol; do not pool across symbols)."""
    f, rv = _clean(f, rv)
    if len(f) == 0:
        return float("nan")
    return float(np.mean(np.abs(f - rv)))


def directional_accuracy(
    f: np.ndarray,
    rv: np.ndarray,
    prev_rv: np.ndarray,
) -> float:
    """Share of correctly-predicted vol direction (up/down vs previous period).

    Forecast direction = sign(F_t - prev_RV_t); outcome = sign(RV_t - prev_RV_t).
    Outcome ties (RV == prev) are excluded from the denominator. A forecast
    that exactly equals prev (the naive arm) counts as a MISS — the naive
    forecast carries no directional content, which is the honest reading.
    """
    f = np.asarray(f, dtype=np.float64)
    rv = np.asarray(rv, dtype=np.float64)
    prev = np.asarray(prev_rv, dtype=np.float64)
    ok = np.isfinite(f) & np.isfinite(rv) & np.isfinite(prev)
    f, rv, prev = f[ok], rv[ok], prev[ok]
    actual_up = rv - prev
    nz = actual_up != 0
    if np.sum(nz) == 0:
        return float("nan")
    pred_up = (f - prev)[nz]
    hits = np.sign(pred_up) == np.sign(actual_up[nz])
    return float(np.mean(hits))


def persistence_direction_accuracy(
    rv: np.ndarray,
    prev_rv: np.ndarray,
    prev2_rv: np.ndarray,
) -> float:
    """The honest directional null: "tomorrow's vol direction repeats
    today's" — sign(RV_t - prev_t) vs sign(prev_t - prev2_t).

    On persistent vol this rule typically scores well above 50%, so the
    thesis's "55% vs coin flip" bar must be read against THIS number on the
    same sample. Outcome and signal ties excluded from the denominator.
    """
    rv = np.asarray(rv, dtype=np.float64)
    prev = np.asarray(prev_rv, dtype=np.float64)
    prev2 = np.asarray(prev2_rv, dtype=np.float64)
    ok = np.isfinite(rv) & np.isfinite(prev) & np.isfinite(prev2)
    rv, prev, prev2 = rv[ok], prev[ok], prev2[ok]
    actual_up = rv - prev
    today_up = prev - prev2
    nz = (actual_up != 0) & (today_up != 0)
    if np.sum(nz) == 0:
        return float("nan")
    hits = np.sign(actual_up[nz]) == np.sign(today_up[nz])
    return float(np.mean(hits))


def interval_coverage(
    lo: np.ndarray,
    hi: np.ndarray,
    rv: np.ndarray,
    nominal: float = 0.80,
) -> float:
    """Empirical coverage of a central prediction interval [lo, hi]."""
    lo = np.asarray(lo, dtype=np.float64)
    hi = np.asarray(hi, dtype=np.float64)
    rv = np.asarray(rv, dtype=np.float64)
    ok = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(rv)
    if np.sum(ok) == 0:
        return float("nan")
    inside = (rv[ok] >= lo[ok]) & (rv[ok] <= hi[ok])
    return float(np.mean(inside))


def pinball_loss(quantiles: dict[float, np.ndarray], rv: np.ndarray) -> float:
    """Mean pinball (quantile) loss across supplied quantile levels —
    proper scoring for probabilistic vol forecasts (secondary metric)."""
    total, n = 0.0, 0
    rv = np.asarray(rv, dtype=np.float64)
    for q, f in quantiles.items():
        f = np.asarray(f, dtype=np.float64)
        ok = np.isfinite(f) & np.isfinite(rv)
        if np.sum(ok) == 0:
            continue
        d = rv[ok] - f[ok]
        total += float(np.mean(np.maximum(q * d, (q - 1.0) * d)))
        n += 1
    return total / n if n else float("nan")
