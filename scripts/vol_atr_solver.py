"""
Vol-conditioned ATR solver — regime cells for BTCUSD / XAUUSD (and friends).

Hypothesis (2026-09-30): BTC wins historically cluster in aggressive moves;
a pooled 10d walk averages those trends into a coin-flip tight band (0.95/0.15).
Split the replay by vol regime and re-solve TP/SL inside each cell.

Two views:
  1. ENTRY-time features (live-gateable): rv_ratio, range_atr, disp_atr
     computed strictly on bars before/at entry.
  2. POST-entry path aggression (hypothesis only — never a live gate):
     max |ATR path| over the 48-bar window.

Same bars as the platform walk (1h closes, 48-bar, first touch, TP-first).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env.local")

from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    fetch_active_symbols,
)
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS, walk
import httpx

OUT = ROOT / "worklog" / "kairos" / "reports" / "20260930_vol_regime_atr.md"
COST = 0.1
WR_LO, WR_HI = 0.40, 0.60
EXP_MAX = 0.20
MIN_PATHS = 80
MIN_DEC = 40
TARGETS = ["BTCUSD", "XAUUSD", "XAGUSD"]


async def load_signals(syms, since, until):
    rows, off = [], 0
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            r = await c.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={
                    "select": "symbol,signal,ts,entry_price,atr",
                    "source": "eq.sweep-light",
                    "signal": "in.(buy,sell)",
                    "symbol": f"in.({','.join(syms)})",
                    "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                    "order": "ts.asc",
                    "limit": "1000",
                    "offset": str(off),
                },
            )
            b = r.json() or []
            rows += b
            off += len(b)
            if len(b) < 1000:
                break
    out, seen = [], set()
    for r in rows:
        try:
            atr = float(r.get("atr") or 0)
            entry = float(r["entry_price"])
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, KeyError):
            continue
        if atr <= 0:
            continue
        k = (r["symbol"], r["signal"], int(ts // 3600))
        if k in seen:
            continue
        seen.add(k)
        out.append(
            {"symbol": r["symbol"], "signal": r["signal"], "entry_price": entry, "atr": atr, "_ts": ts}
        )
    return out


def build_paths_meta(signals, bars):
    """Mirror build_paths + entry-time vol features + post-path aggression.

    Returns paths (n, 48), meta list (same order). Entry-time features use
    only bars at index < j (j = bar in progress at entry).
    """
    tcache = {s: np.array([float(b["t"]) for b in rows if b.get("t") is not None], dtype="float64")
              for s, rows in bars.items()}
    ccache, hcache, lcache = {}, {}, {}
    for s, rows in bars.items():
        cc, hh, ll = [], [], []
        for b in rows:
            if b.get("t") is None or b.get("c") is None:
                continue
            cc.append(float(b["c"]))
            hh.append(float(b.get("h") or b["c"]))
            ll.append(float(b.get("l") or b["c"]))
        ccache[s] = np.asarray(cc, dtype="float64")
        hcache[s] = np.asarray(hh, dtype="float64")
        lcache[s] = np.asarray(ll, dtype="float64")
        tcache[s] = np.array(
            [float(b["t"]) for b in rows if b.get("t") is not None and b.get("c") is not None],
            dtype="float64",
        )

    paths_out, meta_out = [], []
    for r in sorted(signals, key=lambda x: x["_ts"]):
        s = r["symbol"]
        t = tcache.get(s)
        if t is None or not len(t):
            continue
        j = int(np.searchsorted(t, r["_ts"], side="right")) - 1
        if j < 0 or j + EXPIRY_BARS > len(t):
            continue
        c = ccache[s]
        d = 1.0 if r["signal"] == "buy" else -1.0
        path = (c[j:j + EXPIRY_BARS] - r["entry_price"]) * d / r["atr"]
        atr = float(r["atr"])
        # entry-time (strictly past)
        rv24 = rv168 = np.nan
        if j >= 24:
            rets = np.diff(np.log(c[j - 24:j + 1]))
            rv24 = float(rets.std()) if len(rets) else np.nan
        if j >= 168:
            rets_l = np.diff(np.log(c[j - 168:j + 1]))
            rv168 = float(rets_l.std()) if len(rets_l) else np.nan
        vol_ratio = float(rv24 / rv168) if rv168 and rv168 > 0 and np.isfinite(rv24) else np.nan
        range_atr = np.nan
        if j >= 24:
            hi = float(hcache[s][j - 24:j + 1].max())
            lo = float(lcache[s][j - 24:j + 1].min())
            range_atr = (hi - lo) / atr if atr > 0 else np.nan
        disp_atr = float(abs(c[j] - c[j - 24]) / atr) if j >= 24 and atr > 0 else np.nan
        # post-entry aggression (analysis only)
        post_absmax = float(np.max(np.abs(path)))
        post_range = float(path.max() - path.min())
        meta_out.append({
            "symbol": s,
            "ts": r["_ts"],
            "vol_ratio": vol_ratio,
            "range_atr": range_atr,
            "disp_atr": disp_atr,
            "post_absmax": post_absmax,
            "post_range": post_range,
            "j": j,
        })
        paths_out.append(path)
    if not paths_out:
        return np.empty((0, EXPIRY_BARS)), []
    return np.vstack(paths_out), meta_out


def eval_cell(paths, tp, sl, cost=COST):
    if paths is None or len(paths) == 0:
        return None
    pnl, exp = walk(paths, float(tp), float(sl))
    e = float(exp.mean())
    tp_hit = (~exp) & (pnl > 0)
    sl_hit = (~exp) & (pnl <= 0)
    n_tp, n_sl = int(tp_hit.sum()), int(sl_hit.sum())
    dec = n_tp + n_sl
    if dec < MIN_DEC:
        return None
    wr = n_tp / dec
    ev = float((pnl - cost).mean())
    mid = len(paths) // 2
    net = pnl - cost
    ev_h1 = float(net[:mid].mean()) if mid else np.nan
    ev_h2 = float(net[mid:].mean()) if mid else np.nan
    return {
        "tp": float(tp), "sl": float(sl), "wr": wr, "exp": e, "ev": ev,
        "ev_h1": ev_h1, "ev_h2": ev_h2, "n": int(paths.shape[0]), "dec": dec,
    }


def _scan(paths, tps, sls):
    def ok(sc):
        if not sc or sc["exp"] > EXP_MAX or not (WR_LO <= sc["wr"] <= WR_HI):
            return False
        return sc["ev_h1"] > 0 and sc["ev_h2"] > 0

    best = None
    for tp in tps:
        for sl in sls:
            if tp / sl < 1.0:
                continue
            sc = eval_cell(paths, tp, sl)
            if ok(sc) and (best is None or sc["ev"] > best["ev"]):
                best = sc
    return best


def solve(paths, tp_grid=None, sl_grid=None):
    """Coarse 0.25 → 0.05 → 0.01. Both halves must be EV>0."""
    if paths is None or len(paths) < MIN_PATHS:
        return None
    if tp_grid is not None and sl_grid is not None:
        return _scan(paths, tp_grid, sl_grid)
    best = _scan(
        paths,
        np.round(np.arange(0.25, 16.01, 0.25), 2),
        np.round(np.arange(0.25, 10.01, 0.25), 2),
    )
    if best is None:
        return None
    best = _scan(
        paths,
        np.round(np.arange(max(0.20, best["tp"] - 0.30), best["tp"] + 0.30, 0.05), 2),
        np.round(np.arange(max(0.20, best["sl"] - 0.30), best["sl"] + 0.30, 0.05), 2),
    ) or best
    best = _scan(
        paths,
        np.round(np.arange(max(0.15, best["tp"] - 0.08), best["tp"] + 0.08, 0.01), 2),
        np.round(np.arange(max(0.15, best["sl"] - 0.08), best["sl"] + 0.08, 0.01), 2),
    ) or best
    return best


def tercile_masks(values: np.ndarray) -> dict[str, np.ndarray]:
    v = np.asarray(values, dtype="float64")
    finite = np.isfinite(v)
    if finite.sum() < 30:
        return {}
    lo, hi = np.nanpercentile(v[finite], [33.333, 66.667])
    return {
        "low": finite & (v <= lo),
        "mid": finite & (v > lo) & (v <= hi),
        "high": finite & (v > hi),
    }


def oos_split(paths, meta_idx: np.ndarray, tp, sl):
    """Fit-metric: evaluate fixed (tp,sl) on first 70% / last 30% of a mask."""
    idx = np.where(meta_idx)[0]
    if len(idx) < MIN_PATHS:
        return None
    n = len(idx)
    cut = int(n * 0.7)
    a, b = paths[idx[:cut]], paths[idx[cut:]]
    ra, rb = eval_cell(a, tp, sl), eval_cell(b, tp, sl)
    return {"n_is": cut, "n_oos": n - cut, "is": ra, "oos": rb}


def fmt_sc(sc):
    if not sc:
        return "—"
    return (
        f"**{sc['tp']:.2f}** / **{sc['sl']:.2f}** · RR {sc['tp']/sc['sl']:.2f} · "
        f"wr {sc['wr']:.3f} · EV {sc['ev']:+.3f} (h1 {sc['ev_h1']:+.3f} / h2 {sc['ev_h2']:+.3f}) · "
        f"e {sc['exp']:.3f} · n {sc['n']}"
    )


async def run_symbol(peers_map, sym, since, until, lines):
    p = peers_map.get(sym)
    if not p or not p.get("tradingview_symbol"):
        print(f"skip {sym} — no tv symbol", flush=True)
        return
    sig = await load_signals([sym], since, until)
    bars = {}
    await fetch_tradingview_batch(
        [{"symbol": sym, "tradingview_symbol": p["tradingview_symbol"]}],
        hours=24 * 30, interval="1h", ohlcv_out=bars,
    )
    bars = {s: [b for b in rows if b.get("t") and b.get("c")] for s, rows in bars.items()}
    paths, meta = build_paths_meta(sig, bars)
    if paths.shape[0] < MIN_PATHS:
        print(f"skip {sym} paths={paths.shape[0]}", flush=True)
        return

    print(f"\n=== {sym}  n={paths.shape[0]} ===", flush=True)
    lines.append(f"\n## {sym}\n")
    lines.append(f"paths n={paths.shape[0]} · window {since.date()} → {until.date()}\n")

    base = solve(paths)
    print(f"  pooled: {fmt_sc(base)}", flush=True)
    lines.append(f"- **pooled**: {fmt_sc(base)}\n")

    # entry-time feature terciles
    for feat in ("vol_ratio", "range_atr", "disp_atr"):
        vals = np.array([m[feat] for m in meta], dtype="float64")
        masks = tercile_masks(vals)
        if not masks:
            continue
        lines.append(f"\n### entry `{feat}` terciles\n")
        lines.append("| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for name in ("low", "mid", "high"):
            m = masks[name]
            sc = solve(paths[m])
            if sc:
                row = (
                    f"| {name} | {sc['tp']:.2f} | {sc['sl']:.2f} | {sc['tp']/sc['sl']:.2f} | "
                    f"{sc['wr']:.3f} | {sc['ev']:+.3f} | {sc['ev_h1']:+.3f} | {sc['ev_h2']:+.3f} | "
                    f"{sc['exp']:.3f} | {sc['n']} |"
                )
            else:
                row = f"| {name} | — | | | | | | | | {int(m.sum())} |"
            print(f"  {feat}.{name}: {row}", flush=True)
            lines.append(row + "\n")
        # live-gate candidate: high-only if high is the +EV cell
        high_sc = solve(paths[masks["high"]]) if "high" in masks else None
        low_sc = solve(paths[masks["low"]]) if "low" in masks else None
        if high_sc and high_sc["ev"] > 0 and (not low_sc or low_sc["ev"] <= 0):
            oos = oos_split(paths, masks["high"], high_sc["tp"], high_sc["sl"])
            if oos:
                lines.append(
                    f"\n**gate candidate** `{feat}` high-only "
                    f"{high_sc['tp']:.2f}/{high_sc['sl']:.2f} — "
                    f"IS EV {oos['is']['ev']:+.3f} wr {oos['is']['wr']:.3f} n={oos['n_is']}; "
                    f"OOS EV {oos['oos']['ev']:+.3f} wr {oos['oos']['wr']:.3f} n={oos['n_oos']}\n"
                    if oos["oos"] else f"\n**gate candidate** `{feat}` high-only {high_sc['tp']:.2f}/{high_sc['sl']:.2f} (OOS too thin)\n"
                )
                print(f"  gate {feat} high OOS: {oos}", flush=True)

    # post-entry aggression (hypothesis)
    for feat in ("post_absmax", "post_range"):
        vals = np.array([m[feat] for m in meta], dtype="float64")
        masks = tercile_masks(vals)
        if not masks:
            continue
        lines.append(f"\n### post `{feat}` terciles (hypothesis only — not a live gate)\n")
        lines.append("| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for name in ("low", "mid", "high"):
            m = masks[name]
            sc = solve(paths[m])
            if sc:
                row = (
                    f"| {name} | {sc['tp']:.2f} | {sc['sl']:.2f} | {sc['tp']/sc['sl']:.2f} | "
                    f"{sc['wr']:.3f} | {sc['ev']:+.3f} | {sc['ev_h1']:+.3f} | {sc['ev_h2']:+.3f} | "
                    f"{sc['exp']:.3f} | {sc['n']} |"
                )
            else:
                row = f"| {name} | — | | | | | | | | {int(m.sum())} |"
            print(f"  {feat}.{name}: {row}", flush=True)
            lines.append(row + "\n")
        # correlation of entry features with post aggression
        if feat == "post_absmax":
            lines.append("\nentry→post Spearman:\n")
            for ef in ("vol_ratio", "range_atr", "disp_atr"):
                ev = np.array([m[ef] for m in meta], dtype="float64")
                mask = np.isfinite(ev) & np.isfinite(vals)
                if mask.sum() > 20:
                    # rank corr without scipy
                    r = np.corrcoef(np.argsort(np.argsort(ev[mask])), np.argsort(np.argsort(vals[mask])))[0, 1]
                    lines.append(f"- `{ef}` vs `{feat}`: ρ={r:.3f} (n={int(mask.sum())})\n")
                    print(f"  spearman {ef}~{feat}: {r:.3f}", flush=True)


async def main():
    peers = await fetch_active_symbols()
    peers_map = {p["symbol"]: p for p in peers if p.get("symbol")}
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=45)
    lines = [
        "# Vol-regime ATR solve — BTCUSD / XAUUSD",
        "",
        f"{since.date()} → {until.date()} · wr∈[0.40,0.60] · exp≤0.20 · cost 0.1 · both halves EV>0",
        "",
        "Entry features are live-gateable (past bars only). Post-absmax is the",
        "aggressive-move hypothesis check only.\n",
    ]
    for sym in TARGETS:
        await run_symbol(peers_map, sym, since, until, lines)
        OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
