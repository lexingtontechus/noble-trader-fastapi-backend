"""features.py — point-in-time regime features (all causal).

Every value at bar t is computed from bars <= t only.  No lookahead anywhere:
rolling windows are trailing, recursions (Wilder smoothing) run forward, and
the no-lookahead property is pinned by tests (full-series vs truncated-series
identity for all t < N).

Conventions
-----------
* arrays are 1-D numpy float64 of equal length (bars in chronological order);
* warm-up regions carry NaN (never backfilled — backfilling IS lookahead);
* `vol_percentile` uses pandas rolling rank (non-NaN observations only).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import lfilter

NAN = float("nan")


def _wilder_smooth(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's smoothing: y[0] = x[0]; y[i] = ((n-1)*y[i-1] + x[i]) / n.

    Equivalent to an EMA with alpha = 1/n seeded at the first observation
    (the standard ATR convention; the geometric transient decays as
    ((n-1)/n)^i and is dead by the 720-bar warm-up used everywhere here).
    Causal by construction.
    """
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x.copy()
    b0 = 1.0 / n
    zi = np.array([(1.0 - b0) * x[0]], dtype=float)
    y, _ = lfilter([b0], [1.0, -(1.0 - b0)], x, zi=zi)
    return y


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True range; TR[0] = high[0] - low[0] (no previous close)."""
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    prev = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    tr[0] = high[0] - low[0]
    return tr


def atr_wilder(high, low, close, n: int = 14) -> np.ndarray:
    """Wilder ATR (causal recursion)."""
    return _wilder_smooth(true_range(high, low, close), n)


def atr_pct(high, low, close, n: int = 14) -> np.ndarray:
    """ATR as a fraction of close."""
    close = np.asarray(close, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return atr_wilder(high, low, close, n) / close


def kaufman_er(close, n: int = 24):
    """Kaufman efficiency ratio and its signed version.

    ER[i]   = |c[i] - c[i-n]| / sum_{j=i-n+1..i} |c[j] - c[j-1]|   in [0, 1]
    er_s[i] = sign(c[i] - c[i-n]) * ER[i]                          in [-1, 1]

    NaN for i < n (insufficient lookback).  Zero denominator (a perfectly flat
    window) yields ER = 0 (no directional efficiency).
    """
    close = np.asarray(close, float)
    m = close.size
    er = np.full(m, NAN)
    er_s = np.full(m, NAN)
    if m <= n:
        return er, er_s
    d = np.abs(np.diff(close))  # d[j] is the change ending at bar j+1
    cs = np.concatenate(([0.0], np.cumsum(d)))
    idx = np.arange(n, m)
    denom = cs[idx] - cs[idx - n]  # sum of the last n diffs ending at bar i
    num = np.abs(close[idx] - close[idx - n])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(denom > 0, num / np.where(denom > 0, denom, 1.0), 0.0)
    ratio = np.clip(ratio, 0.0, 1.0)
    er[idx] = ratio
    er_s[idx] = np.sign(close[idx] - close[idx - n]) * ratio
    return er, er_s


def choppiness(high, low, close, n: int = 14) -> np.ndarray:
    """Choppiness index, normalised to [0, 1] (1 = pure chop).

    CHOP = 100 * log10(sum TR / (max(high, n) - min(low, n))) / log10(n);
    value = CHOP / 100.  NaN for i < n.  Degenerate zero range -> 1.0.
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    m = close.size
    out = np.full(m, NAN)
    if m <= n:
        return out
    tr = true_range(high, low, close)
    cs = np.concatenate(([0.0], np.cumsum(tr)))
    hh = pd.Series(high).rolling(n).max().to_numpy()
    ll = pd.Series(low).rolling(n).min().to_numpy()
    idx = np.arange(n, m)
    s = cs[idx + 1] - cs[idx + 1 - n]
    rng = hh[idx] - ll[idx]
    logn = np.log10(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        val = np.where(rng > 0, np.log10(np.where(rng > 0, s / np.where(rng > 0, rng, 1.0), 1.0)) / logn, 1.0)
    out[idx] = np.clip(val, 0.0, 1.0)
    return out


def adx(high, low, close, n: int = 14) -> np.ndarray:
    """Standard Wilder ADX (causal)."""
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    m = high.size
    if m < 2:
        return np.full(m, NAN)
    up = np.zeros(m)
    dn = np.zeros(m)
    up[1:] = high[1:] - high[:-1]
    dn[1:] = low[:-1] - low[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(high, low, close)
    sm_p = _wilder_smooth(plus_dm, n)
    sm_m = _wilder_smooth(minus_dm, n)
    sm_t = _wilder_smooth(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        di_p = np.where(sm_t > 0, 100.0 * sm_p / np.where(sm_t > 0, sm_t, 1.0), 0.0)
        di_m = np.where(sm_t > 0, 100.0 * sm_m / np.where(sm_t > 0, sm_t, 1.0), 0.0)
    tot = di_p + di_m
    dx = np.where(tot > 0, 100.0 * np.abs(di_p - di_m) / np.where(tot > 0, tot, 1.0), 0.0)
    return _wilder_smooth(dx, n)


def hurst_rolling(close, window: int = 96, ks=(8, 16, 32, 64)) -> np.ndarray:
    """Rolling Hurst exponent via the aggregate-variance method.

    For each bar i, over the trailing `window` closes: variance of k-step
    overlapping log returns for k in `ks`; H = slope(log var, log k) / 2.
    Fully vectorised (4-point least squares across bars).  NaN until the
    trailing `window` k-step returns exist.
    """
    close = np.asarray(close, float)
    m = close.size
    out = np.full(m, NAN)
    ks = np.asarray(ks, dtype=int)
    if m < window + int(ks.max()):
        return out
    lc = np.log(close)
    logk = np.log(ks.astype(float))
    cols = []
    for k in ks:
        retk = np.full(m, NAN)
        retk[k:] = lc[k:] - lc[:-k]
        var = pd.Series(retk).rolling(window, min_periods=window).var(ddof=1).to_numpy()
        cols.append(var)
    V = np.vstack(cols)  # (n_k, m)
    ok = np.all(np.isfinite(V), axis=0) & np.all(V > 0, axis=0)
    idx = np.where(ok)[0]
    if idx.size == 0:
        return out
    Y = np.log(V[:, idx])
    X = (logk - logk.mean())[:, None]
    Yc = Y - Y.mean(axis=0, keepdims=True)
    slope = (X * Yc).sum(axis=0) / (X * X).sum()
    out[idx] = slope / 2.0
    return out


def vr_z(close, q: int = 48, window: int = 240) -> np.ndarray:
    """Lo-MacKinlay variance-ratio z-statistic (homoskedastic null), rolling.

    VR(q) = var(overlapping q-step log returns) / (q * var(1-step returns)),
    both over the trailing `window` returns.  Under the iid null
    VR(q) - 1 = 2*sum_{j=1..q-1}(1 - j/q)*rho_j, so by Bartlett's formula
    Var(sqrt(n)*(VR-1)) -> 4*sum_j (1-j/q)^2 = 2(q-1)(2q-1)/(3q)  (the
    homoskedastic asymptotic variance used here; any other published constant
    only rescales z monotononegly and changes nothing downstream — Spearman,
    bucketing and gates are scale-invariant).
    """
    close = np.asarray(close, float)
    m = close.size
    out = np.full(m, NAN)
    if m < q + 2:
        return out
    lc = np.log(close)
    r1 = np.full(m, NAN)
    r1[1:] = lc[1:] - lc[:-1]
    xq = np.full(m, NAN)
    xq[q:] = lc[q:] - lc[:-q]
    v1 = pd.Series(r1).rolling(window, min_periods=window).var(ddof=1).to_numpy()
    vq = pd.Series(xq).rolling(window, min_periods=window).var(ddof=1).to_numpy()
    phi = 2.0 * (q - 1) * (2 * q - 1) / (3.0 * q)
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where(v1 > 0, vq / (q * np.where(v1 > 0, v1, 1.0)), NAN)
        z = (vr - 1.0) * np.sqrt(window / phi)
    ok = np.isfinite(z)
    out[ok] = z[ok]
    return out


def vol_percentile(atr_pct_: np.ndarray, trailing: int = 720, min_periods: int = 240) -> np.ndarray:
    """Rolling percentile rank of ATR% within its trailing window, in [0, 1].

    pandas rolling rank (pct=True): fraction of the trailing window's
    non-NaN values <= the current value.  Causal; NaN until `min_periods`.
    """
    s = pd.Series(np.asarray(atr_pct_, float))
    return s.rolling(trailing, min_periods=min_periods).rank(pct=True).to_numpy(dtype=float)


def run_len_signed(close, cap: int = 10) -> np.ndarray:
    """Current consecutive same-sign close-to-close run, signed, / cap.

    Value in [-1, 1]: sign of the run * min(run length, cap) / cap.
    Flat bars (zero change) reset the run and contribute 0.
    """
    close = np.asarray(close, float)
    m = close.size
    out = np.zeros(m)
    if m < 2:
        return out
    s = np.sign(np.diff(close))  # s[j]: sign of change ending at bar j+1
    change = np.ones(s.size, dtype=bool)
    change[1:] = s[1:] != s[:-1]
    grp = np.cumsum(change) - 1          # non-decreasing group ids
    n_groups = int(grp[-1]) + 1
    # run length ENDING at j (position within the group — NOT the group's total
    # length, which would leak future continuation of the run)
    first = np.searchsorted(grp, np.arange(n_groups))
    lens = np.arange(s.size) - first[grp] + 1
    val = s * np.minimum(lens, cap) / float(cap)
    out[1:] = val
    return out


def compute_all(high, low, close) -> dict:
    """Bundle all causal features into a dict of arrays."""
    er, er_s = kaufman_er(close, 24)
    return {
        "atr": atr_wilder(high, low, close, 14),
        "atr_pct": atr_pct(high, low, close, 14),
        "er": er,
        "er_signed": er_s,
        "chop": choppiness(high, low, close, 14),
        "adx": adx(high, low, close, 14),
        "hurst": hurst_rolling(close, 96),
        "vr_z": vr_z(close, 48, 240),
        "vol_pct": vol_percentile(atr_pct(high, low, close, 14)),
        "run_len": run_len_signed(close, 10),
    }
