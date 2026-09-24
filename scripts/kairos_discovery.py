#!/usr/bin/env python3
# ruff: noqa: E501  (inline SVG/HTML templates and report lines)
"""
kairos_discovery.py — Kairos v3 P1 discovery (offline, geometry-free, read-only).

docs/scopes/20260924_KAIROS_V3_SCOPE.md §5.0 (frozen definitions) and §6 P1.
No SL/TP, R, W/L or P/L anywhere: the only outcome is the signed forward move
in ATR units, m_h = d · (C[t+h] − C[t]) / ATR_t.

Steps
  1. Debounced labels: cached cold-fit RegimeHMM labels (.cache/recon_<SYM>_labels.json)
     through RegimeLabelDebouncer, n_confirm calibrated to the live post-debounce
     hourly cell flip rate (0.474/h).
  2. Runs / regime age (Clock A), edge curves m_h (Clock B).
  3. Quadrant plane X (14d shrunk m_24 of the current cell), Y = X7 − X14;
     trajectories, flow field, exit compass.
  4. V1–V4 with the frozen readings; circular-shift label null (1,000 draws).
  5. Factor panel (§3.8 catalogue from TDVA OHLCV + calendar), descriptive only
     (V7 is P2).
  6. Artefacts: .cache/kairos/p1_*.{json,csv,pkl} and a data-driven edge-plane HTML.

Usage
  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/kairos_discovery.py \
      [--symbols EURUSD,XAUUSD,US500,BTCUSD,USDJPY] [--n-null 1000] [--n-boot 2000] \
      [--refresh-volume] [--html docs/scopes/assets/20260924_kairos_edge_plane_discovery.html]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
KCACHE = CACHE / "kairos"
SYMS = ["EURUSD", "XAUUSD", "US500", "BTCUSD", "USDJPY"]
TV = {"EURUSD": "OANDA:EURUSD", "XAUUSD": "OANDA:XAUUSD", "US500": "PEPPERSTONE:US500",
      "BTCUSD": "COINBASE:BTCUSD", "USDJPY": "OANDA:USDJPY"}
CCY = {"EURUSD": ("EUR", "USD"), "XAUUSD": ("USD",), "US500": ("USD",), "BTCUSD": ("USD",),
       "USDJPY": ("USD", "JPY")}
H = (1, 4, 12, 24, 48)
HREF = 24
LIVE_FLIP = 0.474
VOLS = ["low_vol", "med_low_vol", "med_high_vol", "high_vol"]
CELLS = [f"{v}|{d}" for v in VOLS for d in ("bull", "bear")]
BANDS = [0, 6, 12, 24, 48, np.inf]
BAND_LAB = ["0–6h", "6–12h", "12–24h", "24–48h", "≥48h"]
STATES = ["edge_rising", "edge_decaying", "edge_recovering", "no_edge"]
SHRINK_K = 10
RNG = np.random.default_rng(924)


# ─── 1. data ──────────────────────────────────────────────────────────────


async def _fetch_volume(tv: str) -> dict[int, float]:
    import httpx

    from regime_platform.core.settings import settings as s
    h = {"x-rapidapi-key": s.TRADINGVIEW_API_KEY, "x-rapidapi-host": s.TRADINGVIEW_API_HOST,
         "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{s.TRADINGVIEW_BASE_URL}/api/price/batch", headers=h,
                         json={"requests": [{"symbol": tv, "timeframe": "60", "range": 20000}]})
    r.raise_for_status()
    item = r.json()["data"]["data"][0]
    hist = item.get("data", item).get("history", [])
    out = {}
    for b in hist:
        t = float(b["time"])
        t = t / 1000 if t > 1e11 else t
        if b.get("volume") is not None:
            out[int(t)] = float(b["volume"])
    return out


def load_symbol(sym: str, refresh_volume: bool) -> tuple[pd.DataFrame, list]:
    bars = json.loads((CACHE / f"recon_{sym}_bars_1h.json").read_text())
    lab = json.loads((CACHE / f"recon_{sym}_labels.json").read_text())
    if lab["n_bars"] != len(bars):
        raise SystemExit(f"{sym}: label cache ({lab['n_bars']}) ≠ bars ({len(bars)}) — rerun reconstruct")
    vf = KCACHE / f"volume_{sym}_1h.json"
    if refresh_volume or not vf.exists():
        vol = asyncio.run(_fetch_volume(TV[sym]))
        KCACHE.mkdir(parents=True, exist_ok=True)
        vf.write_text(json.dumps({str(k): v for k, v in vol.items()}))
    vol = {int(k): v for k, v in json.loads(vf.read_text()).items()}
    df = pd.DataFrame(bars)
    df["v"] = df.t.map(vol)
    return df, lab["labels"]


def debounce(raw: list[str | None], n: int) -> list[str | None]:
    from regime_platform.core.regime_engine import RegimeLabelDebouncer
    d = RegimeLabelDebouncer(n)
    out, cur = [], None
    for x in raw:
        if x is not None:
            cur = d.update(x)
        out.append(cur)
    return out


def cell_of(label: str | None) -> str | None:
    from regime_platform.sweep_orchestrator.kairos import regime_cell
    return regime_cell(label)


def flip_rate(cells: list[str | None], ts: np.ndarray) -> tuple[int, int]:
    c = np.array([x if x else "" for x in cells], dtype=object)
    cont = np.r_[False, np.diff(ts) == 3600] & (c != "") & np.r_[False, c[:-1] != ""]
    flips = cont & np.r_[False, c[1:] != c[:-1]]
    return int(flips.sum()), int(cont.sum())


# ─── 2. per-symbol arrays ─────────────────────────────────────────────────


def build_arrays(sym: str, df: pd.DataFrame, cells: list[str | None]) -> dict:
    from regime_platform.sweep_orchestrator.heavy_sweep import compute_atr

    n = len(df)
    ts = df.t.to_numpy(dtype=np.int64)
    c = df.c.to_numpy(float)
    atr = np.full(n, np.nan)
    for t in range(14, n):
        a = compute_atr(c[t - 14: t + 1].tolist(), period=14)
        atr[t] = a if a > 0 else np.nan
    move = {}
    for h in H:
        mv = np.full(n, np.nan)
        mv[: n - h] = (c[h:] - c[: n - h]) / atr[: n - h]
        move[h] = mv
    cell_idx = np.array([CELLS.index(x) if x in CELLS else -1 for x in cells], dtype=int)
    d = np.where(cell_idx >= 0, np.where(cell_idx % 2 == 0, 1.0, -1.0), 0.0)
    gap_before = np.r_[True, np.diff(ts) > 3 * 3600]
    return {"sym": sym, "n": n, "ts": ts, "c": c, "atr": atr, "move": move, "cell": cell_idx, "d": d,
            "gap_before": gap_before, "df": df,
            "lo14": np.searchsorted(ts, ts - 14 * 86400, side="left"),
            "lo7": np.searchsorted(ts, ts - 7 * 86400, side="left"),
            "day": (ts // 86400).astype(np.int64)}


def runs_of(cell: np.ndarray, ts: np.ndarray, gap_before: np.ndarray) -> dict:
    """Run segmentation. Returns per-bar run id/age/left-censored flag and a run table."""
    n = len(cell)
    new = gap_before.copy()
    new[1:] |= cell[1:] != cell[:-1]
    new |= cell < 0
    rid = np.cumsum(new) - 1
    start_idx = np.zeros(n, dtype=int)
    starts = np.flatnonzero(new)
    start_idx = starts[rid]
    age_h = (ts - ts[start_idx]) / 3600.0
    left_cens = gap_before[start_idx]
    left_cens[0: 1] = True
    # run table
    ends = np.r_[starts[1:] - 1, n - 1]
    tab = pd.DataFrame({"start": starts, "end": ends})
    tab["cell"] = cell[starts]
    tab["len_bars"] = tab.end - tab.start + 1
    tab["len_h"] = (ts[tab.end] - ts[tab.start]) / 3600.0 + 1.0
    tab["left_cens"] = gap_before[starts]
    tab.loc[0, "left_cens"] = True
    nxt = np.r_[starts[1:], -1]
    tab["next_start"] = nxt
    obs_end = (nxt >= 0) & ~np.r_[gap_before[starts[1:]], True]
    tab["obs_end"] = obs_end
    tab["next_cell"] = np.where(nxt >= 0, cell[np.maximum(nxt, 0)], -1)
    tab = tab[tab.cell >= 0].reset_index(drop=True)
    return {"rid": rid, "start_idx": start_idx, "age_h": age_h, "left_cens": left_cens, "table": tab}


# ─── 3. plane ─────────────────────────────────────────────────────────────


def plane(cell: np.ndarray, m24: np.ndarray, lo14: np.ndarray, lo7: np.ndarray):
    n = len(cell)
    ok = np.isfinite(m24) & (cell >= 0)
    hi = np.clip(np.arange(n) - (HREF - 1), 0, n)            # s ≤ t−24 → s < t−23
    w = np.where(ok, m24, 0.0)
    SA = np.r_[0.0, np.cumsum(w)]
    NA = np.r_[0.0, np.cumsum(ok.astype(float))]

    def win(S, lo):
        return S[hi] - S[np.minimum(lo, hi)]

    sa14, na14, sa7, na7 = win(SA, lo14), win(NA, lo14), win(SA, lo7), win(NA, lo7)
    mu14 = np.where(na14 > 0, sa14 / np.maximum(na14, 1), 0.0)
    mu7 = np.where(na7 > 0, sa7 / np.maximum(na7, 1), 0.0)
    sc14, nc14, sc7, nc7 = (np.zeros(n) for _ in range(4))
    for k in range(len(CELLS)):
        mask = cell == k
        if not mask.any():
            continue
        Ik = ok & mask
        S = np.r_[0.0, np.cumsum(np.where(Ik, m24, 0.0))]
        C = np.r_[0.0, np.cumsum(Ik.astype(float))]
        sc14[mask], nc14[mask] = win(S, lo14)[mask], win(C, lo14)[mask]
        sc7[mask], nc7[mask] = win(S, lo7)[mask], win(C, lo7)[mask]
    X14 = (sc14 + SHRINK_K * mu14) / (nc14 + SHRINK_K)
    X7 = (sc7 + SHRINK_K * mu7) / (nc7 + SHRINK_K)
    Y = X7 - X14
    defined = (nc14 >= 10) & (nc7 >= 5) & (cell >= 0)
    state = np.where(X14 > 0, np.where(Y >= 0, 0, 1), np.where(Y >= 0, 2, 3))
    return X14, Y, defined, state


def flow_steps(ts, X, Y, defined, state, rid=None):
    ok = defined[:-1] & defined[1:] & (np.diff(ts) == 3600)
    idx = np.flatnonzero(ok)
    out = {"dx": X[idx + 1] - X[idx], "dy": Y[idx + 1] - Y[idx], "x": X[idx], "y": Y[idx],
           "region": state[idx]}
    if rid is not None:
        out["within_run"] = rid[idx + 1] == rid[idx]
    return out


def region_speed(steps: list[dict]) -> np.ndarray:
    dx = np.concatenate([s["dx"] for s in steps])
    dy = np.concatenate([s["dy"] for s in steps])
    rg = np.concatenate([s["region"] for s in steps])
    out = np.zeros(4)
    for r in range(4):
        m = rg == r
        out[r] = math.hypot(dx[m].mean(), dy[m].mean()) if m.sum() > 30 else np.nan
    return out


# ─── 4. statistics helpers ────────────────────────────────────────────────


def cluster_boot_mean(vals: np.ndarray, clu: np.ndarray, n_boot: int):
    u, inv = np.unique(clu, return_inverse=True)
    s = np.bincount(inv, weights=vals, minlength=len(u))
    c = np.bincount(inv, minlength=len(u)).astype(float)
    idx = RNG.integers(0, len(u), (n_boot, len(u)))
    b = s[idx].sum(1) / c[idx].sum(1)
    est = s.sum() / c.sum()
    lo, hi = np.percentile(b, [2.5, 97.5])
    return est, lo, hi, float((b <= 0).mean())


def cluster_boot_slope(x, y, clu, n_boot):
    u, inv = np.unique(clu, return_inverse=True)
    A = np.stack([np.bincount(inv, weights=w, minlength=len(u)) for w in (x, y, x * y, x * x, np.ones_like(x))], 1)

    def slope(T):
        sx, sy, sxy, sxx, nn = (T[..., i] for i in range(5))
        return (sxy - sx * sy / nn) / (sxx - sx * sx / nn)

    est = slope(A.sum(0))
    b = slope(A[RNG.integers(0, len(u), (n_boot, len(u)))].sum(1))
    lo, hi = np.percentile(b, [2.5, 97.5])
    return float(est), float(lo), float(hi), float((b >= 0).mean())


def logistic_agg(x: np.ndarray, ev: np.ndarray, nr: np.ndarray, iters: int = 50):
    m = nr > 0
    x, ev, nr = x[m], ev[m], nr[m]
    b = np.array([math.log(max(ev.sum(), 1e-9) / max(nr.sum() - ev.sum(), 1e-9)), 0.0])
    X = np.stack([np.ones_like(x), x], 1)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ b)))
        W = nr * p * (1 - p)
        g = X.T @ (ev - nr * p)
        Hm = (X * W[:, None]).T @ X
        try:
            step = np.linalg.solve(Hm, g)
        except np.linalg.LinAlgError:
            break
        b += step
        if np.abs(step).max() < 1e-9:
            break
    return b


def hazard_counts(L: np.ndarray, ev: np.ndarray, w: np.ndarray, shift: int, kmax: int):
    """At-risk / event counts on the shifted clock k' = k − shift (k = bar age from 0)."""
    Ls = L - shift                                 # bars at risk after the debounce minimum
    keep = Ls >= 1
    Ls, evs, ws = Ls[keep], ev[keep], w[keep]
    Lc = np.minimum(Ls, kmax)
    at = np.bincount(Lc, weights=ws, minlength=kmax + 1)
    at_risk = at[::-1].cumsum()[::-1][1:]          # runs with Ls > k
    evk = np.bincount(np.where(evs & (Ls <= kmax), Ls - 1, kmax + 5), weights=ws, minlength=kmax + 6)[:kmax]
    return at_risk[:kmax], evk


def ks_geometric(L: np.ndarray, n_sim: int = 1000):
    L = L[L >= 1].astype(int)
    if len(L) < 20:
        return np.nan, np.nan
    p = 1.0 / L.mean()

    def D(sample, pp):
        k = np.arange(1, sample.max() + 1)
        emp = np.searchsorted(np.sort(sample), k, side="right") / len(sample)
        return float(np.abs(emp - (1 - (1 - pp) ** k)).max())

    d0 = D(L, p)
    sims = []
    for _ in range(n_sim):
        s = RNG.geometric(p, len(L))
        sims.append(D(s, 1.0 / s.mean()))
    return d0, float((np.array(sims) >= d0).mean())


def holm(ps: dict) -> dict:
    items = sorted(ps.items(), key=lambda kv: (np.nan_to_num(kv[1], nan=1.0)))
    m, run, out = len(items), 0.0, {}
    for i, (k, p) in enumerate(items):
        p = 1.0 if p is None or not np.isfinite(p) else p
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def band(age: np.ndarray) -> np.ndarray:
    return np.digitize(age, BANDS[1:-1], right=False)


# ─── 5. factor panel (descriptive; V7 is P2) ──────────────────────────────


def factor_panel(A: dict, R: dict, cal: list[dict]) -> pd.DataFrame:
    from regime_platform.sweep_orchestrator.kairos import clv as _clv  # noqa: F401 (definition parity)

    df = A["df"].copy()
    atr = pd.Series(A["atr"])
    c, h, low, o, v = df.c, df.h, df.l, df.o, df.v
    ts = pd.to_datetime(df.t, unit="s", utc=True)
    p = pd.DataFrame({"t": df.t})
    for k in (1, 4, 24):
        p[f"roc_{k}_atr"] = (c - c.shift(k)) / atr
    p["accel_4"] = p.roc_4_atr - p.roc_4_atr.shift(4)
    for s in (20, 50):
        p[f"stretch_ema{s}"] = (c - c.ewm(span=s, adjust=False).mean()) / atr
    p["efficiency_24"] = (c - c.shift(24)).abs() / c.diff().abs().rolling(24).sum()
    start = R["start_idx"]
    p["atr_ratio_run"] = atr.to_numpy() / atr.to_numpy()[start]
    p["dist_run_atr"] = A["d"] * (c.to_numpy() - c.to_numpy()[start]) / atr.to_numpy()
    bw = c.rolling(20).std() * 4 / c.rolling(20).mean()
    p["bb_width_pct"] = bw.rolling(500, min_periods=100).rank(pct=True)
    p["parkinson_24"] = np.sqrt((np.log(h / low) ** 2).rolling(24).mean() / (4 * math.log(2)))
    rng_ = (h - low).replace(0, np.nan)
    p["clv"] = ((c - low) - (h - c)) / rng_
    hr = ts.dt.hour
    med = v.groupby(hr).transform(lambda s: s.shift(1).rolling(20, min_periods=3).median())
    p["rel_volume"] = v / med
    p["cmf_24"] = (p.clv * v).rolling(24).sum() / v.rolling(24).sum()
    p["effort_result"] = p.rel_volume / ((c.diff().abs() / atr) + 0.05)
    p["wick_up"] = (h - np.maximum(o, c)) / rng_
    p["wick_dn"] = (np.minimum(o, c) - low) / rng_
    p["range_atr"] = (h - low) / atr
    p["session"] = pd.cut(hr, [-1, 6, 11, 15, 20, 23], labels=["asia", "london", "overlap", "ny", "late"])
    p["dow"] = ts.dt.dayofweek
    # calendar: importance-1 events for this symbol's currencies
    ccy = set(CCY[A["sym"]])
    evt = sorted(pd.to_datetime(e["date"], utc=True).timestamp() for e in cal
                 if (e.get("importance") or 0) >= 1 and e.get("currency") in ccy)
    evt = np.array(evt)
    tt = df.t.to_numpy(float)
    if len(evt):
        i = np.searchsorted(evt, tt, side="left")
        nxt = np.where(i < len(evt), evt[np.minimum(i, len(evt) - 1)], np.nan)
        prv = np.where(i > 0, evt[np.maximum(i - 1, 0)], np.nan)
        p["h_to_event"] = (nxt - tt) / 3600
        p["h_since_event"] = (tt - prv) / 3600
    p["regime_age_h"] = R["age_h"]
    p["left_cens"] = R["left_cens"]
    p["cell"] = [CELLS[k] if k >= 0 else None for k in A["cell"]]
    return p


# ─── 6. HTML artefact ─────────────────────────────────────────────────────


def write_html(path: Path, pts: pd.DataFrame, steps: list[dict], traj: pd.DataFrame,
               compass: pd.DataFrame, verdicts: list[tuple], meta: dict) -> None:
    W, Hh, pad = 560, 420, 34
    x_lo, x_hi = np.nanpercentile(pts.x, [2, 98])
    y_lo, y_hi = np.nanpercentile(pts.y, [2, 98])
    xm = max(abs(x_lo), abs(x_hi))
    ym = max(abs(y_lo), abs(y_hi))

    def sx(x):
        return pad + (np.clip(x, -xm, xm) + xm) / (2 * xm) * (W - 2 * pad)

    def sy(y):
        return Hh - pad - (np.clip(y, -ym, ym) + ym) / (2 * ym) * (Hh - 2 * pad)

    parts = [f'<svg width="{W}" height="{Hh}" viewBox="0 0 {W} {Hh}">',
             '<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" '
             'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--muted-foreground)"/></marker></defs>',
             f'<rect x="{sx(0)}" y="{pad}" width="{W - pad - sx(0)}" height="{sy(0) - pad}" fill="var(--accent)" opacity="0.07"/>',
             f'<rect x="{pad}" y="{sy(0)}" width="{sx(0) - pad}" height="{Hh - pad - sy(0)}" fill="var(--muted-foreground)" opacity="0.06"/>',
             f'<line x1="{pad}" y1="{sy(0)}" x2="{W - pad}" y2="{sy(0)}" stroke="var(--muted-foreground)"/>',
             f'<line x1="{sx(0)}" y1="{pad}" x2="{sx(0)}" y2="{Hh - pad}" stroke="var(--muted-foreground)"/>',
             f'<text x="{W - pad - 70}" y="{pad + 12}" font-weight="600">edge_rising</text>',
             f'<text x="{W - pad - 84}" y="{Hh - pad - 6}" font-weight="600">edge_decaying</text>',
             f'<text x="{pad + 4}" y="{Hh - pad - 6}" font-weight="600">no_edge</text>',
             f'<text x="{pad + 4}" y="{pad + 12}" font-weight="600">edge_recovering</text>',
             f'<text x="{W / 2 - 110}" y="{Hh - 8}" class="m">X = 14d edge level, m₂₄ (ATR units) →</text>',
             f'<text x="10" y="{Hh / 2 + 60}" class="m" transform="rotate(-90 10 {Hh / 2 + 60})">Y = X₇ − X₁₄ (momentum) →</text>']
    # flow field: grid bins
    dx = np.concatenate([s["dx"] for s in steps])
    dy = np.concatenate([s["dy"] for s in steps])
    xx = np.concatenate([s["x"] for s in steps])
    yy = np.concatenate([s["y"] for s in steps])
    nb = 7
    bx = np.clip(((xx + xm) / (2 * xm) * nb).astype(int), 0, nb - 1)
    by = np.clip(((yy + ym) / (2 * ym) * nb).astype(int), 0, nb - 1)
    cellw = (W - 2 * pad) / nb
    vecs = []
    for i in range(nb):
        for j in range(nb):
            m = (bx == i) & (by == j)
            if m.sum() >= 40:
                vecs.append((i, j, dx[m].mean(), dy[m].mean(), int(m.sum())))
    if vecs:
        vmax = max(math.hypot(v[2] / xm, v[3] / ym) for v in vecs) or 1
        for i, j, vx, vy, _ in vecs:
            cx = pad + (i + 0.5) * cellw
            cy = Hh - pad - (j + 0.5) * (Hh - 2 * pad) / nb
            scale = 0.8 * cellw / vmax
            ex, ey = cx + vx / xm * scale, cy - vy / ym * scale
            parts.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="var(--muted-foreground)" '
                         'stroke-width="1.2" opacity="0.7" marker-end="url(#a)"/>')
    # mean trajectories by age band per symbol
    for k, (sym, g) in enumerate(traj.groupby("symbol")):
        g = g.sort_values("band")
        d_attr = " ".join(f"{'M' if i == 0 else 'L'} {sx(r.x):.1f} {sy(r.y):.1f}" for i, r in enumerate(g.itertuples()))
        parts.append(f'<path d="{d_attr}" fill="none" stroke="var(--accent)" stroke-width="2" opacity="{0.45 + 0.1 * k:.2f}"/>')
        for r in g.itertuples():
            op = 1.0 - 0.17 * r.band
            parts.append(f'<circle cx="{sx(r.x):.1f}" cy="{sy(r.y):.1f}" r="4" fill="var(--accent)" opacity="{op:.2f}"/>')
        last = g.iloc[-1]
        parts.append(f'<text x="{sx(last.x) + 6:.1f}" y="{sy(last.y) + 4:.1f}" class="m">{sym}</text>')
    parts.append("</svg>")
    svg = "\n".join(parts)
    comp_rows = "".join(
        f"<tr><td>{r.band_lab}</td><td>{int(r.n)}</td><td>{r.vol_up:.0%}</td><td>{r.vol_down:.0%}</td>"
        f"<td>{r.dir_flip:.0%}</td><td>{r.both:.0%}</td></tr>" for r in compass.itertuples())
    ver_rows = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td><b>{d}</b></td></tr>" for a, b, c, d in verdicts)
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
.w{{display:inline-block}} h3{{margin:0 0 2px 0;font-size:15px;color:var(--foreground)}}
.sub,.m{{font-size:11px;color:var(--muted-foreground);fill:var(--muted-foreground)}}
svg text{{fill:var(--foreground);font-size:11px}} table{{border-collapse:collapse;font-size:12px;margin-top:6px}}
td,th{{border-bottom:1px solid var(--border);padding:2px 8px;text-align:right;color:var(--foreground)}}
th{{color:var(--muted-foreground);font-weight:500}} td:first-child,th:first-child{{text-align:left}}
.row{{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}}</style></head><body><div class="w">
<h3>Kairos v3 P1 — edge plane from data (TDVA reconstruction, geometry-free)</h3>
<div class="sub">{meta['desc']}</div>
<div class="row"><div>{svg}</div><div>
<div class="sub">Frozen tests (§5.0), Holm over V1–V4</div>
<table><tr><th>Test</th><th>Statistic</th><th>p (Holm)</th><th>Result</th></tr>{ver_rows}</table>
<div class="sub" style="margin-top:10px">Exit compass: how completed runs end, by run length</div>
<table><tr><th>run length</th><th>n</th><th>vol ↑</th><th>vol ↓</th><th>dir flip</th><th>vol+dir</th></tr>{comp_rows}</table>
</div></div>
<div class="sub" style="margin-top:6px">Grey arrows = mean step of the symbol-level point per plane cell (flow field, ≥40 steps).
Accent paths = each symbol's mean position by regime age band (0–6h … ≥48h, fading with age).</div>
</div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ─── main ─────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(SYMS))
    ap.add_argument("--n-null", type=int, default=1000)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--refresh-volume", action="store_true")
    ap.add_argument("--n-confirm", type=int, default=0, help="0 = auto (closest to live flip rate)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--html", default=str(ROOT / "docs/scopes/assets/20260924_kairos_edge_plane_discovery.html"))
    a = ap.parse_args()
    SFX = f"_{a.tag}" if a.tag else ""
    syms = a.symbols.split(",")
    t0 = time.time()
    KCACHE.mkdir(parents=True, exist_ok=True)
    cal_f = KCACHE / "econ_calendar.json"
    cal = json.loads(cal_f.read_text()) if cal_f.exists() else []

    # 1. data + debounce calibration
    data, raw_labels = {}, {}
    for s in syms:
        df, labs = load_symbol(s, a.refresh_volume)
        raw = [None] * len(df)
        for t, lbl, _conf, _conv in labs:
            raw[int(t)] = lbl
        data[s], raw_labels[s] = df, raw
        print(f"{s}: {len(df)} bars {pd.to_datetime(df.t.iloc[0], unit='s'):%Y-%m-%d}..{pd.to_datetime(df.t.iloc[-1], unit='s'):%Y-%m-%d} "
              f"· labelled {sum(x is not None for x in raw)} · volume coverage {df.v.notna().mean():.1%}")
    print(f"\n== 1. debounce calibration (live post-debounce hourly cell flip rate = {LIVE_FLIP}) ==")
    rates = {}
    for nconf in range(1, 7):
        f = t_ = 0
        for s in syms:
            cells = [cell_of(x) for x in debounce(raw_labels[s], nconf)]
            ff, tt = flip_rate(cells, data[s].t.to_numpy())
            f, t_ = f + ff, t_ + tt
        rates[nconf] = f / max(t_, 1)
        print(f"  n_confirm={nconf}: flip/h {rates[nconf]:.3f}")
    NCONF = a.n_confirm or min(rates, key=lambda k: abs(rates[k] - LIVE_FLIP))
    print(f"  → n_confirm = {NCONF} (flip/h {rates[NCONF]:.3f} vs live {LIVE_FLIP})"
          + (" [override]" if a.n_confirm else ""))

    A, R = {}, {}
    for s in syms:
        cells = [cell_of(x) for x in debounce(raw_labels[s], NCONF)]
        A[s] = build_arrays(s, data[s], cells)
        R[s] = runs_of(A[s]["cell"], A[s]["ts"], A[s]["gap_before"])

    # 2. edge curves (V1) and age (V2)
    print("\n== 2. edge curves: m_h = d·(C[t+h]−C[t])/ATR, all signals (pooled, symbol-day clusters) ==")
    rows = []
    for s in syms:
        a_, r_ = A[s], R[s]
        for i in range(a_["n"]):
            pass
        base = pd.DataFrame({"symbol": s, "t": a_["ts"], "day": a_["day"], "cell": a_["cell"], "d": a_["d"],
                             "age_h": r_["age_h"], "left_cens": r_["left_cens"]})
        for h in H:
            base[f"m{h}"] = a_["d"] * a_["move"][h]
        rows.append(base[base.d != 0])
    sig = pd.concat(rows, ignore_index=True)
    sig["clu"] = sig.symbol + "|" + sig.day.astype(str)
    curve = []
    for h in H:
        v = sig[f"m{h}"].to_numpy()
        ok = np.isfinite(v)
        est, lo, hi, pneg = cluster_boot_mean(v[ok], sig.clu.to_numpy()[ok], a.n_boot)
        curve.append((h, est, lo, hi, pneg, float((v[ok] > 0).mean())))
        print(f"  h={h:>2}: mean {est:+.4f} ATR [{lo:+.4f},{hi:+.4f}] · hit rate {(v[ok] > 0).mean():.3f} · n {ok.sum()}")
    per_sym = sig.groupby("symbol")[[f"m{h}" for h in H]].mean()
    print("  per symbol:\n    " + per_sym.round(4).to_string().replace("\n", "\n    "))
    cell_tab = sig.assign(cell=sig.cell.map(lambda k: CELLS[k])).groupby("cell")[["m4", "m24"]].agg(["mean", "count"])
    print("  pooled by regime cell (m4, m24):\n    " + cell_tab.round(4).to_string().replace("\n", "\n    "))

    # null: circular shift of labels vs prices
    print(f"\n== null: circular label shift (≥14d offset), {a.n_null} draws ==")
    obs_m = {h: np.nanmean(sig[f"m{h}"]) for h in (4, 24)}
    null_m = {4: [], 24: []}
    null_speed = []
    obs_planes = {}
    steps_obs = []
    for s in syms:
        a_ = A[s]
        X, Y, dfn, st = plane(a_["cell"], a_["d"] * a_["move"][HREF], a_["lo14"], a_["lo7"])
        obs_planes[s] = (X, Y, dfn, st)
        steps_obs.append(flow_steps(a_["ts"], X, Y, dfn, st, R[s]["rid"]))
    speed_obs = region_speed(steps_obs)
    tn = time.time()
    for _ in range(a.n_null):
        sums = {4: [0.0, 0], 24: [0.0, 0]}
        st_list = []
        for s in syms:
            a_ = A[s]
            n = a_["n"]
            o = int(RNG.integers(336, n - 336))
            cell_r = np.roll(a_["cell"], o)
            d_r = np.roll(a_["d"], o)
            for h in (4, 24):
                m = d_r * a_["move"][h]
                ok = np.isfinite(m) & (d_r != 0)
                sums[h][0] += m[ok].sum()
                sums[h][1] += ok.sum()
            X, Y, dfn, st = plane(cell_r, d_r * a_["move"][HREF], a_["lo14"], a_["lo7"])
            st_list.append(flow_steps(a_["ts"], X, Y, dfn, st))
        for h in (4, 24):
            null_m[h].append(sums[h][0] / sums[h][1])
        null_speed.append(region_speed(st_list))
    null_speed = np.array(null_speed)
    print(f"  null done in {time.time() - tn:.0f}s")

    # V1
    p_null = {h: float((np.array(null_m[h]) >= obs_m[h]).mean()) for h in (4, 24)}
    p_ci = {h: next(c[4] for c in curve if c[0] == h) for h in (4, 24)}
    for h in (4, 24):
        lo, hi = np.percentile(null_m[h], [2.5, 97.5])
        print(f"  V1 h={h}: observed {obs_m[h]:+.4f} · null 95% [{lo:+.4f},{hi:+.4f}] · p_null {p_null[h]:.3f} · "
              f"p_CI(one-sided) {p_ci[h]:.3f}")
    pV1 = min(1.0, 2 * min(max(p_ci[h], p_null[h]) for h in (4, 24)))
    v1_pass = any((next(c[2] for c in curve if c[0] == h) > 0) and p_null[h] < 0.05 for h in (4, 24))

    # V2 — edge decay with regime age (m24 demeaned within symbol; left-censored excluded)
    print("\n== 3. V2 edge vs regime age (m24 demeaned within symbol; left-censored runs excluded) ==")
    s2 = sig[np.isfinite(sig.m24) & ~sig.left_cens].copy()
    s2["y"] = s2.m24 - s2.groupby("symbol").m24.transform("mean")
    s2["x"] = np.log1p(s2.age_h)
    v2 = cluster_boot_slope(s2.x.to_numpy(), s2.y.to_numpy(), s2.clu.to_numpy(), a.n_boot)
    per_sym_slope = {s: cluster_boot_slope(g.x.to_numpy(), g.y.to_numpy(), g.clu.to_numpy(), 500)[0]
                     for s, g in s2.groupby("symbol")}
    n_neg = sum(v < 0 for v in per_sym_slope.values())
    print(f"  pooled slope per log(1+age): {v2[0]:+.4f} [{v2[1]:+.4f},{v2[2]:+.4f}] · p(slope≥0) {v2[3]:.3f}")
    print("  per-symbol slopes: " + " · ".join(f"{k} {v:+.4f}" for k, v in per_sym_slope.items()) + f" → {n_neg}/5 negative")
    s2["band"] = band(s2.age_h.to_numpy())
    age_curve = s2.groupby("band")[[f"m{h}" for h in H]].mean()
    age_curve.index = [BAND_LAB[i] for i in age_curve.index]
    age_n = s2.groupby("band").size()
    print("  edge curves by age band (mean m_h, ATR units; rows = age at signal, cols = horizon):\n    "
          + age_curve.round(4).assign(n=age_n.to_numpy()).to_string().replace("\n", "\n    "))
    pV2 = v2[3]
    v2_pass = v2[2] < 0 and n_neg >= 3

    # V3 — flow structure
    print("\n== 4. V3 flow field (symbol-level plane point, regional mean velocity vs null) ==")
    p3 = {}
    for r in range(4):
        nv = null_speed[:, r]
        nv = nv[np.isfinite(nv)]
        p3[STATES[r]] = float((nv >= speed_obs[r]).mean()) if len(nv) and np.isfinite(speed_obs[r]) else np.nan
        print(f"  {STATES[r]:16s} |v̄| {speed_obs[r]:.4f} · null median {np.nanmedian(null_speed[:, r]):.4f} · p {p3[STATES[r]]:.3f}")
    h3 = holm(p3)
    v3_pass = sum(v < 0.05 for v in h3.values()) >= 2
    pV3 = sorted(h3.values())[1] if len(h3) >= 2 else 1.0
    wr = np.concatenate([s["within_run"] for s in steps_obs])
    print(f"  steps: {len(wr)} · within-run {wr.mean():.1%} · across regime exits {1 - wr.mean():.1%}")
    st_all = np.concatenate([s["region"] for s in steps_obs])
    print("  plane occupancy: " + ", ".join(f"{STATES[k]} {(st_all == k).mean():.0%}" for k in range(4)))

    # V4 — hazard (shifted by n_confirm − 1)
    print(f"\n== 5. V4 regime-exit hazard (clock shifted by n_confirm−1 = {NCONF - 1}) ==")
    shift = NCONF - 1
    tabs = []
    for s in syms:
        tb = R[s]["table"].copy()
        tb["symbol"] = s
        tabs.append(tb[~tb.left_cens])
    runs = pd.concat(tabs, ignore_index=True)
    L = runs.len_bars.to_numpy(int)
    ev = runs.obs_end.to_numpy(bool)
    kmax = 240
    at, evk = hazard_counts(L, ev, np.ones(len(L)), shift, kmax)
    xk = np.log1p(np.arange(kmax))
    b = logistic_agg(xk, evk, at)
    boots = []
    for _ in range(a.n_boot):
        w = RNG.multinomial(len(L), np.full(len(L), 1 / len(L))).astype(float)
        at_b, ev_b = hazard_counts(L, ev, w, shift, kmax)
        boots.append(logistic_agg(xk, ev_b, at_b)[1])
    lo4, hi4 = np.percentile(boots, [2.5, 97.5])
    print(f"  runs (not left-censored): {len(L)} · observed ends {ev.sum()} · median length {np.median(L):.0f} bars")
    print(f"  logistic hazard slope on log(1+age'): {b[1]:+.4f} [{lo4:+.4f},{hi4:+.4f}] "
          f"({'rising' if b[1] > 0 else 'falling'} hazard)")
    emp = pd.DataFrame({"k": np.arange(kmax), "at": at, "ev": evk})
    emp["band"] = band(emp.k.to_numpy() + shift)
    hz = emp.groupby("band")[["ev", "at"]].sum().rename(columns={"at": "at_risk"})
    hz["hazard_per_h"] = hz["ev"] / hz["at_risk"]
    hz.index = [BAND_LAB[i] for i in hz.index]
    print("  empirical hazard by age band:\n    " + hz.round(4).to_string().replace("\n", "\n    "))
    ks = {}
    for s in syms:
        rs = runs[(runs.symbol == s) & runs.obs_end]
        d0, pks = ks_geometric(rs.len_bars.to_numpy(int) - shift)
        ks[s] = (d0, pks)
        print(f"  KS vs shifted geometric {s}: D {d0:.3f} · p {pks:.3f} · n {len(rs)}")
    n_rej = sum(1 for v in ks.values() if np.isfinite(v[1]) and v[1] < 0.05)
    p_slope = float((np.array(boots) <= 0).mean())
    v4_pass = lo4 > 0 and n_rej >= 3
    pV4 = p_slope if n_rej >= 3 else 1.0

    # exit compass
    comp = runs[runs.obs_end & (runs.next_cell >= 0)].copy()
    vol_i = comp.cell // 2
    vol_j = comp.next_cell // 2
    dir_i, dir_j = comp.cell % 2, comp.next_cell % 2
    comp["kind"] = np.select([(vol_i != vol_j) & (dir_i != dir_j), dir_i != dir_j, vol_j > vol_i, vol_j < vol_i],
                             ["both", "dir_flip", "vol_up", "vol_down"], "same")
    comp["band"] = band(comp.len_h.to_numpy())
    compass = comp.groupby("band").kind.value_counts(normalize=True).unstack(fill_value=0)
    for k in ("vol_up", "vol_down", "dir_flip", "both"):
        if k not in compass:
            compass[k] = 0.0
    compass["n"] = comp.groupby("band").size()
    compass = compass.reset_index()
    compass["band_lab"] = [BAND_LAB[i] for i in compass.band]
    print("\n== exit compass (completed runs, by run length) ==\n    "
          + compass[["band_lab", "n", "vol_up", "vol_down", "dir_flip", "both"]].round(3).to_string(index=False).replace("\n", "\n    "))

    # Holm + verdicts
    fam = holm({"V1": pV1, "V2": pV2, "V3": pV3, "V4": pV4})
    verdicts = [
        ("V1 edge exists", f"m₄ {obs_m[4]:+.3f} / m₂₄ {obs_m[24]:+.3f} ATR", f"{fam['V1']:.3f}",
         "PASS" if v1_pass and fam["V1"] < 0.05 else "fail"),
        ("V2 decays with age", f"slope {v2[0]:+.3f}, {n_neg}/5 neg", f"{fam['V2']:.3f}",
         "PASS" if v2_pass and fam["V2"] < 0.05 else "fail"),
        ("V3 flow has structure", f"{sum(v < 0.05 for v in h3.values())}/4 regions", f"{fam['V3']:.3f}",
         "PASS" if v3_pass and fam["V3"] < 0.05 else "fail"),
        ("V4 regimes age", f"hazard slope {b[1]:+.3f}, KS {n_rej}/5", f"{fam['V4']:.3f}",
         "PASS" if v4_pass and fam["V4"] < 0.05 else "fail"),
    ]
    print("\n== P1 verdicts (Holm over V1–V4) ==")
    for v in verdicts:
        print(f"  {v[0]:24s} {v[1]:34s} Holm p {v[2]:>6s} → {v[3]}")

    # 6. factor panel (descriptive)
    print("\n== 6. factor panel (§3.8, descriptive — V7 is P2) ==")
    fp_all = []
    for s in syms:
        fp = factor_panel(A[s], R[s], cal)
        fp["symbol"] = s
        fp.to_pickle(KCACHE / f"p1_factor_panel_{s}{SFX}.pkl")
        fp_all.append(fp)
    fpa = pd.concat(fp_all)
    num = [c for c in fpa.columns if c not in ("t", "session", "cell", "symbol", "left_cens", "dow")]
    cov = fpa[num].notna().mean()
    corr_age = fpa[~fpa.left_cens][num].corrwith(np.log1p(fpa[~fpa.left_cens].regime_age_h), method="spearman")
    print("  coverage / Spearman ρ with log(1+regime age) [collinearity preview]:")
    for c_ in num:
        if c_ == "regime_age_h":
            continue
        print(f"    {c_:16s} cov {cov[c_]:.0%} · ρ(age) {corr_age.get(c_, np.nan):+.2f}")

    # artefacts
    pts = pd.concat([pd.DataFrame({"x": obs_planes[s][0][obs_planes[s][2]], "y": obs_planes[s][1][obs_planes[s][2]]})
                     for s in syms])
    traj_rows = []
    for s in syms:
        X, Y, dfn, _ = obs_planes[s]
        m = dfn & ~R[s]["left_cens"]
        bd = band(R[s]["age_h"][m])
        for k in np.unique(bd):
            traj_rows.append({"symbol": s, "band": int(k), "x": float(X[m][bd == k].mean()), "y": float(Y[m][bd == k].mean()),
                              "n": int((bd == k).sum())})
    traj = pd.DataFrame(traj_rows)
    matrix = []
    for s in syms:
        g = s2[s2.symbol == s].copy()
        g["cell"] = g.cell.map(lambda k: CELLS[k])
        g["band_lab"] = [BAND_LAB[i] for i in g.band]
        t_ = g.groupby(["cell", "band_lab"]).agg(n=("m24", "size"), m24=("m24", "mean"),
                                                 hit24=("m24", lambda v: (v > 0).mean())).reset_index()
        t_["symbol"] = s
        matrix.append(t_)
    pd.concat(matrix).to_csv(KCACHE / f"p1_matrix_cell_age{SFX}.csv", index=False)
    summary = {
        "n_confirm": NCONF, "flip_rates": rates, "curve": curve,
        "V1": {"obs": obs_m, "p_null": p_null, "p_ci": p_ci, "pass": bool(v1_pass)},
        "V2": {"slope": v2, "per_symbol": per_sym_slope, "pass": bool(v2_pass)},
        "V3": {"speed_obs": speed_obs.tolist(), "p": p3, "holm": h3, "pass": bool(v3_pass)},
        "V4": {"slope": float(b[1]), "ci": [float(lo4), float(hi4)], "ks": ks, "pass": bool(v4_pass)},
        "holm_family": fam, "verdicts": verdicts, "runtime_s": time.time() - t0,
    }
    (KCACHE / f"p1_summary{SFX}.json").write_text(json.dumps(summary, indent=1, default=float))
    desc = (f"{len(syms)} symbols · {len(sig):,} signals · n_confirm {NCONF} · "
            f"{a.n_null} circular-shift draws · generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    write_html(Path(a.html), pts, steps_obs, traj, compass, verdicts, {"desc": desc})
    print(f"\nartefacts: {KCACHE}/p1_summary.json, p1_matrix_cell_age.csv, p1_factor_panel_*.pkl, {a.html}")
    print(f"runtime {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
