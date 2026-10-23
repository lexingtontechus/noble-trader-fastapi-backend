#!/usr/bin/env python3
"""
regime_cell_edge_persistence.py — does a regime cell's recent edge predict
its next-period edge? (read-only, theoretical; docs/scopes/
20260923_REGIME_CELL_EDGE_PERSISTENCE.md)

Cell = symbol × HMM vol state (4) × HMM trend state (4). Trend state already
encodes direction × strength ({strong,weak} × {bull,bear}) and signal side is
fully determined by it (direction = short iff "bear" in regime_label), so side
is not a separate axis.

Unit of analysis is an EPISODE — first light-sweep row per
(symbol, side, hour) — not a raw nt_signal_sim row: the light sweep re-emits
the same setup every tick (~6.6 rows/episode), so raw rows overstate n.

R per episode (settlement convention, matches paper_book.compute_realized_pnl):
    tp_hit → +tp_bricks/sl_bricks, sl_hit → −1, expired → 0
Sensitivity variant marks expired to market: pnl_bricks / sl_bricks.

No lookahead: a lag window only uses episodes whose resolved_at is before the
anchor/decision time, never just ts < anchor.

Usage:
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/regime_cell_edge_persistence.py \
      [--cache PATH] [--refresh] [--expired-mtm]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from regime_platform.sweep_orchestrator.bracket_reachability import _vol_tier  # noqa: E402

SHRINK_K = 10.0          # pseudo-episodes pulling a cell toward its parent mean
MIN_LAG, MIN_FWD = 10, 5  # per-cell episode floors for the ranking tests
N_PERM = 500
RNG = np.random.default_rng(7)

GRAINS = {
    "G16 vol4×trend4": ["vol", "trend"],
    "G8 vol4×dir2": ["vol", "direction"],
    "G4 vol2×dir2": ["vol2", "direction"],
    "G4 vol2×strength2": ["vol2", "strength"],
}


# ─── data ──────────────────────────────────────────────────────────────


def fetch(cache: Path) -> list[dict]:
    import httpx

    from regime_platform.core.settings import settings as s

    h = {"apikey": s.SUPABASE_SECRET_KEY, "Authorization": f"Bearer {s.SUPABASE_SECRET_KEY}"}
    url = f"{s.SUPABASE_URL}/rest/v1/nt_signal_sim"
    cols = ("signal_id,symbol,signal,ts,resolved_at,exit_ts,regime,regime_conf,outcome,"
            "pnl_bricks,sl_bricks,tp_bricks,qualified,source,entry_price,stop_loss,"
            "take_profit,exit_price,mfe,mae,hold_bars,drift_4h_pct,drift_24h_pct,"
            "effective_kelly,ev,p_win,p_win_dir,atr")
    rows, off = [], 0
    with httpx.Client(timeout=60) as c:
        while True:
            r = c.get(url, headers=h, params={
                "select": cols, "outcome": "in.(tp_hit,sl_hit,expired)",
                "order": "ts.asc,signal_id.asc", "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            page = r.json()
            rows += page
            if len(page) < 1000:
                break
            off += 1000
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Merge, never overwrite: nightly retention deletes resolved nt_signal_sim
    # rows older than signal_sim_retention_resolved_days (was 30, 120 from 2026-09-23), so the cache is the
    # only place history beyond 30 days survives. Newer copy of a row wins.
    if cache.exists():
        merged = {r["signal_id"]: r for r in json.loads(cache.read_text())}
        before = len(merged)
        merged.update({r["signal_id"]: r for r in rows})
        print(f"cache merge: {before} cached + {len(rows)} fetched → {len(merged)} rows")
        rows = sorted(merged.values(), key=lambda r: (r["ts"], str(r["signal_id"])))
    cache.write_text(json.dumps(rows))
    return rows


def episodes(rows: list[dict], expired_mtm: bool, source: str = "sweep-light") -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df = df[df.source == source].copy()
    df["ts"] = pd.to_datetime(df.ts, utc=True, format="ISO8601")
    df["resolved_at"] = pd.to_datetime(df.resolved_at, utc=True, format="ISO8601")
    df["vol"] = df.regime.map(_vol_tier)
    df["trend"] = df.regime.str.split("_vol_").str[1]
    df = df[(df.vol != "unknown") & df.trend.notna()]
    df["hr"] = df.ts.dt.floor("h")
    ep = df.sort_values("ts").groupby(["symbol", "signal", "hr"], as_index=False).first()
    ep["direction"] = np.where(ep.trend.str.contains("bull"), "bull", "bear")
    ep["strength"] = np.where(ep.trend.str.startswith("strong"), "strong", "weak")
    ep["vol2"] = np.where(ep.vol.isin(["low_vol", "med_low_vol"]), "lo", "hi")
    rr = ep.tp_bricks / ep.sl_bricks.clip(lower=1)
    exp_r = (ep.pnl_bricks / ep.sl_bricks.clip(lower=1)) if expired_mtm else 0.0
    ep["r"] = np.select([ep.outcome == "tp_hit", ep.outcome == "sl_hit"], [rr, -1.0], exp_r)
    ep["win"] = (ep.outcome == "tp_hit").astype(float)
    ep["closed"] = ep.outcome.isin(["tp_hit", "sl_hit"]).astype(float)
    ep["expired"] = (ep.outcome == "expired").astype(float)
    return ep


# ─── cell stats ────────────────────────────────────────────────────────


def cell_table(sub: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Per-cell n / mean R, shrunk toward the cell's own symbol mean."""
    ck = ["symbol"] + keys
    sym = sub.groupby("symbol").r.agg(sym_n="size", sym_mean="mean")
    c = sub.groupby(ck).r.agg(n="size", mean="mean").reset_index().join(sym, on="symbol")
    c["shrunk"] = (c.n * c["mean"] + SHRINK_K * c.sym_mean) / (c.n + SHRINK_K)
    return c


def lag_slice(ep, t, days):
    return ep[(ep.ts >= t - pd.Timedelta(days=days)) & (ep.ts < t) & (ep.resolved_at < t)]


def fwd_slice(ep, t, days, gap=0.0):
    a = t + pd.Timedelta(days=gap)
    return ep[(ep.ts >= a) & (ep.ts < a + pd.Timedelta(days=days))]


# ─── test 1+2: cell-ranking persistence, raw and within-symbol ─────────


def persistence(ep, keys, anchors, lag_d, fwd_d, gap=0.0):
    ck = ["symbol"] + keys
    out = []
    for t in anchors:
        lag = cell_table(lag_slice(ep, t, lag_d), keys)
        fwd = fwd_slice(ep, t, fwd_d, gap)
        f = fwd.groupby(ck).r.agg(fn="size", fmean="mean").reset_index()
        m = lag.merge(f, on=ck)
        m = m[(m.n >= MIN_LAG) & (m.fn >= MIN_FWD)]
        if len(m) < 8:
            continue
        rho = spearmanr(m.shrunk, m.fmean).statistic
        # within-symbol: remove each symbol's own level from both sides
        m["lag_w"] = m.shrunk - m.groupby("symbol").shrunk.transform("mean")
        m["fwd_w"] = m.fmean - m.groupby("symbol").fmean.transform("mean")
        rho_w = spearmanr(m.lag_w, m.fwd_w).statistic
        # permutation null for within-symbol: shuffle forward means across a
        # symbol's own cells (keeps symbol level, destroys cell identity).
        # Rank once, permute rank indices within each symbol, correlate ranks.
        xr = m.lag_w.rank().to_numpy()
        yr = m.fwd_w.rank().to_numpy()
        groups = [np.flatnonzero(m.symbol.to_numpy() == s) for s in m.symbol.unique()]
        perm = np.tile(np.arange(len(m)), (N_PERM, 1))
        for gidx in groups:
            if len(gidx) > 1:
                perm[:, gidx] = gidx[np.argsort(RNG.random((N_PERM, len(gidx))), axis=1)]
        ys = yr[perm]
        xc = xr - xr.mean()
        yc = ys - ys.mean(axis=1, keepdims=True)
        null = (yc @ xc) / (np.sqrt((yc ** 2).sum(axis=1)) * np.sqrt((xc ** 2).sum()))
        p_w = float((null >= rho_w).mean())
        out.append(dict(anchor=t.date(), cells=len(m), rho=rho, rho_within=rho_w, p_within=p_w))
    return pd.DataFrame(out)


def symbol_persistence(ep, anchors, lag_d, fwd_d):
    out = []
    for t in anchors:
        a = lag_slice(ep, t, lag_d).groupby("symbol").r.mean()
        b = fwd_slice(ep, t, fwd_d).groupby("symbol").r.mean()
        j = pd.concat([a, b], axis=1, keys=["lag", "fwd"]).dropna()
        if len(j) >= 8:
            out.append(spearmanr(j.lag, j.fwd).statistic)
    return np.array(out)


# ─── test 3: edge quadrant (level × momentum) ──────────────────────────


def quadrants(ep, keys, anchors, long_d=14, short_d=7, fwd_d=7):
    ck = ["symbol"] + keys
    rows = []
    for t in anchors:
        L = cell_table(lag_slice(ep, t, long_d), keys).rename(columns={"shrunk": "lvl", "n": "nL"})
        S = cell_table(lag_slice(ep, t, short_d), keys).rename(columns={"shrunk": "sht", "n": "nS"})
        m = L[ck + ["lvl", "nL"]].merge(S[ck + ["sht", "nS"]], on=ck)
        m = m[(m.nL >= MIN_LAG) & (m.nS >= 5)]
        m["quad"] = np.select(
            [(m.lvl > 0) & (m.sht >= m.lvl), (m.lvl > 0) & (m.sht < m.lvl),
             (m.lvl <= 0) & (m.sht > m.lvl)],
            ["edge_rising", "edge_decaying", "edge_recovering"], "no_edge")
        fwd = fwd_slice(ep, t, fwd_d).merge(m[ck + ["quad"]], on=ck)
        fwd["anchor"] = t.date()
        rows.append(fwd)
    return pd.concat(rows) if rows else pd.DataFrame()


# ─── test 4: walk-forward gate simulation ──────────────────────────────


def simulate(ep, keys, start, end, lag_d=14):
    """Daily-refreshed gate (a nightly job): each day's episodes are scored
    with cell stats from episodes resolved before that day began."""
    ck = ["symbol"] + keys
    taken = []
    for d in pd.date_range(start, end, freq="D", inclusive="left"):
        today = ep[(ep.ts >= d) & (ep.ts < d + pd.Timedelta(days=1))].copy()
        if today.empty:
            continue
        hist = lag_slice(ep, d, lag_d)
        cell = cell_table(hist, keys)[ck + ["n", "shrunk"]]
        sym = hist.groupby("symbol").r.agg(sn="size", smean="mean").reset_index()
        today = today.merge(cell, on=ck, how="left").merge(sym, on="symbol", how="left")
        taken.append(today)
    s = pd.concat(taken)
    rules = {
        "all episodes (no gate)": pd.Series(True, index=s.index),
        "symbol gate (sym lag mean > 0)": s.smean.fillna(-1) > 0,
        "cell gate (cell shrunk > 0, n≥10)": (s.n.fillna(0) >= MIN_LAG) & (s.shrunk > 0),
        "cell gate, top-third cells only": (s.n.fillna(0) >= MIN_LAG)
            & (s.shrunk > s.shrunk.quantile(2 / 3)),
    }
    res = []
    for name, mask in rules.items():
        x = s[mask]
        res.append(dict(rule=name, n=len(x), share=len(x) / len(s),
                        exp_r=x.r.mean(), total_r=x.r.sum(),
                        win_closed=x.win.sum() / max(x.closed.sum(), 1),
                        pct_expired=x.expired.mean(),
                        qualified_in=int(x.qualified.fillna(False).sum())))
    return pd.DataFrame(res), s


def boot_ci(x, n=2000):
    x = np.asarray(x)
    if len(x) < 5:
        return (np.nan, np.nan)
    b = RNG.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return tuple(np.percentile(b, [2.5, 97.5]))


# ─── main ──────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path,
                    default=ROOT / ".cache" / "regime_cell_edge_persistence_sim.json")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--expired-mtm", action="store_true")
    a = ap.parse_args()

    rows = fetch(a.cache) if (a.refresh or not a.cache.exists()) else json.loads(a.cache.read_text())
    ep = episodes(rows, a.expired_mtm)
    t0, t1 = ep.ts.min().normalize(), ep.ts.max().normalize()
    print(f"episodes={len(ep)}  span={t0.date()}..{t1.date()}  "
          f"expired={'mtm' if a.expired_mtm else '0R'}  mean_r={ep.r.mean():+.3f}")
    print(f"regime share of episodes: {ep.trend.value_counts(normalize=True).round(3).to_dict()}")

    # forward data must be fully resolved: last forward day = t1 - 2d (48h deadline)
    last_fwd = t1 - pd.Timedelta(days=2)
    pd.set_option("display.width", 160)

    print("\n== 1. symbol-level persistence (baseline) — Spearman(lag, fwd) over 14 symbols ==")
    for lag_d, fwd_d in [(7, 7), (14, 7)]:
        anchors = pd.date_range(t0 + pd.Timedelta(days=lag_d), last_fwd - pd.Timedelta(days=fwd_d), freq="D")
        r = symbol_persistence(ep, anchors, lag_d, fwd_d)
        print(f"  lag {lag_d:>2}d → fwd {fwd_d}d: mean rho={r.mean():+.3f}  "
              f"frac>0={np.mean(r > 0):.2f}  anchors={len(r)}")

    print("\n== 2. cell persistence — raw rho, and within-symbol rho (regime info beyond symbol) ==")
    for g, keys in GRAINS.items():
        for lag_d, fwd_d in [(7, 7), (14, 7)]:
            anchors = pd.date_range(t0 + pd.Timedelta(days=lag_d), last_fwd - pd.Timedelta(days=fwd_d), freq="D")
            p = persistence(ep, keys, anchors, lag_d, fwd_d)
            if p.empty:
                print(f"  {g:22s} lag {lag_d:>2}d: insufficient cells")
                continue
            wk = p.iloc[::7]  # ~non-overlapping forward windows
            print(f"  {g:22s} lag {lag_d:>2}d→{fwd_d}d: cells≈{int(p.cells.median()):3d}  "
                  f"rho={p.rho.mean():+.3f}  rho_within={p.rho_within.mean():+.3f} "
                  f"(frac>0 {np.mean(p.rho_within > 0):.2f}, median perm-p {p.p_within.median():.2f}; "
                  f"weekly-spaced rho_within {', '.join(f'{v:+.2f}' for v in wk.rho_within)})")

    print("\n== 3. decay — within-symbol rho vs forward gap (lag 7d, fwd 3.5d) ==")
    for g in ["G16 vol4×trend4", "G8 vol4×dir2"]:
        line = []
        for gap in (0, 3.5, 7):
            anchors = pd.date_range(t0 + pd.Timedelta(days=7),
                                    last_fwd - pd.Timedelta(days=3.5 + gap), freq="D")
            p = persistence(ep, GRAINS[g], anchors, 7, 3.5, gap)
            line.append(f"gap {gap:>3}d: {p.rho_within.mean():+.3f}" if not p.empty else f"gap {gap}d: n/a")
        print(f"  {g:22s} " + " | ".join(line))

    print("\n== 4. edge quadrant (level=14d, momentum=7d−14d) → forward 7d R per episode ==")
    for g in ["G16 vol4×trend4", "G8 vol4×dir2"]:
        anchors = pd.date_range(t0 + pd.Timedelta(days=14), last_fwd - pd.Timedelta(days=7), freq="3.5D")
        q = quadrants(ep, GRAINS[g], anchors)
        if q.empty:
            continue
        q = q.assign(cluster=q.symbol + "|" + q.ts.dt.normalize().astype(str))
        print(f"  {g}  (95% CI: cluster bootstrap over symbol-days)")
        for quad in ["edge_rising", "edge_decaying", "edge_recovering", "no_edge"]:
            x = q[q.quad == quad]
            print(f"    {quad:9s} n={len(x):4d}  R/ep={_fmt(_cluster_ci(x, 'r'))}  "
                  f"win_closed={x.win.sum() / max(x.closed.sum(), 1):.2f}  expired={x.expired.mean():.2f}")

    print("\n== 5. walk-forward gate simulation (daily refresh, 14d lookback) ==")
    sim_start = t0 + pd.Timedelta(days=14)
    for g in ["G16 vol4×trend4", "G8 vol4×dir2", "G4 vol2×dir2"]:
        res, s = simulate(ep, GRAINS[g], sim_start, last_fwd)
        print(f"  {g}  ({sim_start.date()}..{last_fwd.date()}, {len(s)} episodes)")
        for _, r in res.iterrows():
            print(f"    {r.rule:36s} n={r.n:5d} ({r.share:4.0%})  R/ep={r.exp_r:+.3f}  "
                  f"totalR={r.total_r:+8.1f}  win_closed={r.win_closed:.2f}  "
                  f"expired={r.pct_expired:.2f}  qualified={r.qualified_in}")

    print("\n== 6. robustness — gate vs lookback, and quadrant leave-one-symbol-out ==")
    for g in ["G16 vol4×trend4", "G8 vol4×dir2"]:
        for lag in (3, 5, 7, 14):
            res, s = simulate(ep, GRAINS[g], sim_start, last_fwd, lag_d=lag)
            b = res.set_index("rule")
            base = b.loc["all episodes (no gate)"]
            pos = b.loc["cell gate (cell shrunk > 0, n≥10)"]
            neg = s[(s.n.fillna(0) >= MIN_LAG) & (s.shrunk <= 0)]
            print(f"  {g:17s} lag {lag:>2}d: all {base.exp_r:+.3f}  "
                  f"cell>0 {pos.exp_r:+.3f} (n={int(pos.n)})  "
                  f"cell<=0 {neg.r.mean():+.3f} (n={len(neg)})")
    anchors = pd.date_range(t0 + pd.Timedelta(days=14), last_fwd - pd.Timedelta(days=7), freq="3.5D")
    q = quadrants(ep, GRAINS["G8 vol4×dir2"], anchors)
    diffs = [
        q[(q.symbol != s) & (q.quad == "no_edge")].r.mean()
        - q[(q.symbol != s) & (q.quad == "edge_rising")].r.mean()
        for s in q.symbol.unique()
    ]
    print(f"  G8 no_edge−edge_rising R/ep, leave-one-symbol-out: min {min(diffs):+.3f}  max {max(diffs):+.3f}")

    extended(rows, ep, t0, last_fwd)
    return 0


# ─── v2: qualification, quadrant actions, transitions, covariance ──────

QUADS = ["edge_rising", "edge_decaying", "edge_recovering", "no_edge"]
NUM = ["regime_conf", "effective_kelly", "ev", "p_win", "p_win_dir", "entry_price",
       "stop_loss", "take_profit", "mfe", "mae", "atr", "drift_4h_pct", "drift_24h_pct"]


def qualification(rows: list[dict]) -> None:
    """Qualified vs gate-pass. `qualified=false` also covers cooldown
    suppression (orchestrator.py ~3803: 60-min same-direction cooldown +
    block while the prior qualified signal is still open). Gate-pass is
    reconstructed from the stamped values (regime_conf, effective_kelly,
    ev, p_win hard floor 0.50); it omits timesfm-confidence/context gates,
    so gate-pass-not-qualified is an UPPER bound on cooldown suppression."""
    raw = pd.DataFrame(rows)
    raw = raw[raw.source == "sweep-light"].copy()
    raw["ts"] = pd.to_datetime(raw.ts, utc=True, format="ISO8601")
    for c in NUM:
        if c in raw:
            raw[c] = pd.to_numeric(raw[c], errors="coerce")
    pw = raw.p_win_dir.fillna(raw.p_win)
    raw["gate"] = (raw.regime_conf >= 0.6) & (raw.effective_kelly >= 0.02) & (raw.ev > 0) & (pw >= 0.5)
    raw["qualified"] = raw.qualified.fillna(False).astype(bool)
    eh = raw.groupby(["symbol", "signal", raw.ts.dt.floor("h")]).agg(q=("qualified", "max"), g=("gate", "max"))
    print(f"  rows: qualified {raw.qualified.sum()} ({raw.qualified.mean():.2%}) · gate-pass "
          f"{raw.gate.sum()} ({raw.gate.mean():.2%}) · gate-pass-not-qualified "
          f"{(raw.gate & ~raw.qualified).sum()} · qualified-not-gate-pass {(~raw.gate & raw.qualified).sum()}")
    print(f"  episodes: {len(eh)} · with a qualified row {eh.q.mean():.1%} · with a gate-pass row {eh.g.mean():.1%}")


def quadrant_panel(ep: pd.DataFrame, t0, last_fwd, keys=("vol", "direction")) -> pd.DataFrame:
    """Point-in-time quadrant per (symbol × cell × day), stamped onto that
    day's episodes. Level = 14d shrunk R, momentum = 7d vs 14d, both from
    episodes resolved before the day starts."""
    keys = list(keys)
    ck = ["symbol"] + keys
    parts = []
    for d in pd.date_range(t0 + pd.Timedelta(days=14), last_fwd, freq="D", inclusive="left"):
        L = cell_table(lag_slice(ep, d, 14), keys).rename(columns={"shrunk": "lvl", "n": "nL"})
        S = cell_table(lag_slice(ep, d, 7), keys).rename(columns={"shrunk": "sht", "n": "nS"})
        q = L[ck + ["lvl", "nL"]].merge(S[ck + ["sht", "nS"]], on=ck)
        q = q[(q.nL >= MIN_LAG) & (q.nS >= 5)]
        q["quad"] = np.select(
            [(q.lvl > 0) & (q.sht >= q.lvl), (q.lvl > 0) & (q.sht < q.lvl), (q.lvl <= 0) & (q.sht > q.lvl)],
            ["edge_rising", "edge_decaying", "edge_recovering"], "no_edge")
        today = ep[(ep.ts >= d) & (ep.ts < d + pd.Timedelta(days=1))].merge(q[ck + ["quad"]], on=ck)
        today["day"] = d
        parts.append(today)
    s = pd.concat(parts)
    for c in NUM:
        if c in s:
            s[c] = pd.to_numeric(s[c], errors="coerce")
    s["tp_d"] = (s.take_profit - s.entry_price).abs()
    s["sl_d"] = (s.entry_price - s.stop_loss).abs()
    s["rr"] = s.tp_d / s.sl_d
    s["d4"] = s.drift_4h_pct / 100 * s.entry_price / s.atr     # signed drift, ATR units
    s["d24"] = s.drift_24h_pct / 100 * s.entry_price / s.atr
    return s[(s.sl_d > 0) & (s.tp_d > 0) & s.mfe.notna() & s.mae.notna()]


def _cluster_ci(g: pd.DataFrame, col: str, n: int = 2000) -> tuple[float, float, float]:
    """Mean of `col` with a 95% CI from a cluster bootstrap over symbol-days.
    Episodes on the same symbol-day share one price path, so an episode-level
    bootstrap overstates precision (v2 did this; v3 corrects it)."""
    cl = g.groupby("cluster")[col].agg(["sum", "size"])
    idx = RNG.integers(0, len(cl), (n, len(cl)))
    sums, sizes = cl["sum"].to_numpy()[idx].sum(axis=1), cl["size"].to_numpy()[idx].sum(axis=1)
    lo, hi = np.percentile(sums / sizes, [2.5, 97.5])
    return g[col].mean(), lo, hi


def _fmt(t: tuple[float, float, float]) -> str:
    m_, lo, hi = t
    return f"{m_:+.3f} [{lo:+.3f},{hi:+.3f}]{'*' if lo > 0 or hi < 0 else ' '}"


def quadrant_actions(s: pd.DataFrame) -> None:
    """Counterfactual knobs per quadrant, from the resolver's close-based
    MFE/MAE (optimistic for tight stops: no wicks, no spread). R per unit
    of risk actually taken, so a half-size stop is rescaled by 1/0.5.
    Reversal = signed 24h drift (1h-ATR units, signal direction) < 0, reported
    as EXCESS over the symbol's own base rate so a quadrant isn't credited
    with a symbol's drift. `*` = cluster-bootstrap 95% CI excludes 0."""
    s = s.assign(
        cluster=s.symbol + "|" + s.day.astype(str),
        tsl=np.where(s.mae >= 0.5 * s.sl_d, -0.5, s.r) / 0.5 - s.r,
        ttp=np.where(s.mfe >= 0.5 * s.tp_d, 0.5 * s.rr, s.r) - s.r,
    )
    s = s.assign(tsl_x=s.tsl - s.tsl.mean(), ttp_x=s.ttp - s.ttp.mean())
    rv = s[s.d24.notna()].assign(rev=lambda x: (x.d24 < 0).astype(float))
    rv = rv.assign(rev_x=rv.rev - rv.groupby("symbol").rev.transform("mean"))
    print(f"  {s.cluster.nunique()} symbol-day clusters · pooled tight SL {_fmt(_cluster_ci(s, 'tsl'))} · "
          f"pooled tight TP {_fmt(_cluster_ci(s, 'ttp'))} · reversal base {rv.rev.mean():.2f}")
    print("  quad       n   cl  R/ep                     tightSL − pooled         tightTP − pooled"
          "         reversal − symbol base")
    for qd in QUADS:
        g = s[s.quad == qd]
        if len(g) < 20:
            print(f"  {qd:9s} n<20")
            continue
        r = rv[rv.quad == qd]
        print(f"  {qd:9s} {len(g):4d} {g.cluster.nunique():4d}  {_fmt(_cluster_ci(g, 'r'))}  "
              f"{_fmt(_cluster_ci(g, 'tsl_x'))}  {_fmt(_cluster_ci(g, 'ttp_x'))}  "
              f"{r.rev.mean():.2f} {_fmt(_cluster_ci(r, 'rev_x'))}")


def quadrant_transitions(s: pd.DataFrame) -> None:
    cq = s.groupby(["symbol", "vol", "direction", "day"]).quad.first().rename("q").reset_index()
    for h in (1, 3, 7):
        nxt = cq.copy()
        nxt["day"] = nxt.day - pd.Timedelta(days=h)
        j = cq.merge(nxt, on=["symbol", "vol", "direction", "day"], suffixes=("", "_n"))
        T = pd.crosstab(j.q, j.q_n, normalize="index").reindex(index=QUADS, columns=QUADS).fillna(0)
        print(f"  P(quadrant at t+{h}d | quadrant at t), n={len(j)}")
        print("    " + T.round(2).to_string().replace("\n", "\n    "))
    last = cq.day.max()
    snap = pd.crosstab(cq[cq.day == last].symbol, cq[cq.day == last].q).reindex(columns=QUADS).fillna(0).astype(int)
    print(f"  snapshot {last.date()} — scored cells per symbol by quadrant:")
    print("    " + snap.to_string().replace("\n", "\n    "))


def _eff_n(corr: np.ndarray) -> tuple[float, float]:
    ev = np.linalg.eigvalsh(corr)[::-1]
    return ev[0] / ev.sum(), ev.sum() ** 2 / (ev ** 2).sum()


def covariance(rows: list[dict], ep: pd.DataFrame, s: pd.DataFrame) -> None:
    from sklearn.covariance import LedoitWolf

    raw = pd.DataFrame(rows)
    raw = raw[raw.source == "sweep-light"].copy()
    raw["ts"] = pd.to_datetime(raw.ts, utc=True, format="ISO8601")
    raw["entry_price"] = pd.to_numeric(raw.entry_price, errors="coerce")
    px = raw.groupby(["symbol", raw.ts.dt.floor("4h")]).entry_price.last().unstack(0).sort_index()
    ret = np.log(px).diff().dropna(thresh=10)
    C = ret.corr(min_periods=40)
    lw = LedoitWolf().fit(ret.fillna(0).values).covariance_
    sd = np.sqrt(np.diag(lw))
    pc1, n_eff = _eff_n(lw / np.outer(sd, sd))
    print(f"  price (4h log returns from entry marks): PC1 {pc1:.0%} · "
          f"effective independent bets {n_eff:.1f} of {C.shape[0]}")
    pairs = sorted(((a, b, C.loc[a, b]) for i, a in enumerate(C) for b in C.columns[i + 1:]),
                   key=lambda x: -abs(x[2]))[:10]
    print("    strongest: " + ", ".join(f"{a}-{b} {v:+.2f}" for a, b, v in pairs))
    R = ep.assign(day=ep.ts.dt.normalize()).groupby(["day", "symbol"]).r.mean().unstack()
    Rc = R.corr(min_periods=15)
    lw2 = LedoitWolf().fit(R.fillna(0).values).covariance_
    sd2 = np.sqrt(np.diag(lw2))
    pc1b, n_eff2 = _eff_n(lw2 / np.outer(sd2, sd2))
    print(f"  strategy (daily mean R per symbol, {len(R)} days): mean pairwise rho "
          f"{np.nanmean(Rc.values[np.triu_indices(len(Rc), 1)]):+.2f} · PC1 {pc1b:.0%} · effective bets {n_eff2:.1f}")
    QR = s.groupby(["day", "quad"]).r.mean().unstack().reindex(columns=QUADS)
    print(f"  quadrant sleeves (daily mean R, {len(QR)} days): correlation")
    print("    " + QR.corr().round(2).to_string().replace("\n", "\n    "))
    eta = {k: s.groupby(v).r.transform("mean").var() / s.r.var()
           for k, v in {"symbol": "symbol", "quadrant": "quad", "symbol×quadrant": ["symbol", "quad"]}.items()}
    chance = {"symbol": s.symbol.nunique(), "quadrant": 4, "symbol×quadrant": s.groupby(["symbol", "quad"]).ngroups}
    print("  share of episode-R variance explained (in-sample η², vs ≈groups/n by chance): "
          + ", ".join(f"{k} {v:.2%} (chance≈{chance[k] / len(s):.2%})" for k, v in eta.items()))


def regime_dwell(rows: list[dict], ep: pd.DataFrame) -> None:
    """HMM regime persistence at the hourly level (modal label per
    symbol-hour), and whether the entry direction still holds at exit."""
    raw = pd.DataFrame(rows)
    raw = raw[raw.source == "sweep-light"].copy()
    raw["ts"] = pd.to_datetime(raw.ts, utc=True, format="ISO8601")
    raw = raw[raw.regime.map(_vol_tier) != "unknown"]
    hr = (raw.groupby(["symbol", raw.ts.dt.floor("h")]).regime
          .agg(lambda s: s.mode().iat[0]).rename("reg").reset_index().rename(columns={"ts": "hr"}))
    hr["vol"] = hr.reg.map(_vol_tier)
    hr["trend"] = hr.reg.str.split("_vol_").str[1]
    hr["dir"] = np.where(hr.trend.str.contains("bull"), "bull", "bear")
    for h in (1, 4, 12, 24, 48):
        out = {}
        for key in ("reg", "vol", "trend", "dir"):
            hits = []
            for _, g in hr.groupby("symbol"):
                g = g.set_index("hr")[key]
                fut = g.reindex(g.index + pd.Timedelta(hours=h))
                ok = fut.notna().to_numpy()
                hits.append(g.to_numpy()[ok] == fut.to_numpy()[ok])
            out[key] = np.concatenate(hits).mean()
        print(f"  P(same after {h:>2}h): 16-cell {out['reg']:.2f} · vol {out['vol']:.2f} · "
              f"trend {out['trend']:.2f} · direction {out['dir']:.2f}")
    hmap = hr.set_index(["symbol", "hr"]).reg
    ex = pd.to_datetime(ep.exit_ts, utc=True, format="ISO8601", errors="coerce")
    reg_exit = [hmap.get((s, t.floor("h"))) if pd.notna(t) else None for s, t in zip(ep.symbol, ex)]
    k = ep.assign(reg_exit=reg_exit).dropna(subset=["reg_exit"])
    k = k.assign(kept=np.where(k.reg_exit.str.contains("bull"), "bull", "bear") == k.direction)
    t = k.groupby("kept").agg(n=("r", "size"), R=("r", "mean"), tp=("win", "mean"),
                              sl=("outcome", lambda s: (s == "sl_hit").mean()), exp=("expired", "mean"))
    print("  outcome by 'entry direction still the HMM direction at exit':")
    print("    " + t.round(3).to_string().replace("\n", "\n    "))


def _transitions(s: pd.DataFrame, h: int) -> pd.DataFrame:
    cq = s.groupby(["symbol", "vol", "direction", "day"]).quad.first().rename("q").reset_index()
    nxt = cq.assign(day=cq.day - pd.Timedelta(days=h))
    j = cq.merge(nxt, on=["symbol", "vol", "direction", "day"], suffixes=("", "_n"))
    return pd.crosstab(j.q, j.q_n, normalize="index").reindex(index=QUADS, columns=QUADS).fillna(0)


def transition_null(ep: pd.DataFrame, s: pd.DataFrame, t0, last_fwd, n_perm: int = 20) -> None:
    """Rolling 7d/14d windows mechanically produce stickiness and a
    edge_rising→edge_decaying→… rotation even with no edge at all. Null: shuffle R
    within each symbol (destroys any time/cell structure), rebuild the panel,
    re-measure. Only observed − null is information."""
    null = {h: [] for h in (1, 3, 7)}
    dead = []
    for _ in range(n_perm):
        e2 = ep.assign(r=ep.groupby("symbol").r.transform(lambda x: RNG.permutation(x.to_numpy())))
        s2 = quadrant_panel(e2, t0, last_fwd)
        for h in null:
            null[h].append(_transitions(s2, h).to_numpy())
        dead.append(s2[s2.quad == "no_edge"].r.mean() - s2.r.mean())
    for h in null:
        o, nm = _transitions(s, h), pd.DataFrame(np.mean(null[h], axis=0), index=QUADS, columns=QUADS)
        rf, rf_n = o.loc["edge_rising", "edge_decaying"], nm.loc["edge_rising", "edge_decaying"]
        nr, nr_n = o.loc["no_edge", "edge_recovering"], nm.loc["no_edge", "edge_recovering"]
        print(f"  h={h}d  P(stay) observed {np.round(np.diag(o), 2)}  null {np.round(np.diag(nm), 2)}  "
              f"· edge_rising→edge_decaying {rf:.2f} (null {rf_n:.2f})  "
              f"· no_edge→edge_recovering {nr:.2f} (null {nr_n:.2f})")
    lo, hi = np.percentile(dead, [2.5, 97.5])
    print(f"  same-day no_edge R − all: observed {s[s.quad == 'no_edge'].r.mean() - s.r.mean():+.3f} · "
          f"null 95% [{lo:+.3f},{hi:+.3f}] ({n_perm} perms)")


def regime_flip(rows: list[dict], s: pd.DataFrame) -> None:
    """Reversal defined on the REGIME, not price: HMM direction at entry+H
    differs from the entry direction. Excess over each symbol's base rate,
    cluster-bootstrap CI."""
    raw = pd.DataFrame(rows)
    raw = raw[raw.source == "sweep-light"].copy()
    raw["ts"] = pd.to_datetime(raw.ts, utc=True, format="ISO8601")
    raw = raw[raw.regime.map(_vol_tier) != "unknown"]
    hr = raw.groupby(["symbol", raw.ts.dt.floor("h")]).regime.agg(lambda x: x.mode().iat[0])
    dirmap = hr.str.contains("bull").map({True: "bull", False: "bear"})
    s = s.assign(cluster=s.symbol + "|" + s.day.astype(str))
    for h in (4, 24):
        fut = pd.Series([dirmap.get(k) for k in zip(s.symbol, s.hr + pd.Timedelta(hours=h))], index=s.index)
        x = s[fut.notna()].assign(flip=(fut.dropna() != s.loc[fut.notna(), "direction"]).astype(float))
        x = x.assign(fx=x.flip - x.groupby("symbol").flip.transform("mean"))
        cells = [f"{q} {x[x.quad == q].flip.mean():.2f} ({_fmt(_cluster_ci(x[x.quad == q], 'fx'))})" for q in QUADS]
        print(f"  HMM direction flipped by +{h}h: base {x.flip.mean():.2f} | " + " | ".join(cells))


def extended(rows, ep, t0, last_fwd) -> None:
    print("\n== 7. qualification vs gate-pass (cooldown correction) ==")
    qualification(rows)
    print("\n== 7b. HMM regime dwell ==")
    regime_dwell(rows, ep)
    s = quadrant_panel(ep, t0, last_fwd)
    print(f"\n== 8. quadrant → action (G8, daily point-in-time, {len(s)} episodes, "
          f"{s.day.min().date()}..{s.day.max().date()}) ==")
    quadrant_actions(s)
    pw = s.p_win_dir.fillna(s.p_win)
    gp = s[(s.regime_conf >= 0.6) & (s.effective_kelly >= 0.02) & (s.ev > 0) & (pw >= 0.5)]
    print(f"\n== 8b. same, gate-pass cohort only (value gates pass on the episode's first row; "
          f"n={len(gp)}, {len(gp) / len(s):.0%}) ==")
    quadrant_actions(gp)
    print("\n== 9. quadrant transition matrix ==")
    quadrant_transitions(s)
    print("\n== 9b. transitions vs mechanical null (rolling windows alone) ==")
    transition_null(ep, s, t0, last_fwd)
    print("\n== 9c. reversal hypothesis, regime definition ==")
    regime_flip(rows, s)
    print("\n== 10. covariance / correlation view ==")
    covariance(rows, ep, s)


if __name__ == "__main__":
    raise SystemExit(main())
