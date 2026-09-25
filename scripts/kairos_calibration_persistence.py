"""Kairos V3 step 1 — does calibration error persist into the next qualified
signal, and how fast does it decay? (V3 scope §14, frozen 2026-09-25.)

Data: nt_signal_sim, source sweep-light, buy/sell, since go-live of resolved
capture. Resolved = tp_hit / sl_hit (expired is a non-result: reported, not
scored).

  err_i   = p_win_i − 1{TP first}              (calibration error; + = overconfident)
  acc_i   = 1{TP first} − SL_i/(TP_i + SL_i)   (accuracy vs the no-skill line)

Targets: every qualified, resolved signal S2. Evidence: the same symbol's
signals (any tier) whose outcome was known before S2 fired (exit_ts < ts_S2),
in lag bins by evidence age (ts_S2 − exit_ts). For each bin:

  β(bin) = slope of err_S2 on the bin's mean evidence error (pooled OLS,
           symbol-day cluster bootstrap CI)  → "how much carries over"
  ρ(bin) = Pearson corr of the same

Decay: exponential weights w = 2^(−age/h) over all prior evidence; the half-
life h that maximises out-of-sample fit of err_S2 ≈ β·(weighted mean), scored
vs the benchmark (flat 7-day window, ≥ 20 records, the current eod_correction
rule) and vs no correction.

Null: within symbol, the S2 target sequence is circularly shifted relative to
the evidence history (breaks timing, keeps both marginal distributions and
each symbol's average bias). Persistence counts only in excess of the null —
this removes "the symbol is just always overconfident" (a static bias) from
"errors carry over and decay" (a dynamic one). Both are reported.

Also: accuracy (acc) persistence the same way, and the expiry share per bin.

Run:  PYTHONPATH="$PWD" python scripts/kairos_calibration_persistence.py [--n-null 500]
Writes worklog/kairos/reports/YYYYMMDD_calibration_persistence.{md,json}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LAG_BINS = [(0, 6), (6, 24), (24, 72), (72, 168), (168, 336)]   # hours since evidence resolved
HALF_LIVES = [3, 6, 12, 24, 48, 96, 168, 336]
BENCH_DAYS, BENCH_MIN = 7, 20
MIN_EVID = 5
RNG = np.random.default_rng(92514)


# ─── data ─────────────────────────────────────────────────────────────────


def fetch() -> pd.DataFrame:
    import httpx
    from dotenv import load_dotenv

    for p in (ROOT / ".env.local", ROOT.parents[1] / ".env.local"):
        if p.exists():
            load_dotenv(p)
    from regime_platform.core.settings import Settings
    s = Settings()
    u, k = s.SUPABASE_URL.rstrip("/"), s.SUPABASE_KEY
    h = {"apikey": k, "Authorization": f"Bearer {k}"}
    cols = ("signal_id,symbol,signal,ts,exit_ts,outcome,qualified,p_win,predicted_p_win_raw,"
            "entry_price,stop_loss,take_profit,regime")
    rows, off = [], 0
    with httpx.Client(timeout=120) as c:
        while True:
            r = c.get(f"{u}/rest/v1/nt_signal_sim", headers=h, params={
                "select": cols, "source": "eq.sweep-light", "signal": "in.(buy,sell)",
                "outcome": "in.(tp_hit,sl_hit,expired)", "order": "ts.asc",
                "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            b = r.json()
            rows += b
            off += len(b)
            if len(b) < 1000:
                break
    df = pd.DataFrame(rows)
    for c_ in ("p_win", "predicted_p_win_raw", "entry_price", "stop_loss", "take_profit"):
        df[c_] = pd.to_numeric(df[c_], errors="coerce")
    df["ts"] = pd.to_datetime(df.ts, utc=True, format="ISO8601")
    df["exit_ts"] = pd.to_datetime(df.exit_ts, utc=True, format="ISO8601", errors="coerce")
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["resolved"] = d.outcome.isin(["tp_hit", "sl_hit"])
    d["y"] = (d.outcome == "tp_hit").astype(float)
    tp = (d.take_profit - d.entry_price).abs()
    sl = (d.stop_loss - d.entry_price).abs()
    d["noskill"] = sl / (tp + sl)
    d["err"] = np.where(d.resolved, d.p_win - d.y, np.nan)
    d["acc"] = np.where(d.resolved, d.y - d.noskill, np.nan)
    # outcome known at: exit_ts for TP/SL (exact bar), else never used as evidence
    d["known_at"] = np.where(d.resolved, d.exit_ts, pd.NaT)
    d["known_at"] = pd.to_datetime(d.known_at, utc=True)
    d = d[d.p_win.notna() & d.noskill.between(0.01, 0.99)]
    return d.sort_values("ts").reset_index(drop=True)


# ─── evidence features per target ────────────────────────────────────────


def evidence_table(d: pd.DataFrame, col: str) -> pd.DataFrame:
    """One row per qualified resolved target: target value + per-lag-bin
    mean evidence (any tier, same symbol, known before target ts) + age
    vectors for decay weighting."""
    out = []
    for sym, g in d.groupby("symbol"):
        ev = g[g.resolved].sort_values("known_at")
        ev_t = ev.known_at.to_numpy(dtype="datetime64[ns]").astype(np.int64) / 3.6e12
        ev_v = ev[col].to_numpy(float)
        tg = g[g.qualified.astype(bool) & g.resolved]
        for r in tg.itertuples():
            t = r.ts.value / 3.6e12
            j = np.searchsorted(ev_t, t, side="left")  # evidence strictly before target
            age = t - ev_t[:j]
            val = ev_v[:j]
            rec = {"symbol": sym, "ts": r.ts, "day": r.ts.floor("D"), "target": getattr(r, col),
                   "p_win": r.p_win, "y": r.y, "ages": age, "vals": val}
            for lo, hi in LAG_BINS:
                m = (age >= lo) & (age < hi)
                rec[f"n_{lo}"] = int(m.sum())
                rec[f"e_{lo}"] = float(val[m].mean()) if m.sum() >= MIN_EVID else np.nan
            out.append(rec)
    return pd.DataFrame(out)


def ols_beta(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 10 or np.var(x) == 0:
        return np.nan, np.nan, len(x)
    b = np.cov(x, y, bias=True)[0, 1] / np.var(x)
    r = np.corrcoef(x, y)[0, 1]
    return float(b), float(r), len(x)


def cluster_boot(T: pd.DataFrame, xcol: str, ycol: str, n_boot: int = 1000):
    ok = T[[xcol, ycol]].notna().all(axis=1)
    T = T[ok]
    if len(T) < 10:
        return np.nan, np.nan
    clu = (T.symbol + "|" + T.day.astype(str)).to_numpy()
    uniq, inv = np.unique(clu, return_inverse=True)
    x, y = T[xcol].to_numpy(float), T[ycol].to_numpy(float)
    bs = []
    for _ in range(n_boot):
        pick = RNG.integers(0, len(uniq), len(uniq))
        w = np.bincount(pick, minlength=len(uniq))[inv].astype(float)
        if w.sum() == 0:
            continue
        mx, my = np.average(x, weights=w), np.average(y, weights=w)
        vx = np.average((x - mx) ** 2, weights=w)
        if vx > 0:
            bs.append(np.average((x - mx) * (y - my), weights=w) / vx)
    return (float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))) if bs else (np.nan, np.nan)


def decayed_mean(ages, vals, hl):
    if len(vals) < MIN_EVID:
        return np.nan
    w = np.power(2.0, -ages / hl)
    return float(np.sum(w * vals) / np.sum(w)) if w.sum() > 0 else np.nan


def flat_window(ages, vals, days=BENCH_DAYS, nmin=BENCH_MIN):
    m = ages < days * 24
    return float(vals[m].mean()) if m.sum() >= nmin else np.nan


def shift_targets(T: pd.DataFrame) -> pd.DataFrame:
    """Null: within symbol, rotate target (and p_win/y) vs evidence rows."""
    parts = []
    for sym, g in T.groupby("symbol"):
        g = g.copy()
        n = len(g)
        if n > 2:
            o = int(RNG.integers(1, n))
            for c in ("target", "p_win", "y"):
                g[c] = np.roll(g[c].to_numpy(), o)
        parts.append(g)
    return pd.concat(parts)


def decay_fit(T: pd.DataFrame) -> dict:
    """Per half-life: β and r of target on decayed mean (demeaned within
    symbol to isolate dynamic carryover); plus the raw (static+dynamic)."""
    res = {}
    for hl in HALF_LIVES:
        x = np.array([decayed_mean(a, v, hl) for a, v in zip(T.ages, T.vals)])
        tmp = T.assign(x=x)
        b_raw, r_raw, n = ols_beta(tmp.x, tmp.target)
        dm = tmp.dropna(subset=["x"]).copy()
        dm["x"] -= dm.groupby("symbol").x.transform("mean")
        dm["t"] = dm.target - dm.groupby("symbol").target.transform("mean")
        b_dyn, r_dyn, _ = ols_beta(dm.x, dm.t)
        res[hl] = {"beta_raw": b_raw, "r_raw": r_raw, "beta_dyn": b_dyn, "r_dyn": r_dyn, "n": n}
    return res


def oos_brier(T: pd.DataFrame, hl: float | None) -> dict:
    """Walk-forward: corrected p = clip(p_win − β̂·x, .01, .99), β̂ fitted on
    targets strictly earlier in time (expanding, ≥ 30). hl None = flat 7d."""
    T = T.sort_values("ts").reset_index(drop=True)
    if hl is None:
        x = np.array([flat_window(a, v) for a, v in zip(T.ages, T.vals)])
    else:
        x = np.array([decayed_mean(a, v, hl) for a, v in zip(T.ages, T.vals)])
    p0, y = T.p_win.to_numpy(float), T.y.to_numpy(float)
    br0, br1, ll0, ll1, used = [], [], [], [], 0
    for i in range(len(T)):
        past = np.arange(i)
        ok = np.isfinite(x[past])
        if ok.sum() < 30 or not np.isfinite(x[i]):
            continue
        xp, ep = x[past][ok], (p0 - y)[past][ok]
        b = np.cov(xp, ep, bias=True)[0, 1] / np.var(xp) if np.var(xp) > 0 else 0.0
        b = float(np.clip(b, 0.0, 1.0))
        p1 = float(np.clip(p0[i] - b * x[i], 0.01, 0.99))
        pp = float(np.clip(p0[i], 0.01, 0.99))
        br0.append((pp - y[i]) ** 2)
        br1.append((p1 - y[i]) ** 2)
        ll0.append(-(y[i] * np.log(pp) + (1 - y[i]) * np.log(1 - pp)))
        ll1.append(-(y[i] * np.log(p1) + (1 - y[i]) * np.log(1 - p1)))
        used += 1
    if not used:
        return {"n": 0}
    d = np.array(br1) - np.array(br0)
    return {"n": used, "brier_uncorr": float(np.mean(br0)), "brier_corr": float(np.mean(br1)),
            "d_brier": float(d.mean()), "d_brier_se": float(d.std(ddof=1) / np.sqrt(used)) if used > 1 else np.nan,
            "d_logloss": float(np.mean(ll1) - np.mean(ll0))}


# ─── main ────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-null", type=int, default=500)
    ap.add_argument("--n-boot", type=int, default=1000)
    a = ap.parse_args()
    t0 = time.time()
    raw = fetch()
    d = prepare(raw)
    q = d[d.qualified.astype(bool)]
    L: list[str] = []
    P = L.append
    P("# Kairos V3 step 1: calibration-error persistence into the next qualified signal\n")
    P(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `scripts/kairos_calibration_persistence.py` "
      f"(scope §14). Data: `nt_signal_sim` sweep-light buy/sell with an outcome, {d.ts.min():%Y-%m-%d} → "
      f"{d.ts.max():%Y-%m-%d}.\n")
    P("err = p_win − 1{TP first} (+ = overconfident) · acc = 1{TP first} − SL/(TP+SL) (vs no-skill) · "
      "evidence = same symbol, any tier, outcome known before the target fired · expired = non-result.\n")

    P("## 0. Population\n")
    P("| | signals | TP | SL | expired | expired share | mean p_win (resolved) | TP rate (resolved) | mean no-skill | mean err | mean acc |")
    P("|---|---|---|---|---|---|---|---|---|---|---|")
    for name, g in (("all", d), ("qualified", q), ("not qualified", d[~d.qualified.astype(bool)])):
        rz = g[g.resolved]
        P(f"| {name} | {len(g):,} | {(g.outcome == 'tp_hit').sum():,} | {(g.outcome == 'sl_hit').sum():,} | "
          f"{(g.outcome == 'expired').sum():,} | {(g.outcome == 'expired').mean():.2f} | {rz.p_win.mean():.3f} | "
          f"{rz.y.mean():.3f} | {rz.noskill.mean():.3f} | {rz.err.mean():+.3f} | {rz.acc.mean():+.3f} |")
    P("")

    results = {}
    for col, label in (("err", "calibration error"), ("acc", "accuracy vs no-skill")):
        T = evidence_table(d, col)
        P(f"## {'1' if col == 'err' else '2'}. Persistence of {label} (targets = qualified resolved, n = {len(T)})\n")
        if len(T) < 20:
            P("Too few targets.\n")
            continue
        # static part: symbol mean
        sym_mean = T.groupby("symbol").target.mean()
        P("Static part (per-symbol mean of the target — a constant bias, not carryover): "
          + " · ".join(f"{s} {v:+.2f}" for s, v in sym_mean.items()) + "\n")
        # by lag bin, observed and null
        obs_bins = {}
        for lo, hi in LAG_BINS:
            b, r, n = ols_beta(T[f"e_{lo}"], T.target)
            ci = cluster_boot(T, f"e_{lo}", "target", a.n_boot)
            obs_bins[lo] = (b, r, n, ci)
        null_b = {lo: [] for lo, _ in LAG_BINS}
        null_dec = {hl: [] for hl in HALF_LIVES}
        dec_obs = decay_fit(T)
        tn = time.time()
        for i in range(a.n_null):
            Tn = shift_targets(T)
            for lo, _ in LAG_BINS:
                null_b[lo].append(ols_beta(Tn[f"e_{lo}"], Tn.target)[0])
            dn = decay_fit(Tn) if i < min(a.n_null, 200) else None
            if dn:
                for hl in HALF_LIVES:
                    null_dec[hl].append(dn[hl]["beta_dyn"])
        P("| evidence age (h since resolved) | targets with ≥ 5 evidence | β carryover [95% CI] | r | null β median [2.5, 97.5] | p (β ≥ obs) |")
        P("|---|---|---|---|---|---|")
        for lo, hi in LAG_BINS:
            b, r, n, ci = obs_bins[lo]
            nb = np.array([x for x in null_b[lo] if np.isfinite(x)])
            p = float((nb >= b).mean()) if len(nb) and np.isfinite(b) else np.nan
            nq = np.percentile(nb, [50, 2.5, 97.5]) if len(nb) else [np.nan] * 3
            P(f"| {lo}–{hi} | {n} | {b:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}] | {r:+.3f} | {nq[0]:+.3f} [{nq[1]:+.3f}, {nq[2]:+.3f}] | {p:.3f} |")
        P("\n**Decay (exponential half-life h over all prior evidence).** raw = static + dynamic; dyn = within-symbol "
          "demeaned (carryover beyond the symbol's constant bias), with its null.\n")
        P("| half-life h | n | β raw | r raw | β dyn | r dyn | null β dyn median | p (dyn) |")
        P("|---|---|---|---|---|---|---|---|")
        for hl in HALF_LIVES:
            o = dec_obs[hl]
            nb = np.array([x for x in null_dec[hl] if np.isfinite(x)])
            p = float((nb >= o["beta_dyn"]).mean()) if len(nb) and np.isfinite(o["beta_dyn"]) else np.nan
            P(f"| {hl}h | {o['n']} | {o['beta_raw']:+.3f} | {o['r_raw']:+.3f} | {o['beta_dyn']:+.3f} | {o['r_dyn']:+.3f} | "
              f"{np.median(nb) if len(nb) else np.nan:+.3f} | {p:.3f} |")
        results[col] = {"bins": {str(k): {"beta": v[0], "r": v[1], "n": v[2], "ci": v[3],
                                          "null_p": float((np.array([x for x in null_b[k] if np.isfinite(x)]) >= v[0]).mean())
                                          if np.isfinite(v[0]) else None} for k, v in obs_bins.items()},
                        "decay": dec_obs, "static": sym_mean.to_dict()}
        if col == "err":
            P("\n**Out-of-sample (walk-forward) correction of S2's p_win:** p′ = p_win − β̂·x, β̂ from earlier targets "
              "only (clipped to [0, 1]). Negative Δ = better than uncorrected.\n")
            P("| evidence rule | targets scored | Brier uncorrected | Brier corrected | Δ Brier ± SE | Δ log-loss |")
            P("|---|---|---|---|---|---|")
            oos = {}
            for name, hl in [("flat 7d, ≥ 20 (current eod_correction)", None)] + [(f"decay h = {x}h", x) for x in HALF_LIVES]:
                r_ = oos_brier(T, hl)
                oos[name] = r_
                if r_.get("n"):
                    P(f"| {name} | {r_['n']} | {r_['brier_uncorr']:.4f} | {r_['brier_corr']:.4f} | "
                      f"{r_['d_brier']:+.4f} ± {r_['d_brier_se']:.4f} | {r_['d_logloss']:+.4f} |")
                else:
                    P(f"| {name} | 0 | – | – | – | – |")
            results["oos"] = oos
        P("")
        print(f"  {col}: null {time.time() - tn:.0f}s")

    P(f"_Runtime {time.time() - t0:.0f}s · {a.n_null} null draws (decay null: {min(a.n_null, 200)}) · "
      f"{a.n_boot} cluster-bootstrap draws (symbol-day)._")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = ROOT / "worklog" / "kairos" / "reports" / f"{stamp}_calibration_persistence"
    out.with_suffix(".md").write_text("\n".join(L) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(results, default=float, indent=1), encoding="utf-8")
    print("\n".join(L))
    print(f"\nreport: {out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
