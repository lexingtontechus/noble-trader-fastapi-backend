"""
Per-symbol fine ATR solver (0.05 → 0.01). Forex shares one grid;
every other symbol gets its own TP/SL.
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
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS, build_paths, walk
import httpx

OUT = ROOT / "worklog" / "kairos" / "reports" / "20260930_per_symbol_atr.md"
COST = 0.1
WR_LO, WR_HI = 0.40, 0.60
EXP_MAX = 0.20
# major FX share fractional-dollar behaviour
FOREX = {"AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY"}


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


def eval_cell(paths, tp, sl, cost=COST):
    pnl, exp = walk(paths, float(tp), float(sl))
    e = float(exp.mean())
    tp_hit = (~exp) & (pnl > 0)
    sl_hit = (~exp) & (pnl <= 0)
    n_tp, n_sl = int(tp_hit.sum()), int(sl_hit.sum())
    dec = n_tp + n_sl
    if dec < 40:
        return None
    wr = n_tp / dec
    ev = float((pnl - cost).mean())
    return {"tp": float(tp), "sl": float(sl), "wr": wr, "exp": e, "ev": ev, "n": int(paths.shape[0])}


def solve(paths):
    best = None
    for tp in np.round(np.arange(0.20, 20.01, 0.05), 2):
        for sl in np.round(np.arange(0.20, 12.01, 0.05), 2):
            if tp / sl < 1.0:
                continue
            sc = eval_cell(paths, tp, sl)
            if not sc or sc["exp"] > EXP_MAX or not (WR_LO <= sc["wr"] <= WR_HI):
                continue
            if best is None or sc["ev"] > best["ev"]:
                best = sc
    if best is None:
        return None
    for tp in np.arange(max(0.15, best["tp"] - 0.25), best["tp"] + 0.25, 0.01):
        for sl in np.arange(max(0.15, best["sl"] - 0.25), best["sl"] + 0.25, 0.01):
            if tp / sl < 1.0:
                continue
            sc = eval_cell(paths, tp, sl)
            if not sc or sc["exp"] > EXP_MAX or not (WR_LO <= sc["wr"] <= WR_HI):
                continue
            if sc["ev"] > best["ev"]:
                best = sc
    return best


async def main():
    peers = await fetch_active_symbols()
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=14)
    lines = [
        "# Per-symbol ATR solve (forex pooled)",
        "",
        f"{since.date()} → {until.date()} · wr∈[0.40,0.60] · exp≤0.20 · cost 0.1 · EV in ATR",
        "",
        "| symbol | TP | SL | RR | wr | exp | EV | n |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    results = {}
    # forex pooled
    fx = [p for p in peers if p["symbol"] in FOREX and p.get("tradingview_symbol")]
    if fx:
        sig = await load_signals([p["symbol"] for p in fx], since, until)
        bars = {}
        await fetch_tradingview_batch(
            [{"symbol": p["symbol"], "tradingview_symbol": p["tradingview_symbol"]} for p in fx],
            hours=24 * 20, interval="1h", ohlcv_out=bars,
        )
        bars = {s: [b for b in rows if b.get("t") and b.get("c")] for s, rows in bars.items()}
        paths = build_paths(sig, bars)
        best = solve(paths)
        results["forex"] = best
        row = (
            f"| **forex** (pooled) | **{best['tp']:.2f}** | **{best['sl']:.2f}** | "
            f"{best['tp']/best['sl']:.2f} | {best['wr']:.3f} | {best['exp']:.3f} | "
            f"**{best['ev']:+.4f}** | {best['n']} |"
        ) if best else "| forex | — | | | | | | |"
        print(row, flush=True)
        lines.append(row)

    for p in peers:
        sym = p["symbol"]
        if sym in FOREX or not p.get("tradingview_symbol"):
            continue
        sig = await load_signals([sym], since, until)
        if len(sig) < 40:
            print(f"skip {sym} n={len(sig)}", flush=True)
            continue
        bars = {}
        await fetch_tradingview_batch(
            [{"symbol": sym, "tradingview_symbol": p["tradingview_symbol"]}],
            hours=24 * 20, interval="1h", ohlcv_out=bars,
        )
        bars = {s: [b for b in rows if b.get("t") and b.get("c")] for s, rows in bars.items()}
        paths = build_paths(sig, bars)
        best = solve(paths)
        results[sym] = best
        if best:
            row = (
                f"| {sym} | **{best['tp']:.2f}** | **{best['sl']:.2f}** | "
                f"{best['tp']/best['sl']:.2f} | {best['wr']:.3f} | {best['exp']:.3f} | "
                f"**{best['ev']:+.4f}** | {best['n']} |"
            )
        else:
            row = f"| {sym} | — | | | | | | {paths.shape[0]} |"
        print(row, flush=True)
        lines.append(row)

    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")
    # yaml snippet
    print("\n# yaml by_symbol")
    for k, v in results.items():
        if v:
            print(f"  {k.lower()}:  # TP {v['tp']:.2f} SL {v['sl']:.2f} EV {v['ev']:+.4f}")
            print(f"    tp_atr: [{v['tp']:.2f}]")
            print(f"    sl_atr: [{v['sl']:.2f}]")


if __name__ == "__main__":
    asyncio.run(main())
