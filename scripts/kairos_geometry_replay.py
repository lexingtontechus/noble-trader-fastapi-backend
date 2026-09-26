"""Geometry replay: re-walk past signals under alternative TP/SL brackets.

Answers "would a different geometry (e.g. dynamic ATR's bands) have done
better?" directly from history, without waiting for a live shadow.

Holds each past signal's entry, direction and recorded sweep ATR fixed, and
walks the real 1h closes after entry for 48 bars (same close-only first-passage
rule and 48-bar expiry as the live resolver) under:
  * the recorded geometry (validated against the recorded live outcome);
  * a grid of TP x SL in ATR multiples, including dynamic ATR's feasible set
    (TP inside the asset-class band from tiered_atr.TIER_TP_ATR_BAND,
    TP/SL >= min_rr 1.5).

Per geometry: expiry share, TP rate (TP / decided), no-skill line
SL/(TP+SL), edge = TP rate - no-skill, expectancy per signal in ATR units
(TP -> +TP, SL -> -SL, expired -> mark-to-market at bar 48), median hours to
decision. Split by time halves for stability.

Usage:
  PYTHONPATH="$PWD" python -u scripts/kairos_geometry_replay.py [--since 2026-09-01] [--tier all]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from dotenv import load_dotenv  # noqa: E402

load_dotenv("../../.env.local")
from regime_platform.core.settings import Settings  # noqa: E402
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key, preferred_tp_atr_band  # noqa: E402

EXPIRY = 48
TP_GRID = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 9.0, 12.0, 17.0]
SL_GRID = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 9.0]
MIN_RR = 1.5
S = Settings()
U = S.SUPABASE_URL.rstrip("/") + "/rest/v1/"
H = {"apikey": S.SUPABASE_KEY, "Authorization": f"Bearer {S.SUPABASE_KEY}"}


def load_signals(since: str, until: str) -> pd.DataFrame:
    cols = "signal_id,symbol,signal,ts,entry_price,stop_loss,take_profit,atr,outcome,gate_outcome,qualified,p_win"
    rows, off = [], 0
    while True:
        r = httpx.get(U + "nt_signal_sim", headers=H, timeout=60, params={
            "select": cols, "source": "eq.sweep-light", "signal": "in.(buy,sell)",
            "and": f"(ts.gte.{since},ts.lt.{until})", "order": "ts.asc",
            "limit": "1000", "offset": str(off)})
        r.raise_for_status()
        b = r.json(); rows += b; off += len(b)
        if len(b) < 1000:
            break
    df = pd.DataFrame(rows)
    for c in ("entry_price", "stop_loss", "take_profit", "atr"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df.ts, utc=True, format="ISO8601")
    df = df[(df.atr > 0) & df.entry_price.notna() & df.stop_loss.notna() & df.take_profit.notna()].copy()
    df["tier"] = df.gate_outcome.fillna(pd.Series(np.where(df.qualified.fillna(False).astype(bool), "qualified", "unstamped"), index=df.index))
    # one signal per symbol x direction x hour (the 15-min sweeps repeat the same call)
    df["hour"] = df.ts.dt.floor("h")
    df = df.drop_duplicates(["symbol", "signal", "hour"], keep="first")
    return df


async def load_bars(symbols: list[str], n: int) -> dict[str, pd.DataFrame]:
    r = httpx.get(U + "nt_symbol", headers=H, timeout=30,
                  params={"select": "symbol,tradingview_symbol,asset_class", "active": "eq.true"})
    r.raise_for_status()
    meta = {x["symbol"]: x for x in r.json()}
    specs = [{"symbol": s, "tradingview_symbol": meta[s]["tradingview_symbol"]} for s in symbols if s in meta]
    ohlcv: dict = {}
    await fetch_tradingview_batch(specs, hours=n, interval="1h", ohlcv_out=ohlcv)
    out = {}
    for s, bars in ohlcv.items():
        b = pd.DataFrame(bars).dropna(subset=["t", "c"])
        b["t"] = pd.to_numeric(b.t)
        out[s] = b.sort_values("t").reset_index(drop=True)
    return out, {s: meta[s].get("asset_class") for s in meta}


def first_hit(path: np.ndarray, level: float) -> int:
    """Index of the first bar whose (direction-signed, ATR-scaled) close reaches level; 99 if never."""
    idx = np.flatnonzero(path >= level) if level > 0 else np.flatnonzero(path <= level)
    return int(idx[0]) if idx.size else 99


def build_paths(df: pd.DataFrame, bars: dict) -> tuple[np.ndarray, pd.DataFrame]:
    P, keep = [], []
    tnum = {s: b.t.to_numpy(dtype="float64") for s, b in bars.items()}
    for i, r in enumerate(df.itertuples()):
        b = bars.get(r.symbol)
        if b is None:
            continue
        # entry bar = the bar in progress when the signal fired (live resolver anchors the same way)
        j = int(np.searchsorted(tnum[r.symbol], int(r.ts.timestamp()), side="right")) - 1
        if j < 0 or j + EXPIRY > len(b):
            continue
        c = b.c.to_numpy(float)[j:j + EXPIRY]
        d = 1.0 if r.signal == "buy" else -1.0
        P.append((c - r.entry_price) * d / r.atr)
        keep.append(i)
    return np.vstack(P), df.iloc[keep].reset_index(drop=True)


def walk(P: np.ndarray, tp: np.ndarray, sl: np.ndarray):
    """Vector first-passage for per-signal tp/sl (ATR units). TP-first on a same-bar tie (live policy)."""
    n = len(P)
    ti = np.full(n, 99); si = np.full(n, 99)
    hit_tp = P >= tp[:, None]; hit_sl = P <= -sl[:, None]
    a = hit_tp.any(1); ti[a] = hit_tp[a].argmax(1)
    b = hit_sl.any(1); si[b] = hit_sl[b].argmax(1)
    out = np.where((ti < 99) & (ti <= si), "tp_hit", np.where(si < 99, "sl_hit", "expired"))
    hrs = np.where(out == "tp_hit", ti, np.where(out == "sl_hit", si, np.nan)) + 1
    # Realised exit = the triggering close (close-only walk, as live). Booking
    # the nominal ±TP/±SL instead ignores overshoot: mean |Δclose| is 1 ATR by
    # construction, so a 0.5-ATR stop is typically overshot by more than its
    # own width — nominal booking made tight stops look profitable.
    hit = np.where(out == "tp_hit", ti, np.where(out == "sl_hit", si, EXPIRY - 1))
    pnl = P[np.arange(n), hit]
    return out, hrs, pnl


def score(out, hrs, pnl, tp, sl, cost_atr: float = 0.0) -> dict:
    dec = out != "expired"; nd = int(dec.sum())
    tpr = float((out == "tp_hit").sum() / nd) if nd else np.nan
    ns = float(np.mean((sl / (tp + sl))[dec])) if nd else np.nan
    # round-trip cost charged on every entry (expired trades are closed at bar 48 too)
    net = pnl - cost_atr
    return {"n": len(out), "expiry": float((~dec).mean()), "decided": nd, "tp_rate": tpr,
            "no_skill": ns, "edge": tpr - ns, "exp_atr": float(np.mean(net)),
            "med_h": float(np.nanmedian(hrs)) if nd else np.nan}


# ─── Walk-forward selection (2026-09-26, offline evidence for dynamic ATR) ─
# Scope §14.5. Picks one TP/SL (ATR units)
# per asset class on one time half and scores it on the other, both ways.
WF_TP = [3.0, 4.0, 6.0, 9.0, 12.0, 17.0, 24.0]
WF_SL = [1.0, 1.5, 2.0, 3.0]          # ≥ 1 ATR: a sub-ATR stop is inside one bar's noise
WF_MAX_EXPIRY = 0.25                   # keep the 48h contract meaningful


def select_geometry(P: np.ndarray, m: np.ndarray, cost_atr: float) -> tuple[float, float, dict]:
    best = None
    for tpk in WF_TP:
        for slk in WF_SL:
            if tpk / slk < MIN_RR:
                continue
            tp = np.full(int(m.sum()), tpk); sl = np.full(int(m.sum()), slk)
            s = score(*walk(P[m], tp, sl), tp, sl, cost_atr)
            if s["expiry"] > WF_MAX_EXPIRY:
                continue
            if best is None or s["exp_atr"] > best[2]["exp_atr"]:
                best = (tpk, slk, s)
    return best


def walk_forward(P: np.ndarray, df: pd.DataFrame, tp0: np.ndarray, sl0: np.ndarray, cost_atr: float) -> list[str]:
    half = (df.ts < df.ts.min() + (df.ts.max() - df.ts.min()) / 2).to_numpy()
    L = [f"## 7. Walk-forward per asset class (fit one half, score the other; cost {cost_atr:g} ATR/trade)", "",
         "| class | fit→test | picked TP/SL | test exp | test expiry | recorded exp (test) | Δ |", "|" + "---|" * 7]
    picks: dict[str, list] = {}
    for ac in sorted(df.ac.unique()):
        acm = (df.ac == ac).to_numpy()
        for name, fit, test in (("h1→h2", half, ~half), ("h2→h1", ~half, half)):
            f, t = acm & fit, acm & test
            if f.sum() < 50 or t.sum() < 50:
                continue
            tpk, slk, _ = select_geometry(P, f, cost_atr)
            tp = np.full(int(t.sum()), tpk); sl = np.full(int(t.sum()), slk)
            st = score(*walk(P[t], tp, sl), tp, sl, cost_atr)
            sr = score(*walk(P[t], tp0[t], sl0[t]), tp0[t], sl0[t], cost_atr)
            picks.setdefault(ac, []).append((tpk, slk))
            L.append(f"| {ac} | {name} | {tpk:g}/{slk:g} | {st['exp_atr']:+.3f} | {st['expiry']:.0%} | "
                     f"{sr['exp_atr']:+.3f} | {st['exp_atr'] - sr['exp_atr']:+.3f} |")
        allm = acm
        tpk, slk, sa = select_geometry(P, allm, cost_atr)
        picks.setdefault(ac, []).append(("full", tpk, slk, round(sa["exp_atr"], 3)))
    L += ["", "Full-period pick per class (the value to configure): " +
          ", ".join(f"{ac} {p[-1][1]:g}/{p[-1][2]:g} ({p[-1][3]:+.3f})" for ac, p in picks.items()), ""]
    return L


def pwin_strata(P: np.ndarray, df: pd.DataFrame, tpk: float, slk: float, cost_atr: float) -> list[str]:
    pw = pd.to_numeric(df.p_win, errors="coerce").to_numpy()
    L = [f"## 8. Does the directional p_win gate add value at {tpk:g}/{slk:g}?", "",
         "| p_win band | n | TP rate | expiry | exp/ATR |", "|---|---|---|---|---|"]
    for lo, hi in ((0, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01)):
        m = (pw >= lo) & (pw < hi)
        if m.sum() < 30:
            continue
        tp = np.full(int(m.sum()), tpk); sl = np.full(int(m.sum()), slk)
        s = score(*walk(P[m], tp, sl), tp, sl, cost_atr)
        L.append(f"| {lo:.2f}–{min(hi, 1):.2f} | {int(m.sum())} | {s['tp_rate']:.3f} | {s['expiry']:.0%} | {s['exp_atr']:+.3f} |")
    return L + [""]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01T00:00:00Z")
    ap.add_argument("--tier", default="all", help="all | qualified | suppressed | gate_fail | q+s")
    ap.add_argument("--out", default="worklog/kairos/reports/20260926_geometry_replay.md")
    ap.add_argument("--cost", type=float, nargs="+", default=[0.0, 0.1],
                    help="round-trip cost per trade in ATR units for the walk-forward (spread + slippage)")
    a = ap.parse_args()
    until = (datetime.now(timezone.utc) - timedelta(hours=EXPIRY + 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    df = load_signals(a.since, until)
    bars, aclass = asyncio.run(load_bars(sorted(df.symbol.unique()), 1500))
    P, df = build_paths(df, bars)
    if a.tier == "q+s":
        m = df.tier.isin(["qualified", "suppressed"]).to_numpy()
    elif a.tier != "all":
        m = (df.tier == a.tier).to_numpy()
    else:
        m = np.ones(len(df), bool)
    P, df = P[m], df[m].reset_index(drop=True)
    df["ac"] = df.symbol.map(lambda s: asset_class_key(aclass.get(s)))
    half = df.ts < df.ts.min() + (df.ts.max() - df.ts.min()) / 2
    L = [f"# Geometry replay ({a.since[:10]} → {until[:10]}, tier={a.tier})", "",
         f"{len(df):,} signals (one per symbol × direction × hour), {df.symbol.nunique()} symbols, "
         "48-bar close-only walk, same rule as the live resolver. Units: recorded sweep ATR (close-to-close, 14×1h).", ""]

    # 1. recorded geometry + parity with live outcomes
    tp0 = (df.take_profit - df.entry_price).abs().to_numpy() / df.atr.to_numpy()
    sl0 = (df.stop_loss - df.entry_price).abs().to_numpy() / df.atr.to_numpy()
    o0, h0, p0 = walk(P, tp0, sl0)
    live = df.outcome.to_numpy()
    lm = pd.notna(live)
    parity = float((o0[lm] == live[lm]).mean()) if lm.any() else np.nan
    r0 = score(o0, h0, p0, tp0, sl0)
    L += ["## 1. Recorded geometry", "",
          f"Median TP {np.median(tp0):.1f} ATR, SL {np.median(sl0):.1f} ATR (TP/SL {np.median(tp0 / sl0):.2f}). "
          f"Replay matches the recorded live outcome on **{parity:.1%}** of {int(lm.sum()):,} closed signals.", ""]

    # 2. grid
    rows = []
    for tpk in TP_GRID:
        for slk in SL_GRID:
            tp = np.full(len(df), tpk); sl = np.full(len(df), slk)
            o, h, p = walk(P, tp, sl)
            s = score(o, h, p, tp, sl)
            s1 = score(*[x[half.to_numpy()] for x in (o, h, p, tp, sl)])
            s2 = score(*[x[~half.to_numpy()] for x in (o, h, p, tp, sl)])
            rows.append({"tp": tpk, "sl": slk, **s, "exp_h1": s1["exp_atr"], "exp_h2": s2["exp_atr"],
                         "edge_h1": s1["edge"], "edge_h2": s2["edge"]})
    G = pd.DataFrame(rows)
    G["dyn_feasible"] = False
    # dynamic ATR feasible set per asset class (share of signals whose class band admits tp, and tp/sl >= min_rr)
    for i, g in G.iterrows():
        ok = [lo <= g.tp <= hi for lo, hi in (preferred_tp_atr_band(ac) for ac in df.ac.unique())]
        G.loc[i, "dyn_feasible"] = any(ok) and g.tp / g.sl >= MIN_RR
    fmt = lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x)  # noqa: E731
    cols = ["tp", "sl", "expiry", "tp_rate", "no_skill", "edge", "exp_atr", "exp_h1", "exp_h2", "med_h"]

    def table(t: pd.DataFrame) -> list[str]:
        out = ["| TP | SL | expiry | TP rate | no-skill | edge | exp/ATR | exp h1 | exp h2 | med h |", "|" + "---|" * 10]
        for r in t.itertuples():
            out.append(f"| {r.tp:g} | {r.sl:g} | {r.expiry:.0%} | {r.tp_rate:.3f} | {r.no_skill:.3f} | {r.edge:+.3f} | "
                       f"{r.exp_atr:+.3f} | {r.exp_h1:+.3f} | {r.exp_h2:+.3f} | {r.med_h:.0f} |")
        return out

    rec = {"tp": float(np.median(tp0)), "sl": float(np.median(sl0)), **r0}
    L += [f"Recorded: expiry {r0['expiry']:.0%}, TP rate {r0['tp_rate']:.3f}, no-skill {r0['no_skill']:.3f}, "
          f"edge {r0['edge']:+.3f}, expectancy {r0['exp_atr']:+.3f} ATR/signal, median {r0['med_h']:.0f}h to decision.", ""]
    L += ["## 2. Best geometries by expectancy (whole grid)", ""] + table(G.sort_values("exp_atr", ascending=False).head(12)) + [""]
    L += ["## 3. Dynamic ATR feasible set (TP in class band, TP/SL ≥ 1.5)", ""] + \
        table(G[G.dyn_feasible].sort_values("exp_atr", ascending=False)) + [""]
    L += ["## 4. SL ≥ TP shapes (outside dynamic ATR)", ""] + \
        table(G[G.sl >= G.tp].sort_values("exp_atr", ascending=False).head(10)) + [""]
    L += ["## 5. Edge vs no-skill across the grid (does direction carry skill at any geometry?)", "",
          f"Edge range {G.edge.min():+.3f} … {G.edge.max():+.3f}; share of geometries with edge > 0: "
          f"{(G.edge > 0).mean():.0%} (h1 {(G.edge_h1 > 0).mean():.0%}, h2 {(G.edge_h2 > 0).mean():.0%}).", ""]
    # 6. per symbol: best geometry vs recorded
    L += ["## 6. Per symbol: recorded vs best grid geometry (expectancy, ATR/signal)", "",
          "| symbol | n | rec TP/SL | rec exp | rec expiry | best TP/SL | best exp | best expiry | best in dyn set |", "|" + "---|" * 9]
    for sym, idx in df.groupby("symbol").groups.items():
        ix = np.asarray(list(idx))
        o, h, p = walk(P[ix], tp0[ix], sl0[ix]); rs = score(o, h, p, tp0[ix], sl0[ix])
        best, bdyn = None, None
        lo, hi = preferred_tp_atr_band(df.ac.iloc[ix[0]])
        for tpk in TP_GRID:
            for slk in SL_GRID:
                tp = np.full(len(ix), tpk); sl = np.full(len(ix), slk)
                s = score(*walk(P[ix], tp, sl), tp, sl)
                if best is None or s["exp_atr"] > best[2]["exp_atr"]:
                    best = (tpk, slk, s)
                if lo <= tpk <= hi and tpk / slk >= MIN_RR and (bdyn is None or s["exp_atr"] > bdyn[2]["exp_atr"]):
                    bdyn = (tpk, slk, s)
        L.append(f"| {sym} | {len(ix)} | {np.median(tp0[ix]):.1f}/{np.median(sl0[ix]):.1f} | {rs['exp_atr']:+.2f} | {rs['expiry']:.0%} | "
                 f"{best[0]:g}/{best[1]:g} | {best[2]['exp_atr']:+.2f} | {best[2]['expiry']:.0%} | "
                 f"{bdyn[0]:g}/{bdyn[1]:g} {bdyn[2]['exp_atr']:+.2f} |" if bdyn else "| – |")
    L += ["", "Caveats: entries/directions held fixed (a new geometry would change which signals pass the gates); "
          "grid picked in-sample (see h1/h2); close-only walk like live; ATR is the recorded close-to-close sweep ATR.", ""]
    for c in a.cost:
        L += walk_forward(P, df, tp0, sl0, c)
    L += pwin_strata(P, df, 12.0, 1.5, a.cost[-1])
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
