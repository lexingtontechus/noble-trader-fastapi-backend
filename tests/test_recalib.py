"""test_recalib.py — Phase 0.5 recalibration tests.

Only numpy/pandas/pytest. Everything is synthetic with fixed seeds — no
models, no services, no live path. Covers the load-bearing properties:

  * MZ (a, b) recovery on a known miscalibration (bias + slope)
  * spread k recovery when the claimed σ_f is half the true residual sd
    (k ≈ 2) and calibration lifts coverage into the acceptance band
  * lockbox discipline: fit cutoffs strictly before test cutoffs
  * evidence ladder: cell → pooled → global → none (identity)
  * k shrinkage toward 1 with small n
  * identity passthrough for unknown cells / arms without intervals
  * run merging dedupes cutoff collisions, later run wins
  * CLI end-to-end smoke: artifacts written, JSON round-trips
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vol_backtest.recalib import (  # noqa: E402
    LEVEL_CELL,
    LEVEL_NONE,
    LEVEL_POOLED,
    Z80,
    CalibrationMap,
    CellCalibration,
    _fit_spread_k,
    _mz_fit,
    _sigma_f,
    _sigma_f_one,
    fit_calibration_map,
    load_records,
)
from vol_backtest.recalib import main as recalib_main  # noqa: E402


def _synth_records(
    *,
    n: int = 400,
    a_true: float = 0.25,
    b_true: float = 0.8,
    claimed_sigma: float = 0.5,
    true_sigma: float = 1.0,
    seed: int = 7,
    symbol: str = "SYN",
    horizon: int = 24,
    arm: str = "synarm",
    lo80_frac: float | None = None,
) -> pd.DataFrame:
    """Records with a KNOWN miscalibration.

    F ~ lognormal(level 0.5, sd 0.5); RV = exp(a + b·logF + true_sigma·z).
    The claimed interval is F·exp(±Z80·claimed_sigma) (log-symmetric), so
    the implied k_true = true_sigma / claimed_sigma.
    """
    rng = np.random.default_rng(seed)
    log_f = 0.5 + 0.5 * rng.standard_normal(n)
    f = np.exp(log_f)
    log_rv = a_true + b_true * log_f + true_sigma * rng.standard_normal(n)
    rv = np.exp(log_rv)
    if lo80_frac is None:
        lo = f * np.exp(-Z80 * claimed_sigma)
        hi = f * np.exp(Z80 * claimed_sigma)
    else:  # simulate the CLT-style additive interval of arms.py
        half = lo80_frac * f
        lo = np.maximum(f - half, 1e-16)
        hi = f + half
    return pd.DataFrame({
        "symbol": symbol,
        "horizon": horizon,
        "cutoff": np.arange(n),
        "arm": arm,
        "forecast": f,
        "actual": rv,
        "lo80": lo,
        "hi80": hi,
        "prev_rv": np.nan,
        "prev2_rv": np.nan,
        "run_id": "syn_run",
    })


# ─── unit: core math ─────────────────────────────────────────────────────────


def test_mz_fit_recovers_bias_and_slope():
    rng = np.random.default_rng(11)
    log_f = 0.3 + 0.4 * rng.standard_normal(3000)
    log_rv = 0.25 + 0.8 * log_f + 0.2 * rng.standard_normal(3000)
    a, b, r2 = _mz_fit(log_f, log_rv)
    assert abs(a - 0.25) < 0.02
    assert abs(b - 0.80) < 0.02
    assert r2 > 0.6  # var(b·x)=0.64·0.16 vs var(ε)=0.04 → r² ≈ 0.72


def test_mz_fit_degenerate_is_identity():
    a, b, r2 = _mz_fit(np.full(50, 1.0), np.random.default_rng(3).standard_normal(50))
    assert (a, b) == (0.0, 1.0)
    assert np.isnan(r2)


def test_sigma_f_one_and_vectorized():
    f = 0.02
    lo, hi = f * np.exp(-Z80 * 0.4), f * np.exp(Z80 * 0.4)
    assert abs(_sigma_f_one(lo, hi) - 0.4) < 1e-12
    v = _sigma_f(np.array([lo, np.nan]), np.array([hi, np.nan]))
    assert abs(v[0] - 0.4) < 1e-12 and np.isnan(v[1])
    assert _sigma_f_one(None, 5.0) is None
    assert _sigma_f_one(0.5, 0.4) is None  # hi <= lo


def test_spread_k_recovers_ratio_and_shrinks():
    rng = np.random.default_rng(5)
    e = np.abs(rng.standard_normal(5000))  # q80(|z|) = Z80 → k_raw = 1
    k, k_raw, shrunk = _fit_spread_k(e, shrink_n0=0, k_prior=1.0, min_e=20)
    assert abs(k_raw - 1.0) < 0.03 and not shrunk
    # 2× the dispersion → k ≈ 2
    k2, k2_raw, _ = _fit_spread_k(e * 2.0, shrink_n0=0, k_prior=1.0, min_e=20)
    assert abs(k2_raw - 2.0) < 0.06
    # shrinkage pulls toward 1 with small n
    ks, _, shr = _fit_spread_k(e[:30] * 2.0, shrink_n0=20.0, k_prior=1.0, min_e=20)
    assert shr and ks < 2.0
    # below min_e → no spread calibration
    k0, k0r, _ = _fit_spread_k(e[:10], shrink_n0=0, k_prior=1.0, min_e=20)
    assert k0 is None and k0r is None


# ─── integration: fit_calibration_map on synthetic miscalibration ───────────


def test_fit_recovers_known_miscalibration():
    # claimed σ_f = 0.5, true residual sd = 1.0 → k_true = 2; a=0.25, b=0.8
    df = _synth_records()
    cmap = fit_calibration_map(df, min_n_cell=60, min_n_pooled=80, min_e=20,
                                shrink_n0=0.0)
    c = cmap.cell("SYN", 24, "synarm")
    assert c is not None and c.level == LEVEL_CELL
    assert abs(c.mz_a - 0.25) < 0.25
    assert abs(c.mz_b - 0.80) < 0.25
    assert abs(c.spread_k - 2.0) < 0.5  # k ≈ 2 within sampling noise


def test_lockbox_split_is_time_ordered():
    df = _synth_records(n=200)
    cmap = fit_calibration_map(df, train_frac=0.7, min_n_cell=60,
                                shrink_n0=0.0)
    c = cmap.cell("SYN", 24, "synarm")
    assert c.n_fit + c.n_test == 200
    assert c.fit_cutoff_hi is not None and c.n_test > 0
    # every fitted cutoff is strictly below every lockbox cutoff
    test_cutoffs = df["cutoff"].to_numpy()[-c.n_test:]
    assert c.fit_cutoff_hi < test_cutoffs.min()


def test_calibration_lifts_coverage_into_band():
    # claimed σ_f = 0.35 vs true residual sd 1.0 → k_true ≈ 2.9, raw
    # coverage ~0.34; calibrated must land near nominal 0.80 — assert the
    # band and the improvement, tolerant of n=400 sampling noise.
    df = _synth_records(n=400, seed=21, claimed_sigma=0.35)
    cmap = fit_calibration_map(df, shrink_n0=0.0)
    c = cmap.cell("SYN", 24, "synarm")
    assert c.cov_raw_test is not None and c.cov_raw_test < 0.45
    assert c.cov_cal_test is not None
    assert 0.70 <= c.cov_cal_test <= 0.90
    assert c.cov_cal_test > c.cov_raw_test + 0.25
    assert c.accepted is True
    # no-evidence ladder floor: force level=none (thresholds unreachable)
    # → identity, k=None → not scoreable, nothing ships un-measured.
    df2 = _synth_records(n=400, seed=21, claimed_sigma=0.35)
    cmap2 = fit_calibration_map(df2, shrink_n0=0.0, min_n_cell=10_000,
                                 min_n_pooled=10_000)  # forced level=none
    c2 = cmap2.cell("SYN", 24, "synarm")
    assert c2.level == LEVEL_NONE and c2.accepted is None


def test_ladder_falls_through_pooled_then_none():
    # 30 train rows per (horizon) cell → below min_n_cell=60; two horizons
    # pooled per (symbol, arm) = 60 train rows ≥ min_n_pooled=60 → pooled.
    df_a = _synth_records(n=50, horizon=24, seed=1, symbol="LAD")
    df_b = _synth_records(n=50, horizon=48, seed=2, symbol="LAD")
    cmap = fit_calibration_map(
        pd.concat([df_a, df_b], ignore_index=True),
        min_n_cell=60, min_n_pooled=60, shrink_n0=0.0,
    )
    assert cmap.cell("LAD", 24, "synarm").level == LEVEL_POOLED
    assert cmap.cell("LAD", 48, "synarm").level == LEVEL_POOLED

    # same data but min_n_pooled above the pooled count → identity
    cmap2 = fit_calibration_map(
        pd.concat([df_a, df_b], ignore_index=True),
        min_n_cell=60, min_n_pooled=200, shrink_n0=0.0,
    )
    for H in (24, 48):
        c = cmap2.cell("LAD", H, "synarm")
        assert c.level == LEVEL_NONE
        assert (c.mz_a, c.mz_b, c.spread_k) == (0.0, 1.0, None)
        assert c.accepted is None


def test_arm_without_intervals_gets_center_only():
    df = _synth_records(n=200)
    df["lo80"] = np.nan
    df["hi80"] = np.nan
    cmap = fit_calibration_map(df, shrink_n0=0.0)
    c = cmap.cell("SYN", 24, "synarm")
    assert c.spread_k is None and c.n_fit_spread == 0
    assert c.accepted is None  # not scoreable without a claimed spread
    assert c.qlike_cal_test is not None  # center still scored


def test_apply_identity_and_calibration():
    df = _synth_records(n=200, seed=9)
    cmap = fit_calibration_map(df, shrink_n0=0.0)
    f, lo, hi = 0.02, 0.015, 0.027
    out = cmap.apply("SYN", 24, "synarm", f, lo, hi)
    assert out.applied and out.level == LEVEL_CELL
    assert out.rv != f and out.lo80 is not None and out.lo80 < out.rv < out.hi80
    # log-symmetric interval around the corrected center
    mid = np.sqrt(out.lo80 * out.hi80)
    assert abs(mid - out.rv) / out.rv < 1e-9
    # unknown symbol → passthrough
    out2 = cmap.apply("NOPE", 24, "synarm", f, lo, hi)
    assert not out2.applied and out2.rv == f and out2.lo80 == lo
    # known cell, missing interval → calibrated center, no interval
    out3 = cmap.apply("SYN", 24, "synarm", f)
    assert out3.applied and out3.lo80 is None and out3.hi80 is None
    # garbage forecast → passthrough NaN-safe
    out4 = cmap.apply("SYN", 24, "synarm", float("nan"), lo, hi)
    assert not out4.applied and np.isnan(out4.rv)


def test_additive_clt_interval_also_gets_fixed():
    # the arms.py construction (rv ± frac·rv) under-claims dispersion the
    # same way Chronos/ewma did in run 010230; k must repair it too.
    df = _synth_records(n=400, seed=33, lo80_frac=0.185)  # ≈ Z80/√48
    cmap = fit_calibration_map(df, shrink_n0=0.0)
    c = cmap.cell("SYN", 24, "synarm")
    assert c.cov_raw_test is not None and c.cov_raw_test < 0.55
    assert c.cov_cal_test is not None and c.cov_cal_test >= 0.70
    assert c.spread_k is not None and c.spread_k > 1.5


# ─── records loading ─────────────────────────────────────────────────────────


def test_load_records_dedupes_later_run_wins(tmp_path: Path):
    r1 = _synth_records(n=40, seed=1, arm="har")
    r2 = _synth_records(n=40, seed=1, arm="har")
    r2["forecast"] = r2["forecast"] * 1.5  # make the newer run distinguishable
    d1, d2 = tmp_path / "run_a", tmp_path / "run_b"
    d1.mkdir()
    d2.mkdir()
    r1.to_csv(d1 / "records.csv", index=False)
    r2.to_csv(d2 / "records.csv", index=False)
    merged = load_records([d1, d2])
    assert len(merged) == 40  # 80 rows in, same 40 cutoffs → deduped
    # later dir wins
    f_new = float(r2["forecast"].iloc[0])
    row = merged[merged["cutoff"] == 0].iloc[0]
    assert abs(float(row["forecast"]) - f_new) < 1e-12
    assert merged.attrs["run_ids"] == ["run_a", "run_b"]
    with pytest.raises(FileNotFoundError):
        load_records([tmp_path / "missing"])


# ─── CLI end-to-end ──────────────────────────────────────────────────────────


def test_cli_smoke(tmp_path: Path, capsys):
    d = tmp_path / "run_smoke"
    d.mkdir()
    _synth_records(n=200, seed=13).to_csv(d / "records.csv", index=False)
    out = tmp_path / "recal"
    rc = recalib_main(["--run-dir", str(d), "--out", str(out),
                       "--shrink-n0", "0"])
    assert rc == 0
    for name in ("recalib.json", "recalib.csv", "recalib_report.md"):
        assert (out / name).exists()
    art = json.loads((out / "recalib.json").read_text())
    assert art["schema_version"] == 1
    assert art["n_cells"] == 1
    row = art["cells"][0]
    assert row["symbol"] == "SYN" and row["horizon_bars"] == 24
    assert row["arm"] == "synarm" and row["level"] == LEVEL_CELL
    assert row["spread_k"] > 1.0
    assert row["accepted"] is True
    txt = (out / "recalib_report.md").read_text()
    assert "cov raw→cal (test)" in txt and "ACCEPT" in txt
    assert "train_frac=0.7" in txt  # params echoed
    printed = capsys.readouterr().out
    assert "accepted: 1" in printed


def test_default_out_dir_is_first_run_dir(tmp_path: Path):
    d = tmp_path / "run_default"
    d.mkdir()
    _synth_records(n=120, seed=17).to_csv(d / "records.csv", index=False)
    rc = recalib_main(["--run-dir", str(d)])
    assert rc == 0
    assert (d / "recalib" / "recalib.json").exists()


def test_calibration_map_frozen_and_params_stamped():
    df = _synth_records(n=150, seed=23)
    cmap = fit_calibration_map(df, shrink_n0=5.0, qlike_tol=0.2)
    assert isinstance(cmap, CalibrationMap)
    assert cmap.params["shrink_n0"] == 5.0
    assert cmap.params["qlike_tol"] == 0.2
    cell = next(iter(cmap.cells.values()))
    assert isinstance(cell, CellCalibration)
    # to_row round-trips through json (tuple → str)
    row = cell.to_row()
    json.dumps(row)
    assert row["run_ids"] == "syn_run"
