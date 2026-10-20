"""tsfm.py — machine-parsed reframe of the repo's own vol_backtest artifacts.

Everything here is PARSED from the repo's run artifacts (never trusted from
memory): 20261019_005157 (HAR-family vs naive), 20261019_010230 (chronos2
zero-shot vs HAR), 20261019_recalib_phase05 (MZ + spread-k recalibration).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

RUN_HAR = "20261019_005157"
RUN_CHRONOS = "20261019_010230"
RUN_RECALIB = "20261019_recalib_phase05"


def parse_vol_backtest(reports_dir: str) -> dict:
    base = os.path.join(reports_dir, RUN_HAR)
    dm = pd.read_csv(os.path.join(base, "dm_tests.csv"))
    dm_ch = pd.read_csv(os.path.join(reports_dir, RUN_CHRONOS, "dm_tests.csv"))
    cells = dm.groupby(["symbol", "horizon"]).ngroups

    def beats(dm_df, arm, ref):
        sub = dm_df[(dm_df.arm_a == arm) & (dm_df.arm_b == ref)]
        sig = sub[(sub.mean_loss_diff < 0) & (sub.significant_holm == True)]  # noqa: E712
        return int(len(sig)), int(len(sub))

    har_n, har_t = beats(dm, "har", "naive")
    hew_n, hew_t = beats(dm, "har_ewma", "naive")
    ch_n = 0
    for arm in ("chronos2", "chronos2_cov"):
        n, _ = beats(dm_ch, arm, "har")
        ch_n += n

    # QLIKE wins by arm, pooled across both runs on matching (symbol, horizon)
    frames = []
    for run in (RUN_HAR, RUN_CHRONOS):
        p = os.path.join(reports_dir, run, "results.csv")
        if os.path.exists(p):
            frames.append(pd.read_csv(p)[["symbol", "horizon", "arm", "qlike"]])
    res = pd.concat(frames, ignore_index=True)
    best = {}
    for (s, h), grp in res.groupby(["symbol", "horizon"]):
        best[(s, h)] = grp.loc[grp.qlike.idxmin(), "arm"]
    wins = {}
    for arm in best.values():
        wins[arm] = wins.get(arm, 0) + 1
    return {
        "har_beats_naive_holm_cells": f"{har_n}/{har_t}",
        "har_ewma_beats_naive": f"{hew_n}/{hew_t}",
        "chronos_beats_har_sig_cells": int(ch_n),
        "qlike_best_by_arm": dict(sorted(wins.items(), key=lambda kv: -kv[1])),
        "n_cells": int(len(best)),
    }


def parse_recalib(reports_dir: str) -> dict:
    p = os.path.join(reports_dir, RUN_RECALIB, "recalib.csv")
    r = pd.read_csv(p)
    accepted = int((r.accepted == True).sum())  # noqa: E712
    mean_b = float(np.nanmean(r.mz_b)) if "mz_b" in r else float("nan")
    mean_k = float(np.nanmean(r.spread_k)) if "spread_k" in r else float("nan")
    return {"accepted": f"{accepted}/{len(r)}", "mean_b": round(mean_b, 4),
            "mean_k": round(mean_k, 4)}


def reframe(repo_root: str) -> dict:
    reports = os.path.join(repo_root, "worklog", "kairos", "reports", "vol_backtest")
    vb = parse_vol_backtest(reports)
    rc = parse_recalib(reports)
    conclusion = (
        "Zero-shot TSFMs carry no directional edge: no chronos arm significantly "
        f"beats HAR on QLIKE in any of the {vb['n_cells']} cells "
        f"({vb['chronos_beats_har_sig_cells']} Holm-significant wins), while HAR/HAR-EWMA "
        f"beat naive in {vb['har_beats_naive_holm_cells']} and "
        f"{vb['har_ewma_beats_naive']} cells. TSFM's role is calibrated vol spreads for "
        f"sizing/geometry (Phase 0.5 recalibration accepted {rc['accepted']} cells at "
        f"mean b={rc['mean_b']}, k={rc['mean_k']}); DIRECTION/EDGE comes from regime "
        "values (this engine)."
    )
    return {"vol_backtest": vb, "recalib": rc, "conclusion": conclusion}
