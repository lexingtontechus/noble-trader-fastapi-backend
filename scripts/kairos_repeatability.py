"""Kairos: is the edge/decay MAP repeatable? (2026-09-25)

Reframes P1 (V1–V4 pass/fail on pooled averages) as the question the
experiment actually asks: *when* (horizon h, regime age a) and *where*
(regime cell, symbol) is there edge or decay — and does that map repeat
out of sample?

Method (same data and frozen definitions as scripts/kairos_discovery.py):
  * Signals: every labelled 1h bar, d = label direction, m_h = d·ΔC_h/ATR.
  * Excess edge e_h = d·(ΔC_h/ATR − μ_h), μ_h = the symbol's trailing 30-day
    mean move (only bars already resolved at t — no look-ahead). This strips
    drift: P1 found bull AND bear labels both followed by rises, i.e. m_h
    was mostly market drift, not label information.
  * Map M[key, block] = mean over symbol-days of e_h (equal day weights),
    keys = cell×age-band (pooled), symbol×cell, cell×age-band×symbol.
  * Repeatability = Spearman ρ of M between time blocks (first vs second
    half, and consecutive quarters), plus sign persistence of the keys that
    stood out (|t| ≥ 2) in the first half.
  * Null: circular shift of labels vs prices (≥ 14d, per symbol — the P1
    null). It keeps drift, label frequencies and run structure, so any
    repeatability that is only drift or only "cells are common" repeats in
    the null too. p = share of null draws ≥ observed.
  * Decay: per-quarter slope of e_24 on log(1+age); sign consistency vs null.
  * Hazard: per-quarter run-exit hazard by age band (descriptive).

Run:  PYTHONPATH="$PWD" python scripts/kairos_repeatability.py [--n-null 200] [--n-confirm 2]
Writes worklog/kairos/reports/YYYYMMDD_repeatability_nc{N}.{md,json}.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kd", ROOT / "scripts" / "kairos_discovery.py")
kd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kd)

H = (1, 4, 12, 24, 48)
HN = (4, 24)            # horizons carried through the null
DRIFT_WIN = 720         # 30 days of 1h bars
MIN_DAYS = 10           # map entry needs ≥ 10 symbol-days in a block
RNG = np.random.default_rng(925)


def _cache_dir() -> Path:
    for c in (ROOT / ".cache", ROOT.parents[1] / ".cache"):
        if (c / "recon_EURUSD_labels.json").exists():
            return c
    raise SystemExit("recon caches not found (.cache/recon_*_{bars_1h,labels}.json)")


def trailing_drift(move: np.ndarray, h: int, win: int = DRIFT_WIN) -> np.ndarray:
    """μ[t] = mean(move[s]) over s ∈ [t−win, t−h] (resolved by t). NaN if < 50 obs."""
    n = len(move)
    ok = np.isfinite(move)
    cs = np.r_[0.0, np.cumsum(np.where(ok, move, 0.0))]
    cn = np.r_[0, np.cumsum(ok)]
    t = np.arange(n)
    hi = np.clip(t - h + 1, 0, n)          # exclusive end: s ≤ t−h
    lo = np.clip(t - win, 0, n)
    cnt = cn[hi] - cn[lo]
    mu = np.where(cnt >= 50, (cs[hi] - cs[lo]) / np.maximum(cnt, 1), np.nan)
    return mu


def build(nconf: int):
    kd.CACHE = _cache_dir()
    kd.KCACHE = kd.CACHE / "kairos"
    A, R, MU = {}, {}, {}
    for s in kd.SYMS:
        df, labs = kd.load_symbol(s, False)
        raw = [None] * len(df)
        for t, lbl, _c, _v in labs:
            raw[int(t)] = lbl
        cells = [kd.cell_of(x) for x in kd.debounce(raw, nconf)]
        A[s] = kd.build_arrays(s, df, cells)
        R[s] = kd.runs_of(A[s]["cell"], A[s]["ts"], A[s]["gap_before"])
        MU[s] = {h: trailing_drift(A[s]["move"][h], h) for h in H}
    return A, R, MU


def frame(A, R, MU, offsets: dict | None = None, hs=H) -> pd.DataFrame:
    """Signal table; with ``offsets`` the label side (cell, d, age, censoring)
    is circularly shifted against prices (the null)."""
    out = []
    for s, a in A.items():
        cell, d = a["cell"], a["d"]
        age, lc = R[s]["age_h"], R[s]["left_cens"]
        if offsets:
            o = offsets[s]
            cell, d, age, lc = (np.roll(x, o) for x in (cell, d, age, lc))
        keep = d != 0
        f = pd.DataFrame({"symbol": s, "ts": a["ts"][keep], "day": a["day"][keep],
                          "cell": cell[keep], "d": d[keep], "age": age[keep], "lc": lc[keep]})
        for h in hs:
            mv = a["move"][h][keep]
            f[f"m{h}"] = d[keep] * mv
            f[f"e{h}"] = d[keep] * (mv - MU[s][h][keep])
        out.append(f)
    f = pd.concat(out, ignore_index=True)
    dt = pd.to_datetime(f.ts, unit="s", utc=True)
    f["q"] = dt.dt.year.astype(str) + "Q" + dt.dt.quarter.astype(str)
    mid = np.median(f.ts)
    f["half"] = np.where(f.ts < mid, "H1", "H2")
    f["band"] = pd.cut(f.age, [-0.01, 6, 12, 24, 48, np.inf], labels=kd.BAND_LAB).astype(str)
    f["cellname"] = f.cell.map(lambda k: kd.CELLS[k])
    return f


def emap(f: pd.DataFrame, keys: list[str], block: str, col: str) -> pd.DataFrame:
    """Mean of symbol-day means (so overlapping hourly signals don't inflate n)."""
    g = f.dropna(subset=[col]).groupby(keys + [block, "symbol", "day"], observed=True)[col].mean()
    g = g.groupby(keys + [block], observed=True).agg(["mean", "std", "count"])
    g = g[g["count"] >= MIN_DAYS]
    g["t"] = g["mean"] / (g["std"] / np.sqrt(g["count"]))
    return g


def rep_stats(f: pd.DataFrame, keys: list[str], col: str) -> dict:
    """ρ(H1,H2), mean ρ over consecutive quarters, sign persistence of |t|≥2 keys."""
    out = {}
    m = emap(f, keys, "half", col)["mean"].unstack("half")
    tt = emap(f, keys, "half", col)["t"].unstack("half")
    both = m.dropna()
    out["n_keys"] = int(len(both))
    out["rho_half"] = float(both["H1"].rank().corr(both["H2"].rank())) if len(both) >= 5 else np.nan
    strong = tt.dropna()
    strong = strong[strong["H1"].abs() >= 2]
    out["n_strong"] = int(len(strong))
    out["sign_keep"] = float((np.sign(strong["H1"]) == np.sign(strong["H2"])).mean()) if len(strong) else np.nan
    mq = emap(f, keys, "q", col)["mean"].unstack("q")
    qs = sorted(mq.columns)
    rs = []
    for q0, q1 in zip(qs, qs[1:]):
        p = mq[[q0, q1]].dropna()
        if len(p) >= 5:
            rs.append(p[q0].rank().corr(p[q1].rank()))
    out["rho_quarters"] = float(np.nanmean(rs)) if rs else np.nan
    out["n_quarter_pairs"] = len(rs)
    return out


def decay_by_quarter(f: pd.DataFrame, col: str = "e24") -> pd.DataFrame:
    g = f[~f.lc].dropna(subset=[col]).copy()
    g[col] = g[col] - g.groupby("symbol")[col].transform("mean")
    rows = []
    for q, x in g.groupby("q"):
        if len(x) < 200:
            continue
        xs = np.log1p(x.age.to_numpy())
        y = x[col].to_numpy()
        slope = float(np.polyfit(xs, y, 1)[0])
        rows.append({"q": q, "n": len(x), "slope": slope,
                     "share_age_ge12": float((x.age >= 12).mean())})
    return pd.DataFrame(rows)


def hazard_by_quarter(A, R) -> pd.DataFrame:
    rows = []
    for s in A:
        tab = R[s]["table"]
        tab = tab[~tab.left_cens].copy()
        st = pd.to_datetime(A[s]["ts"][tab.start], unit="s", utc=True)
        tab["q"] = st.year.astype(str) + "Q" + st.quarter.astype(str)
        rows.append(tab[["q", "len_h", "obs_end"]])
    t = pd.concat(rows)
    out = []
    edges = [0, 6, 12, 24, 48]
    for q, x in t.groupby("q"):
        rec = {"q": q, "runs": len(x), "median_len_h": float(x.len_h.median())}
        L, ev = x.len_h.to_numpy(), x.obs_end.to_numpy()
        for lo, hi in zip(edges, edges[1:] + [np.inf]):
            at_risk = L > lo
            expo = np.clip(np.minimum(L, hi) - lo, 0, None)[at_risk].sum()
            ends = (at_risk & (L <= hi) & ev).sum()
            rec[f"h_{lo}"] = float(ends / expo) if expo > 0 else np.nan
        out.append(rec)
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-null", type=int, default=200)
    ap.add_argument("--n-confirm", type=int, default=2)
    a = ap.parse_args()
    t0 = time.time()
    A, R, MU = build(a.n_confirm)
    f = frame(A, R, MU)
    print(f"signals {len(f)} · symbols {f.symbol.nunique()} · quarters {sorted(f.q.unique())} · half split {pd.to_datetime(np.median(f.ts), unit='s'):%Y-%m-%d}")

    KEYS = {"cell×age": ["cellname", "band"], "symbol×cell": ["symbol", "cellname"],
            "symbol×cell×age": ["symbol", "cellname", "band"]}
    obs = {(k, c): rep_stats(f, keys, c) for k, keys in KEYS.items() for c in (f"e{h}" for h in HN)}
    obs.update({(k, c): rep_stats(f, keys, c) for k, keys in KEYS.items() for c in (f"m{h}" for h in HN)})

    # null
    null = {kk: {"rho_half": [], "rho_quarters": [], "sign_keep": []} for kk in obs}
    dec_obs = decay_by_quarter(f)
    null_dec_neg = []
    tn = time.time()
    for i in range(a.n_null):
        offs = {s: int(RNG.integers(336, A[s]["n"] - 336)) for s in A}
        fn = frame(A, R, MU, offs, hs=HN)
        for (k, c) in obs:
            r = rep_stats(fn, KEYS[k], c)
            for st in null[(k, c)]:
                null[(k, c)][st].append(r[st])
        dn = decay_by_quarter(fn)
        null_dec_neg.append(float((dn.slope < 0).mean()) if len(dn) else np.nan)
        if i == 4:
            print(f"  null: ~{(time.time() - tn) / 5 * a.n_null:.0f}s for {a.n_null} draws")

    def pval(o, arr):
        arr = np.array([x for x in arr if np.isfinite(x)])
        return float((arr >= o).mean()) if len(arr) and np.isfinite(o) else np.nan

    res = []
    for (k, c), o in obs.items():
        nn = null[(k, c)]
        res.append({"map": k, "measure": c, **o,
                    "null_rho_half_p50": float(np.nanmedian(nn["rho_half"])),
                    "p_rho_half": pval(o["rho_half"], nn["rho_half"]),
                    "null_rho_q_p50": float(np.nanmedian(nn["rho_quarters"])),
                    "p_rho_quarters": pval(o["rho_quarters"], nn["rho_quarters"]),
                    "null_sign_keep_p50": float(np.nanmedian(nn["sign_keep"])),
                    "p_sign_keep": pval(o["sign_keep"], nn["sign_keep"])})
    res = pd.DataFrame(res)
    dec_neg_obs = float((dec_obs.slope < 0).mean())
    p_dec = pval(dec_neg_obs, null_dec_neg)
    haz = hazard_by_quarter(A, R)

    # where: the pooled map, H1 vs H2, for the largest keys
    mp = emap(f, ["cellname", "band"], "half", "e24")[["mean", "t", "count"]].unstack("half")
    mp.columns = [f"{a_}_{b}" for a_, b in mp.columns]
    mp = mp.dropna().sort_values("mean_H1")
    curve = (f.groupby("cellname")[[f"e{h}" for h in H]].mean())

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = ROOT / "worklog" / "kairos" / "reports" / f"{stamp}_repeatability_nc{a.n_confirm}"
    L = []
    P = L.append
    P(f"# Kairos repeatability: is the edge/decay map stable out of sample? (n_confirm {a.n_confirm})\n")
    P(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/kairos_repeatability.py` · "
      f"{len(f):,} signals · 5 symbols · {a.n_null} circular-shift null draws · runtime {time.time() - t0:.0f}s.\n")
    P("e_h = label-following move in ATR **minus the symbol's trailing 30-day drift** (no look-ahead). "
      "m_h = raw label-following move (the P1 measure). ρ = Spearman rank correlation of the map between "
      "blocks; p = share of null draws (labels circularly shifted vs prices) at or above the observed value.\n")
    P("## 1. Does the map repeat?\n")
    P("| map | measure | keys | ρ H1→H2 | null ρ | p | ρ consecutive quarters | null ρ | p | |t|≥2 keys in H1 | same sign in H2 | null | p |")
    P("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in res.itertuples():
        P(f"| {r.map} | {r.measure} | {r.n_keys} | {r.rho_half:+.2f} | {r.null_rho_half_p50:+.2f} | {r.p_rho_half:.3f} | "
          f"{r.rho_quarters:+.2f} | {r.null_rho_q_p50:+.2f} | {r.p_rho_quarters:.3f} | {r.n_strong} | "
          f"{r.sign_keep:.2f} | {r.null_sign_keep_p50:.2f} | {r.p_sign_keep:.3f} |")
    P("\n## 2. Where: pooled cell × age map of e₂₄ (ATR), first half vs second half\n")
    P("| cell | age | H1 mean | H1 t | H2 mean | H2 t | days H1/H2 |")
    P("|---|---|---|---|---|---|---|")
    for (cell, band), r in mp.iterrows():
        P(f"| {cell} | {band} | {r.mean_H1:+.3f} | {r.t_H1:+.1f} | {r.mean_H2:+.3f} | {r.t_H2:+.1f} | "
          f"{int(r.count_H1)}/{int(r.count_H2)} |")
    P("\n## 3. When (horizon): mean e_h by cell, full sample (ATR)\n")
    P("| cell | " + " | ".join(f"e{h}" for h in H) + " |")
    P("|---|" + "---|" * len(H))
    for cell, r in curve.iterrows():
        P(f"| {cell} | " + " | ".join(f"{r[f'e{h}']:+.3f}" for h in H) + " |")
    P("\n## 4. When (age): does decay with regime age repeat quarter to quarter?\n")
    P("| quarter | signals | slope of e₂₄ on log(1+age) | share age ≥ 12h |")
    P("|---|---|---|---|")
    for r in dec_obs.itertuples():
        P(f"| {r.q} | {r.n} | {r.slope:+.3f} | {r.share_age_ge12:.3f} |")
    P(f"\nNegative-slope quarters: {dec_neg_obs:.0%} · null median {np.nanmedian(null_dec_neg):.0%} · p {p_dec:.3f}\n")
    P("## 5. Regime-exit hazard by age band, per quarter (exits per hour at risk)\n")
    P("| quarter | runs | median length h | 0–6h | 6–12h | 12–24h | 24–48h | ≥48h |")
    P("|---|---|---|---|---|---|---|---|")
    for r in haz.itertuples():
        P(f"| {r.q} | {r.runs} | {r.median_len_h:.0f} | " + " | ".join(
            "–" if not np.isfinite(getattr(r, f"h_{lo}")) else f"{getattr(r, f'h_{lo}'):.3f}"
            for lo in (0, 6, 12, 24, 48)) + " |")
    out.with_suffix(".md").write_text("\n".join(L) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps({
        "n_confirm": a.n_confirm, "n_null": a.n_null, "repeat": res.to_dict("records"),
        "decay_by_quarter": dec_obs.to_dict("records"), "decay_neg_share": dec_neg_obs, "p_decay": p_dec,
        "hazard_by_quarter": haz.to_dict("records")}, default=float, indent=1), encoding="utf-8")
    print("\n".join(L))
    print(f"\nreport: {out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
