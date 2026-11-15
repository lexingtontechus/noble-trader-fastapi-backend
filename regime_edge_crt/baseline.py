"""baseline.py — Experiment A0: the v3 EA exactly as configured, no optimization.

Port of ``baseline_check.py`` (CRT_v5_CONT_MT4.zip). Reads the same
``CRT_CONT_RESEARCH_<SYMBOL>.csv`` files, applies the v3 filters and
settings, and reports EV per trade (net R, costs included) by
symbol x TF x direction x session, plus EV per calendar year so you can see
whether the edge is stable or decaying.

Nothing is selected or fitted here, so these numbers carry no selection bias.

Changes vs the toolkit (sanctioned): the CLI loads the CSV ONCE and passes
(df, metas); ``sys.exit`` → ``CRTLabError``; outputs go to the runid report
dir; a structured result comes back for the artifact + Discord layers. The
filters, stats and cost-burden math are verbatim.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from .config import CRTLabError
from .optimizer import grid_columns, outcomes

SESS = {0: "OUT", 1: "NY", 2: "ASIA"}


def stats(r):
    r = np.asarray(r, float)
    n = len(r)
    if n == 0:
        return dict(n=0, ev=np.nan, t=np.nan, win=np.nan, pf=np.nan, total=0.0, maxdd=0.0)
    ev = r.mean()
    sd = r.std(ddof=1) if n > 1 else np.nan
    t = ev / (sd / np.sqrt(n)) if n > 1 and sd > 0 else np.nan
    gw, gl = r[r > 0].sum(), -r[r <= 0].sum()
    eq = np.cumsum(r)
    dd = (np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq).max()
    return dict(n=n, ev=round(ev, 3), t=round(t, 2), win=round((r > 0).mean() * 100, 1),
                pf=round(gw / gl, 2) if gl > 0 else np.nan, total=round(r.sum(), 1), maxdd=round(dd, 1))


def run_baseline(a, df, metas):
    """``a``: the baseline config fields (model/max_sweep/min_sweep/chop/
    sessions/eb/sb/r/hold/out). Returns the structured A0 result."""
    if a.start:
        df = df[df["c3_time"] >= pd.Timestamp(a.start)].reset_index(drop=True)
    rcols, hcols, rs, hs = grid_columns(df)
    if not np.isclose(rs, a.r).any():
        raise CRTLabError(f"R {a.r} not in exported grid {list(rs)}")
    if a.hold not in hs:
        raise CRTLabError(f"hold {a.hold} not in exported grid {list(hs)}")
    sess = [int(s) for s in a.sessions.split(",") if s.strip() != ""]

    m = (df["model"] == a.model) & np.isclose(df["sl_buf"], a.sb)
    if a.model in ("STOP", "CONT"):
        m &= np.isclose(df["entry_buf"], a.eb)
    if a.max_sweep > 0:
        m &= df["sweep_atr"] <= a.max_sweep
    if a.min_sweep > 0:
        m &= df["sweep_atr"] >= a.min_sweep
    if a.chop:
        m &= df["chop_pass"] == 1
    m &= df["session"].isin(sess)
    d = df[m].copy()
    if d.empty:
        raise CRTLabError("no setups match these filters")

    k = int(np.where(np.isclose(rs, a.r))[0][0])
    h = int(np.where(hs == a.hold)[0][0])
    R = outcomes(d, rcols, hcols, rs, hs)[:, k, h]
    d["r"] = R
    filled = d["filled"] == 1
    d = d[~np.isnan(d["r"])]
    d["sess"] = d["session"].map(SESS)
    d["year"] = d["c3_time"].dt.year

    rows = []
    for key, g in d.groupby(["symbol", "tf", "dir", "sess"]):
        s = stats(g.sort_values("c3_time")["r"])
        rows.append(dict(zip(["symbol", "tf", "dir", "session"], key), **s))
    for key, g in d.groupby(["symbol", "tf"]):
        s = stats(g.sort_values("c3_time")["r"])
        rows.append(dict(symbol=key[0], tf=key[1], dir="ALL", session="ALL", **s))
    for sym, g in d.groupby("symbol"):
        s = stats(g.sort_values("c3_time")["r"])
        rows.append(dict(symbol=sym, tf="ALL", dir="ALL", session="ALL", **s))
    tab = pd.DataFrame(rows)

    # Cost burden: how many R each trade pays in spread + slippage at its structural stop size
    cost_rows = []
    for (sym, tf), g in d.groupby(["symbol", "tf"]):
        sp = float(metas.get(sym, {}).get("spread_pts", "nan"))
        rp = g["risk_pts"].astype(float)
        cost_r = sp / rp + g["entry_cost_r"] + g["exit_slip_r"]
        cost_rows.append(dict(symbol=sym, tf=tf, median_stop_pts=round(rp.median(), 1), spread_pts=sp,
                              spread_pct_of_stop=round((sp / rp).median() * 100, 1),
                              est_cost_R_per_trade=round(cost_r.median(), 3),
                              ev_R=round(g["r"].mean(), 3),
                              ev_before_costs_R_approx=round(g["r"].mean() + cost_r.mean(), 3)))
    cost = pd.DataFrame(cost_rows)

    yr = d.groupby(["symbol", "tf", "year"])["r"].agg(["count", "mean", "sum"]).round(3).reset_index()
    yr.columns = ["symbol", "tf", "year", "n", "ev", "total_r"]

    os.makedirs(a.out, exist_ok=True)
    tab.to_csv(os.path.join(a.out, "baseline_buckets.csv"), index=False)
    yr.to_csv(os.path.join(a.out, "baseline_by_year.csv"), index=False)
    cost.to_csv(os.path.join(a.out, "baseline_costs.csv"), index=False)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    print(f"\nBASELINE (no optimization): model {a.model}, entry buf {a.eb}, SL buf {a.sb}, {a.r}R, hold {a.hold}, "
          f"sweep <= {a.max_sweep or 'any'}, chop {'on' if a.chop else 'off'}, sessions {[SESS[s] for s in sess]}")
    print(f"setups after filters: {int(m.sum()):,} | filled: {int(filled.sum()):,} | resolved trades: {len(d):,}")
    print(f"data {d['c3_time'].min():%Y-%m-%d} -> {d['c3_time'].max():%Y-%m-%d}\n")
    print(tab.sort_values(["symbol", "tf", "dir", "session"]).to_string(index=False))
    print("\nEV by year (symbol x TF):")
    print(yr.pivot_table(index=["symbol", "tf"], columns="year", values="ev").round(3).to_string())
    print("\nCOST BURDEN (median stop size vs spread; EV before costs is an approximation):")
    print(cost.to_string(index=False))
    print(f"\nwrote {a.out}{os.sep}baseline_buckets.csv, baseline_by_year.csv, baseline_costs.csv")

    return dict(
        buckets=tab, by_year=yr, costs=cost,
        n_setups=int(m.sum()), n_filled=int(filled.sum()), n_resolved=int(len(d)),
        data_min=str(d["c3_time"].min()), data_max=str(d["c3_time"].max()),
    )
