"""recalib.py — Phase 0.5: post-hoc recalibration of RV forecasts.

Mincer-Zarnowitz affine on the CENTER + ONE spread-inflation factor k on the
claimed interval, per (symbol, horizon, arm) cell, fitted on a TRAIN window
of rolling-origin cutoffs and scored on the LATER lockbox cutoffs — never
fit and scored on the same window (signal-quality thesis §4.3, C1/C6;
scope doc 20261019_VOL_CALIBRATION_AND_LLM_SERVER_REPURPOSE.md §1.3).

The measured defect this repairs (run 20261019_010230): coverage80
0.19–0.44 against a nominal 0.80 — stated uncertainty 2–4× too narrow —
while discrimination was real (dir_acc 0.57–0.83). Calibration is the
defect, not discrimination; this module repairs the stated uncertainty.

Method (scope doc §1.3, verbatim recipe)
────────────────────────────────────────
Per cell, on the train cutoffs only:

  1. MZ on the center (log space):
         log(RV) = a + b · log(F) + ε        (OLS)
         F' = exp(a) · F^b
     Diagnostics: b < 1 → over-reaction; b > 1 → under-reaction.

  2. Spread inflation (the overconfidence fix). Standardize the train
     errors against the arm's OWN claimed log-spread:
         σ_f = (log hi80 − log lo80) / (2 · Z80)
         e   = (log RV − log F') / σ_f
         k   = quantile_0.80(|e|) / Z80       (one number per fit level)
     calibrated 80% interval = F' · exp(±Z80 · k · σ_f)

     σ_f is whatever the arm CLAIMS — Chronos/ewma/garch claim the
     normal-approximation half-width Z80·rv/√(2H) (arms.py), Log-HAR claims
     its empirical residual q10/q90. Standardizing by the claim makes k
     construction-agnostic: it repairs an understated CLT interval and a
     mis-spread empirical interval with the same machinery.

  3. Lockbox: parameters are frozen on train; coverage80 + QLIKE are
     scored on the LATER test cutoffs only. Acceptance = the thesis band
     [0.80, 0.95] applied as a ONE-SIDED Wilson test (reject only when
     the lockbox can statistically rule the band out) AND QLIKE not
     degraded beyond tolerance. A point-estimate band at lockbox n would
     fail a PERFECT calibrator ~half the time (nominal 0.80 target).

Evidence ladder (same shape as every fail-open chain in this codebase):
    cell (symbol×horizon×arm, n_train ≥ min_n_cell)
      → pooled (symbol×arm, n_train ≥ min_n_pooled)
      → global (arm, n_train ≥ min_n_pooled)
      → none (identity; a=0, b=1, k=None — no evidence, no calibration)

k is shrunk toward 1.0 with strength shrink_n0 (a prior of n0 observations
at k=1): small samples cannot justify wild inflation. Both raw and shrunk
values are reported; the shrunk value is what ships and what the lockbox
scores.

DYNAMIC-TIER COHERENCE (why this rides the tier pattern)
────────────────────────────────────────────────────────
The four dynamic tiers (dynamic_tier_optimizer.py: dynamic_atr /
dynamic_deadline / regime_edge / geometry_incubation) are fitted on
REALIZED outcomes only — reachability percentiles, resolved nt_signal_sim
rows, walked paths. No tier parameter consumes a Chronos/HAR forecast, so
forecast overconfidence cannot contaminate the tier substrate; recalib
adds the missing (forecast → realized) map on the same realized-outcome
discipline. The param_store row shape (symbol, tier_type, tier_key,
tier_value JSON, n, computed_at) carries a future `vol_recalib` tier with
zero schema change. Recalib cells are keyed (symbol, horizon, arm) and
deliberately NOT vol_tier: vol_tier labels are HMM argmax parses of
regime_label (bracket_reachability._vol_tier) — uncalibrated state
estimates that must earn their own reliability measurement before they
condition anything.

Consumption gate (scope doc §1.3 step 5): ONLY calibrated spreads may
feed the Kelly vol clamp / brick sizing. This module changes no live
path — it is research tooling outside regime_platform, like the rest of
vol_backtest.

CLI
───
    python -m vol_backtest.recalib \
        --run-dir worklog/kairos/reports/vol_backtest/20261019_005157 \
        --run-dir worklog/kairos/reports/vol_backtest/20261019_010230 \
        [--out DIR] [--train-frac 0.7] [--min-n-cell 60] [--min-n-pooled 80]
        [--shrink-n0 20] [--qlike-tol 0.10]

Outputs (in --out, default <first run dir>/recalib):
    recalib.json     versioned artifact — one row per cell, the
                     nt_vol_recalib table shape + lockbox diagnostics
    recalib.csv      the same rows flat
    recalib_report.md human verdict: coverage raw → calibrated on the
                     lockbox, QLIKE raw → calibrated, per-cell k and b
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import metrics as M
from .arms import Z80

EPS = 1e-16

# Parameter guards — these are safety clamps, not estimators.
B_MIN, B_MAX = 0.25, 4.0
A_MIN, A_MAX = -2.0, 2.0
K_MIN, K_MAX = 0.25, 16.0

LEVEL_CELL = "cell"
LEVEL_POOLED = "pooled_symbol_arm"
LEVEL_GLOBAL = "global_arm"
LEVEL_NONE = "none"

COV_LO, COV_HI = 0.80, 0.95  # thesis acceptance band for coverage80


# ─── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CalibratedForecast:
    """Result of applying a calibration map to one forecast."""

    rv: float
    lo80: float | None
    hi80: float | None
    applied: bool   # False → identity passthrough (no cell / bad input)
    level: str


@dataclass(frozen=True)
class CellCalibration:
    """One (symbol, horizon, arm) calibration + its lockbox diagnostics."""

    symbol: str
    horizon: int
    arm: str
    level: str                          # cell | pooled_symbol_arm | global_arm | none
    mz_a: float = 0.0                   # center intercept (log space)
    mz_b: float = 1.0                   # center slope
    spread_k: float | None = None       # inflation factor; None → center-only
    n_fit: int = 0                      # train rows used for the center fit
    n_fit_spread: int = 0               # train rows with a usable claimed σ_f
    n_test: int = 0
    fit_cutoff_lo: int | None = None
    fit_cutoff_hi: int | None = None
    mz_r2: float = float("nan")
    k_raw: float | None = None
    shrunk: bool = False
    # lockbox diagnostics (None when not scoreable)
    cov_raw_train: float | None = None
    cov_cal_train: float | None = None
    cov_raw_test: float | None = None
    cov_cal_test: float | None = None   # the lockbox number that matters
    cov_oos_lo95: float | None = None   # Wilson 95% CI on cov_cal_test
    cov_oos_hi95: float | None = None
    qlike_raw_test: float | None = None
    qlike_cal_test: float | None = None
    accepted: bool | None = None
    run_ids: tuple[str, ...] = ()

    def to_row(self) -> dict[str, Any]:
        """Flat dict — the nt_vol_recalib table shape + diagnostics."""
        return {
            "symbol": self.symbol,
            "horizon_bars": int(self.horizon),
            "arm": self.arm,
            "level": self.level,
            "mz_a": None if math.isnan(self.mz_a) else round(self.mz_a, 6),
            "mz_b": round(self.mz_b, 6),
            "spread_k": None if self.spread_k is None else round(self.spread_k, 6),
            "n_fit": self.n_fit,
            "n_fit_spread": self.n_fit_spread,
            "n_test": self.n_test,
            "fit_cutoff_lo": self.fit_cutoff_lo,
            "fit_cutoff_hi": self.fit_cutoff_hi,
            "mz_r2": None if math.isnan(self.mz_r2) else round(self.mz_r2, 4),
            "k_raw": None if self.k_raw is None else round(self.k_raw, 6),
            "shrunk": self.shrunk,
            "cov_insample": _r3(self.cov_cal_train),
            "cov_oos": _r3(self.cov_cal_test),
            "cov_oos_lo95": _r3(self.cov_oos_lo95),
            "cov_oos_hi95": _r3(self.cov_oos_hi95),
            "cov_raw_test": _r3(self.cov_raw_test),
            "qlike_raw": _r4(self.qlike_raw_test),
            "qlike_cal": _r4(self.qlike_cal_test),
            "accepted": self.accepted,
            "run_ids": ",".join(self.run_ids),
        }


def _r3(x: float | None) -> float | None:
    return None if x is None or math.isnan(x) else round(x, 3)


def _r4(x: float | None) -> float | None:
    return None if x is None or math.isnan(x) else round(x, 4)


@dataclass(frozen=True)
class CalibrationMap:
    """Immutable set of cell calibrations + the parameters that made them."""

    cells: dict[tuple[str, int, str], CellCalibration] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)

    def cell(self, symbol: str, horizon: int, arm: str) -> CellCalibration | None:
        return self.cells.get((symbol, int(horizon), arm))

    def apply(
        self,
        symbol: str,
        horizon: int,
        arm: str,
        forecast: float,
        lo80: float | None = None,
        hi80: float | None = None,
    ) -> CalibratedForecast:
        """Apply the cell's (a, b, k). Identity passthrough when no evidence.

        The interval is rebuilt log-symmetric around the corrected center:
            F' = exp(a) · F^b
            [F'·exp(−Z80·k·σ_f), F'·exp(+Z80·k·σ_f)]   with the caller's OWN
            claimed σ_f = (log hi80 − log lo80) / (2·Z80).
        """
        c = self.cell(symbol, horizon, arm)
        f = _finite_pos(forecast)
        if c is None or c.level == LEVEL_NONE or f is None:
            return CalibratedForecast(
                rv=forecast if _finite_pos(forecast) is not None else float("nan"),
                lo80=lo80, hi80=hi80,
                applied=False, level=c.level if c else LEVEL_NONE,
            )
        fp = math.exp(c.mz_a) * (f ** c.mz_b)
        if not math.isfinite(fp) or fp <= 0:
            return CalibratedForecast(
                rv=forecast, lo80=lo80, hi80=hi80, applied=False, level=c.level,
            )
        lo, hi = lo80, hi80
        if c.spread_k is not None:
            s = _sigma_f_one(lo80, hi80)
            if s is not None and math.isfinite(s) and s > 0:
                half = Z80 * c.spread_k * s
                lo = fp * math.exp(-half)
                hi = fp * math.exp(half)
            else:
                lo, hi = None, None
        else:
            lo, hi = None, None
        return CalibratedForecast(rv=fp, lo80=lo, hi80=hi, applied=True, level=c.level)


# ─── Core math ───────────────────────────────────────────────────────────────


def _finite_pos(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _sigma_f_one(lo: Any, hi: Any) -> float | None:
    """Claimed log-spread σ_f of one interval, first-order in log space."""
    lo_v = _finite_pos(lo)
    hi_v = _finite_pos(hi)
    if lo_v is None or hi_v is None or hi_v <= lo_v:
        return None
    return (math.log(hi_v) - math.log(lo_v)) / (2.0 * Z80)


def _sigma_f(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Vectorized claimed σ_f; NaN where the interval is missing/degenerate."""
    lo = np.asarray(lo, dtype="float64")
    hi = np.asarray(hi, dtype="float64")
    ok = np.isfinite(lo) & np.isfinite(hi) & (lo > 0) & (hi > lo)
    out = np.full(len(lo), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[ok] = (np.log(hi[ok]) - np.log(lo[ok])) / (2.0 * Z80)
    return out


def _mz_fit(log_f: np.ndarray, log_rv: np.ndarray) -> tuple[float, float, float]:
    """OLS log_rv ~ a + b·log_f. Returns (a, b, r²); identity on degenerate x.

    b is clamped to [B_MIN, B_MAX], a to [A_MIN, A_MAX] — guards, not
    estimators: a wild fit is not allowed to become a wild correction.
    """
    n = len(log_f)
    if n < 8 or float(np.std(log_f)) < 1e-12:
        return 0.0, 1.0, float("nan")
    A = np.column_stack([np.ones(n), log_f])
    try:
        beta, *_ = np.linalg.lstsq(A, log_rv, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0, 1.0, float("nan")
    a, b = float(beta[0]), float(beta[1])
    if not (math.isfinite(a) and math.isfinite(b)):
        return 0.0, 1.0, float("nan")
    b = min(B_MAX, max(B_MIN, b))
    a = min(A_MAX, max(A_MIN, a))
    resid = log_rv - (a + b * log_f)
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((log_rv - np.mean(log_rv)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return a, b, r2


def _fit_spread_k(
    e_abs: np.ndarray,
    *,
    shrink_n0: float,
    k_prior: float,
    min_e: int,
) -> tuple[float | None, float | None, bool]:
    """k = q80(|e|)/Z80 with shrinkage toward k_prior.

    Returns (k_used, k_raw, shrunk); (None, None, False) below min_e —
    spread calibration needs its own evidence count, independent of the
    center's.
    """
    e = np.asarray(e_abs, dtype="float64")
    e = e[np.isfinite(e) & (e >= 0)]
    if len(e) < min_e:
        return None, None, False
    k_raw = float(np.quantile(e, 0.80)) / Z80
    if not math.isfinite(k_raw) or k_raw <= 0:
        return None, None, False
    if shrink_n0 > 0:
        k = (len(e) * k_raw + shrink_n0 * k_prior) / (len(e) + shrink_n0)
        shrunk = True
    else:
        k, shrunk = k_raw, False
    k = min(K_MAX, max(K_MIN, k))
    return float(k), float(k_raw), shrunk


# ─── Records loading / splitting ─────────────────────────────────────────────


def load_records(run_dirs: list[str | Path]) -> "Any":
    """Merge records.csv across run dirs, deduping (symbol,horizon,arm,cutoff).

    Later run dirs win collisions (they are the same bars re-pulled —
    verified on 005157 vs 010230: identical har forecasts on all 59 common
    cutoffs). Provenance is kept per row via run_id.
    """
    import pandas as pd

    frames = []
    for d in run_dirs:
        p = Path(d) / "records.csv"
        if not p.exists():
            raise FileNotFoundError(f"records.csv not found in {d}")
        df = pd.read_csv(p)
        df["run_id"] = p.parent.name
        frames.append(df)
    if not frames:
        raise ValueError("no run dirs given")
    merged = pd.concat(frames, ignore_index=True)
    before = len(merged)
    # Later run dirs win collisions (row-wise: drop_duplicates does NOT
    # skip NaN per column the way groupby.last() does, so a newer row with
    # a missing interval cannot resurrect an older row's interval).
    merged = merged.sort_values("run_id", kind="stable")
    merged = merged.drop_duplicates(
        subset=["symbol", "horizon", "arm", "cutoff"], keep="last"
    )
    merged = merged.sort_values(["symbol", "horizon", "arm", "cutoff"], kind="stable")
    merged.attrs["deduped_from"] = before
    merged.attrs["run_ids"] = sorted({f for fr in frames for f in fr["run_id"].unique()})
    return merged


def _usable(df: "Any") -> "Any":
    """Rows with a finite positive forecast and a finite actual."""
    import numpy as _np

    ok = (
        _np.isfinite(df["forecast"].to_numpy("float64"))
        & _np.isfinite(df["actual"].to_numpy("float64"))
        & (df["forecast"].to_numpy("float64") > 0)
        & (df["actual"].to_numpy("float64") >= 0)
    )
    return df[ok]


def _split_train_test(g: "Any", train_frac: float) -> tuple["Any", "Any"]:
    """Time-ordered split by cutoff — the lockbox discipline."""
    g = g.sort_values("cutoff", kind="stable")
    n = len(g)
    if n < 4:
        return g, g.iloc[0:0]
    k = int(math.ceil(train_frac * n))
    k = min(max(k, 1), n - 1)  # always leave at least one test row
    return g.iloc[:k], g.iloc[k:]


# ─── Fitting ─────────────────────────────────────────────────────────────────


def _fit_level(
    train_frames: list["Any"],
    *,
    shrink_n0: float,
    k_prior: float,
    min_e: int,
) -> tuple[float, float, float | None, float | None, bool, int, int, float]:
    """Fit (a, b, k) on the concatenation of the given train frames.

    Returns (a, b, k, k_raw, shrunk, n_center, n_spread, r2).
    """
    import pandas as pd

    if not train_frames:
        return 0.0, 1.0, None, None, False, 0, 0, float("nan")
    df = pd.concat(train_frames, ignore_index=True) if len(train_frames) > 1 else train_frames[0]
    log_f = np.log(df["forecast"].to_numpy("float64"))
    log_rv = np.log(np.maximum(df["actual"].to_numpy("float64"), EPS))
    a, b, r2 = _mz_fit(log_f, log_rv)
    n_center = int(len(df))
    # spread: standardize train errors against each row's OWN claimed σ_f
    s = _sigma_f(df["lo80"].to_numpy("float64"), df["hi80"].to_numpy("float64"))
    ok = np.isfinite(s) & (s > 0)
    n_spread = int(np.sum(ok))
    k = k_raw = None
    shrunk = False
    if n_spread > 0:
        e = (log_rv[ok] - (a + b * log_f[ok])) / s[ok]
        k, k_raw, shrunk = _fit_spread_k(
            np.abs(e), shrink_n0=shrink_n0, k_prior=k_prior, min_e=min_e
        )
    return a, b, k, k_raw, shrunk, n_center, n_spread, r2


def _wilson95(hits: int, n: int) -> tuple[float, float] | None:
    """Wilson 95% interval for a binomial proportion; None on n=0.

    The lockbox verdict is a POINT estimate of coverage; with small n_test
    (35 rows for the pooled chronos cells) a perfectly calibrated cell
    lands below 0.80 about half the time by sampling noise alone. The CI
    is reported next to every verdict so "0.78 with CI [0.66, 0.87]" is
    not misread as "still broken".
    """
    if n <= 0:
        return None
    z = 1.959963984540054
    p = hits / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return center - half, center + half


def _score(
    g: "Any",
    a: float,
    b: float,
    k: float | None,
) -> dict[str, Any]:
    """Apply (a, b, k) to a records frame; return raw/cal coverage + QLIKE."""
    f = g["forecast"].to_numpy("float64")
    rv = g["actual"].to_numpy("float64")
    lo = g["lo80"].to_numpy("float64")
    hi = g["hi80"].to_numpy("float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        fp = np.exp(a) * np.power(f, b)
    fp = np.where(np.isfinite(fp) & (fp > 0), fp, np.nan)
    out: dict[str, Any] = {
        "qlike_raw": M.qlike(f, rv),
        "qlike_cal": M.qlike(fp, rv),
    }
    has_interval = np.isfinite(lo) & np.isfinite(hi)
    if np.any(has_interval):
        out["cov_raw"] = M.interval_coverage(lo, hi, rv)
        out["n_raw_int"] = int(np.sum(has_interval))
        out["hits_raw"] = int(
            np.sum((rv[has_interval] >= lo[has_interval]) & (rv[has_interval] <= hi[has_interval]))
        )
    else:
        out["cov_raw"] = None
        out["n_raw_int"] = 0
        out["hits_raw"] = 0
    if k is None:
        out["cov_cal"] = None
        out["n_cal_int"] = 0
        out["hits_cal"] = 0
    else:
        s = _sigma_f(lo, hi)
        ok = np.isfinite(fp) & np.isfinite(s) & (s > 0)
        if np.any(ok):
            half = Z80 * k * s[ok]
            cal_lo = fp[ok] * np.exp(-half)
            cal_hi = fp[ok] * np.exp(half)
            out["cov_cal"] = M.interval_coverage(cal_lo, cal_hi, rv[ok])
            out["n_cal_int"] = int(np.sum(ok))
            out["hits_cal"] = int(
                np.sum((rv[ok] >= cal_lo) & (rv[ok] <= cal_hi))
            )
        else:
            out["cov_cal"] = None
            out["n_cal_int"] = 0
            out["hits_cal"] = 0
    return out


def fit_calibration_map(
    records: "Any",
    *,
    train_frac: float = 0.7,
    min_n_cell: int = 60,
    min_n_pooled: int = 80,
    min_e: int = 20,
    shrink_n0: float = 20.0,
    k_prior: float = 1.0,
    qlike_tol: float = 0.10,
) -> CalibrationMap:
    """Fit the whole map: split → ladder → (a, b, k) → lockbox diagnostics.

    The ladder is per cell: cell → pooled (symbol, arm) → global (arm) →
    none. Pooled/global fits use ONLY the train rows of the cells that
    fell through, so the borrowed parameters describe those horizons.
    """
    df = _usable(records)
    cells: dict[tuple[str, int, str], CellCalibration] = {}
    if df.empty:
        return CalibrationMap(cells=cells, params=_params_dict(locals()))

    splits: dict[tuple[str, int, str], tuple["Any", "Any"]] = {}
    cell_runs: dict[tuple[str, int, str], tuple[str, ...]] = {}
    for (sym, H, arm), g in df.groupby(["symbol", "horizon", "arm"], sort=True):
        key = (str(sym), int(H), str(arm))
        splits[key] = _split_train_test(g, train_frac)
        if "run_id" in g:
            cell_runs[key] = tuple(sorted({str(x) for x in g["run_id"].unique()}))

    # ---- level resolution -------------------------------------------------
    level_of: dict[tuple[str, int, str], str] = {}
    for key, (tr, _te) in splits.items():
        level_of[key] = LEVEL_CELL if len(tr) >= min_n_cell else ""

    # pooled (symbol, arm) from the cells that fell through cell level
    pooled_fits: dict[tuple[str, str], tuple] = {}
    fallthrough = [k for k, lv in level_of.items() if lv == ""]
    by_sym_arm: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    for key in fallthrough:
        by_sym_arm.setdefault((key[0], key[2]), []).append(key)
    for (sym, arm), keys in sorted(by_sym_arm.items()):
        frames = [splits[k][0] for k in keys]
        n = sum(len(fr) for fr in frames)
        if n >= min_n_pooled:
            pooled_fits[(sym, arm)] = _fit_level(
                frames, shrink_n0=shrink_n0, k_prior=k_prior, min_e=min_e
            )
            for k in keys:
                level_of[k] = LEVEL_POOLED

    # global (arm) from whatever is still unresolved
    global_fits: dict[str, tuple] = {}
    still = [k for k, lv in level_of.items() if lv == ""]
    by_arm: dict[str, list[tuple[str, int, str]]] = {}
    for key in still:
        by_arm.setdefault(key[2], []).append(key)
    for arm, keys in sorted(by_arm.items()):
        frames = [splits[k][0] for k in keys]
        n = sum(len(fr) for fr in frames)
        if n >= min_n_pooled:
            global_fits[arm] = _fit_level(
                frames, shrink_n0=shrink_n0, k_prior=k_prior, min_e=min_e
            )
            for k in keys:
                level_of[k] = LEVEL_GLOBAL
    for k, lv in level_of.items():
        if lv == "":
            level_of[k] = LEVEL_NONE

    # ---- assemble cells + lockbox diagnostics ------------------------------
    for key in sorted(splits):
        sym, H, arm = key
        tr, te = splits[key]
        lv = level_of[key]
        if lv == LEVEL_CELL:
            a, b, k, k_raw, shrunk, n_c, n_s, r2 = _fit_level(
                [tr], shrink_n0=shrink_n0, k_prior=k_prior, min_e=min_e
            )
        elif lv == LEVEL_POOLED:
            a, b, k, k_raw, shrunk, n_c, n_s, r2 = pooled_fits[(sym, arm)]
        elif lv == LEVEL_GLOBAL:
            a, b, k, k_raw, shrunk, n_c, n_s, r2 = global_fits[arm]
        else:
            a, b, k, k_raw, shrunk, n_c, n_s, r2 = 0.0, 1.0, None, None, False, 0, 0, float("nan")

        sc_tr = _score(tr, a, b, k) if len(tr) else {}
        sc_te = _score(te, a, b, k) if len(te) else {}
        run_ids = cell_runs.get(key, ())

        cov_cal_test = sc_te.get("cov_cal")
        wil = _wilson95(int(sc_te.get("hits_cal", 0)), int(sc_te.get("n_cal_int", 0)))
        q_raw = sc_te.get("qlike_raw")
        q_cal = sc_te.get("qlike_cal")
        accepted = None
        if len(te) > 0 and k is not None and cov_cal_test is not None and wil is not None:
            # Acceptance = the thesis band [0.80, 0.95] applied as a
            # ONE-SIDED Wilson test, not a point-estimate gate: a perfectly
            # calibrated cell targets 0.80 nominal, so at lockbox n a point
            # estimate lands below 0.80 about half the time by sampling
            # noise alone. Reject only when the data can RULE OUT adequate
            # coverage (CI upper < 0.80) or rule out ≤ 0.95 (CI lower
            # > 0.95) — catastrophes (0.19–0.44) still reject hard.
            q_tol_ref = q_raw if q_raw is not None and math.isfinite(q_raw) and q_raw > 0 else EPS
            accepted = bool(
                wil[1] >= COV_LO
                and wil[0] <= COV_HI
                and q_cal is not None
                and math.isfinite(q_cal)
                and q_cal <= q_tol_ref * (1.0 + qlike_tol)
            )

        cuts_tr = tr["cutoff"].to_numpy()
        cells[key] = CellCalibration(
            symbol=sym, horizon=int(H), arm=arm, level=lv,
            mz_a=a, mz_b=b, spread_k=k,
            n_fit=n_c, n_fit_spread=n_s, n_test=int(len(te)),
            fit_cutoff_lo=int(cuts_tr.min()) if len(cuts_tr) else None,
            fit_cutoff_hi=int(cuts_tr.max()) if len(cuts_tr) else None,
            mz_r2=r2, k_raw=k_raw, shrunk=shrunk,
            cov_raw_train=sc_tr.get("cov_raw"), cov_cal_train=sc_tr.get("cov_cal"),
            cov_raw_test=sc_te.get("cov_raw"), cov_cal_test=cov_cal_test,
            cov_oos_lo95=wil[0] if wil else None,
            cov_oos_hi95=wil[1] if wil else None,
            qlike_raw_test=q_raw, qlike_cal_test=q_cal,
            accepted=accepted, run_ids=run_ids,
        )

    return CalibrationMap(
        cells=cells,
        params=_params_dict(
            {
                "train_frac": train_frac, "min_n_cell": min_n_cell,
                "min_n_pooled": min_n_pooled, "min_e": min_e,
                "shrink_n0": shrink_n0, "k_prior": k_prior,
                "qlike_tol": qlike_tol,
            }
        ),
    )


def _params_dict(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if isinstance(v, (int, float, str, bool))}


# ─── Artifacts ───────────────────────────────────────────────────────────────


def write_artifacts(cmap: CalibrationMap, out_dir: str | Path) -> dict[str, Path]:
    """Write recalib.json / recalib.csv / recalib_report.md."""
    import pandas as pd

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = [c.to_row() for c in cmap.cells.values()]
    df = pd.DataFrame(rows)

    jpath = out / "recalib.json"
    jpath.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "description": (
                    "Phase 0.5 vol-forecast recalibration — MZ center (a, b) + "
                    "spread inflation k, lockbox-scored. nt_vol_recalib-shaped "
                    "rows; JSON artifact first, table when an arm earns it."
                ),
                "params": cmap.params,
                "n_cells": len(rows),
                "cells": [
                    {**r, "run_ids": r["run_ids"].split(",") if r["run_ids"] else []}
                    for r in rows
                ],
            },
            indent=2,
        )
    )
    cpath = out / "recalib.csv"
    df.to_csv(cpath, index=False)
    rpath = out / "recalib_report.md"
    rpath.write_text(_report_md(cmap, rows))
    return {"json": jpath, "csv": cpath, "report": rpath}


def _report_md(cmap: CalibrationMap, rows: list[dict[str, Any]]) -> str:
    p = cmap.params
    scoreable = [r for r in rows if r["accepted"] is not None]
    n_acc = sum(1 for r in scoreable if r["accepted"])
    lines = [
        "# Phase 0.5 — RV forecast recalibration (MZ + spread k)",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{len(rows)} cells · params: train_frac={p.get('train_frac')}, "
        f"min_n_cell={p.get('min_n_cell')}, min_n_pooled={p.get('min_n_pooled')}, "
        f"shrink_n0={p.get('shrink_n0')}, qlike_tol={p.get('qlike_tol')}",
        "",
        "Fit on TRAIN cutoffs, scored on LATER lockbox cutoffs (never both).",
        "Acceptance: the thesis band [0.80, 0.95] as a ONE-SIDED Wilson "
        "test (reject only when the CI rules the band out) AND QLIKE not "
        f"degraded > {float(p.get('qlike_tol', 0.10)) * 100:.0f}%.",
        "",
        "## Lockbox verdict per cell",
        "",
        "| symbol | H | arm | level | n_fit | n_test | b | k | "
        "cov raw→cal (test) | cov 95% CI | qlike raw→cal (test) | verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["arm"], x["symbol"], x["horizon_bars"])):
        cov = "→".join(
            "-" if v is None else f"{v:.2f}" for v in (r["cov_raw_test"], r["cov_oos"])
        )
        ci = "-"
        if r["cov_oos_lo95"] is not None and r["cov_oos_hi95"] is not None:
            ci = f"[{r['cov_oos_lo95']:.2f}, {r['cov_oos_hi95']:.2f}]"
        q = "→".join(
            "-" if v is None else f"{v:.4f}" for v in (r["qlike_raw"], r["qlike_cal"])
        )
        verdict = (
            "ACCEPT" if r["accepted"] else
            "reject" if r["accepted"] is not None else "n/s"
        )
        b_str = "-" if r["mz_b"] is None else f"{r['mz_b']:.2f}"
        k_str = "-" if r["spread_k"] is None else f"{r['spread_k']:.2f}"
        lines.append(
            f"| {r['symbol']} | {r['horizon_bars']} | {r['arm']} | {r['level']} | "
            f"{r['n_fit']} | {r['n_test']} | {b_str} | {k_str} | "
            f"{cov} | {ci} | {q} | {verdict} |"
        )
    lines += [
        "",
        f"**{n_acc}/{len(scoreable)}** scoreable cells accepted "
        f"({len(rows) - len(scoreable)} not scoreable: no test rows, no "
        "claimed interval, or below the evidence ladder).",
        "",
        "## Reading notes",
        "",
        "- `b` < 1 → the center over-reacts; b > 1 → under-reacts (MZ slope).",
        "- `k` is the spread inflation on the arm's OWN claimed σ_f; the "
        "calibrated interval is F'·exp(±Z80·k·σ_f) — log-symmetric by "
        "construction.",
        "- `level` shows where the evidence came from: `cell` = per "
        "(symbol×horizon×arm); `pooled_symbol_arm`/`global_arm` = borrowed "
        "from a wider window (horizon-mixed for the center, σ_f-standardized "
        "for k); `none` = identity, no calibration.",
        "- The verdict is a POINT estimate against the band [0.80, 0.95]. "
        "With small n_test a perfectly calibrated cell can land below 0.80 "
        "by sampling noise alone — read `cov 95% CI` next to it: 0.78 with "
        "CI [0.66, 0.87] is sampling noise; 0.55 is a real defect.",
        "- Honest boundaries: per-regime k and per-quantile (asymmetric) "
        "mapping need more history than these runs carry; QLIKE remains the "
        "loss of record, so a calibrated arm that still loses calibrated-HAR "
        "on QLIKE/DM does not ship.",
        "- Nothing here changes any live path. Consumption gate (scope doc "
        "§1.3 step 5): ONLY calibrated spreads feed Kelly vol clamp / brick "
        "sizing.",
        "",
    ]
    return "\n".join(lines)


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m vol_backtest.recalib",
        description="Phase 0.5 recalibration: fit MZ(a,b) + spread k on train "
                    "cutoffs, lockbox-score on later cutoffs.",
    )
    ap.add_argument(
        "--run-dir", action="append", required=True,
        help="vol_backtest run dir containing records.csv (repeatable; "
             "later dirs win cutoff collisions)",
    )
    ap.add_argument(
        "--out", default=None,
        help="output dir (default: <first run dir>/recalib)",
    )
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--min-n-cell", type=int, default=60)
    ap.add_argument("--min-n-pooled", type=int, default=80)
    ap.add_argument("--min-e", type=int, default=20)
    ap.add_argument("--shrink-n0", type=float, default=20.0)
    ap.add_argument("--qlike-tol", type=float, default=0.10)
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else Path(args.run_dir[0]) / "recalib"

    records = load_records([Path(d) for d in args.run_dir])
    dedup = records.attrs.get("deduped_from", len(records))
    cmap = fit_calibration_map(
        records,
        train_frac=args.train_frac,
        min_n_cell=args.min_n_cell,
        min_n_pooled=args.min_n_pooled,
        min_e=args.min_e,
        shrink_n0=args.shrink_n0,
        qlike_tol=args.qlike_tol,
    )
    paths = write_artifacts(cmap, out)

    scoreable = [c for c in cmap.cells.values() if c.accepted is not None]
    n_acc = sum(1 for c in scoreable if c.accepted)
    print(f"records: {dedup} rows in, {len(records)} after cutoff dedupe")
    print(f"cells: {len(cmap.cells)} | scoreable: {len(scoreable)} | "
          f"accepted: {n_acc}")
    print(f"artifacts: {paths['json'].name}, {paths['csv'].name}, "
          f"{paths['report'].name} -> {out}")
    for c in sorted(cmap.cells.values(), key=lambda c: (c.arm, c.symbol, c.horizon)):
        cov = "-" if c.cov_cal_test is None else f"{c.cov_cal_test:.2f}"
        cov0 = "-" if c.cov_raw_test is None else f"{c.cov_raw_test:.2f}"
        k = "-" if c.spread_k is None else f"{c.spread_k:.2f}"
        print(f"  {c.symbol:>7} H{c.horizon:<3} {c.arm:<13} {c.level:<17} "
              f"b={c.mz_b:.2f} k={k} cov_test {cov0}->{cov} "
              f"n=({c.n_fit}/{c.n_test})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
