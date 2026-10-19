"""dm.py — Diebold-Mariano tests with Newey-West HAC variance + Holm correction.

Why HAC: with cutoffs every `step` bars and forecasts spanning `horizon`
bars, consecutive forecast errors OVERLAP (horizon > step) and the plain DM
variance is wrong. Following the standard remedy, the lag is
``hac_lag = max(0, horizon // step - 1)``.

Why Holm: the benchmark compares K arms against one or two references per
symbol — K independent-ish tests. Holm-Bonferroni controls the family-wise
error rate and is reported alongside the raw p-value (the repo convention:
flag anything not significant at 0.05 after correction).

Reference: Diebold & Mariano (1995); Brini (2026) applies exactly this
machinery (DM + MCS) to TSFM vs econometric RV forecasting, arXiv 2607.05291.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass
class DMResult:
    arm_a: str
    arm_b: str
    n: int
    mean_loss_diff: float      # mean(L_a) - mean(L_b); negative => a better
    dm_stat: float             # standard normal under H0
    p_value: float
    hac_lag: int

    @property
    def a_better(self) -> bool:
        """Raw (uncorrected) significance in a's favour."""
        return bool(self.mean_loss_diff < 0 and self.p_value < 0.05)

    def as_dict(self) -> dict:
        return {
            "arm_a": self.arm_a,
            "arm_b": self.arm_b,
            "n": self.n,
            "mean_loss_diff": self.mean_loss_diff,
            "dm_stat": self.dm_stat,
            "p_value": self.p_value,
            "hac_lag": self.hac_lag,
        }


def _newey_west_variance(d: np.ndarray, lag: int) -> float:
    """HAC variance of the mean of `d` with Bartlett weights (lag L).

    V = g0 + 2 * sum_{j=1..L} (1 - j/(L+1)) * gj,  gj = autocov at lag j.
    """
    n = len(d)
    if n < 2:
        return float("nan")
    dbar = float(np.mean(d))
    xc = d - dbar
    g0 = float(np.dot(xc, xc)) / n
    v = g0
    for j in range(1, min(lag, n - 1) + 1):
        gj = float(np.dot(xc[j:], xc[:-j])) / n
        v += 2.0 * (1.0 - j / (lag + 1.0)) * gj
    return v / n


def dm_test(
    losses_a: np.ndarray,
    losses_b: np.ndarray,
    *,
    hac_lag: int = 0,
    name_a: str = "a",
    name_b: str = "b",
) -> DMResult:
    """Two-sided DM test of equal predictive accuracy (QLIKE or any loss).

    d_t = L_a,t - L_b,t;  DM = mean(d) / sqrt(HAC var of mean(d)).
    Negative mean_loss_diff => arm a has LOWER loss (better).
    NaNs (failed forecasts) are dropped pairwise.
    """
    a = np.asarray(losses_a, dtype=np.float64)
    b = np.asarray(losses_b, dtype=np.float64)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ok = np.isfinite(a) & np.isfinite(b)
    d = a[ok] - b[ok]
    n_ok = len(d)
    if n_ok < 5:
        return DMResult(name_a, name_b, n_ok, float("nan"), float("nan"), 1.0, hac_lag)

    v = _newey_west_variance(d, hac_lag)
    if not np.isfinite(v) or v <= 0:
        return DMResult(name_a, name_b, n_ok, float(np.mean(d)), float("nan"), 1.0, hac_lag)

    dm = float(np.mean(d)) / float(np.sqrt(v))
    p = float(2.0 * stats.norm.sf(abs(dm)))
    return DMResult(name_a, name_b, n_ok, float(np.mean(d)), dm, p, hac_lag)


def holm_adjust(p_values: np.ndarray) -> np.ndarray:
    """Holm-Bonferroni adjusted p-values (family-wise alpha control).

    Returns adjusted p-values in the ORIGINAL order. NaN p-values pass
    through as NaN (treated as never-significant).
    """
    p = np.asarray(p_values, dtype=np.float64)
    out = np.full(len(p), np.nan)
    idx = np.where(np.isfinite(p))[0]
    if len(idx) == 0:
        return out
    order = idx[np.argsort(p[idx])]           # ascending finite p
    m = len(order)
    running_max = 0.0
    for rank, i in enumerate(order):
        adj = (m - rank) * p[i]
        running_max = max(running_max, adj)
        out[i] = min(1.0, running_max)
    return out
