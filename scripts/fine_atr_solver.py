"""
Fine continuous TP/SL solver — no 0.5 grid, no brick snap.

EV = wr*TP - (1-wr)*SL - cost
Search TP, SL on a 0.05 ATR lattice (true fractional).
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
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
from regime_platform.sweep_orchestrator.tiered_atr import (
    EXPIRY_BARS,
    asset_class_key,
    build_paths,
    walk,
)
import httpx

OUT = ROOT / "worklog" / "kairos" / "reports" / "20260930_fine_atr_solver.md"
COST = 0.1
# 0.05 ATR lattice — fractions, not half/whole
TP_GRID = np.round(np.arange(0.20, 20.01, 0.05), 2)
SL_GRID = np.round(np.arange(0.20, 12.01, 0.05), 2)
WR_LO, WR_HI = 0.40, 0.60
EXP_MAX = 0.20


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


async def main():
    peers = await fetch_active_symbols()
    by = defaultdict(list)
    for p in peers:
        if p.get("tradingview_symbol"):
            by[asset_class_key(p.get("asset_class"))].append(p)
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=14)

    lines = [
        "# Fine continuous ATR solver (0.05 lattice)",
        "",
        "EV = wr·TP − (1−wr)·SL − cost · cost=0.1 ATR",
        "Band: wr ∈ [0.40, 0.60] · exp ≤ 0.20 · maximize EV",
        "",
        "| class | TP | SL | RR | wr | exp | EV | n |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cls in sorted(by):
        mem = by[cls]
        syms = [m["symbol"] for m in mem]
        print("loading", cls, syms, flush=True)
        sig = await load_signals(syms, since, until)
        bars = {}
        await fetch_tradingview_batch(
            [{"symbol": m["symbol"], "tradingview_symbol": m["tradingview_symbol"]} for m in mem],
            hours=24 * 20,
            interval="1h",
            ohlcv_out=bars,
        )
        bars = {s: [b for b in rows if b.get("t") and b.get("c")] for s, rows in bars.items()}
        paths = build_paths(sig, bars)
        print("  paths", paths.shape, flush=True)
        best = None
        # coarse then refine around best
        for tp in TP_GRID:
            for sl in SL_GRID:
                if tp / sl < 1.0:
                    continue
                sc = eval_cell(paths, tp, sl)
                if not sc or sc["exp"] > EXP_MAX:
                    continue
                if not (WR_LO <= sc["wr"] <= WR_HI):
                    continue
                if best is None or sc["ev"] > best["ev"]:
                    best = sc
        if best is not None:
            # 0.01 refine
            for tp in np.arange(max(0.15, best["tp"] - 0.25), best["tp"] + 0.25, 0.01):
                for sl in np.arange(max(0.15, best["sl"] - 0.25), best["sl"] + 0.25, 0.01):
                    if tp / sl < 1.0:
                        continue
                    sc = eval_cell(paths, tp, sl)
                    if not sc or sc["exp"] > EXP_MAX:
                        continue
                    if not (WR_LO <= sc["wr"] <= WR_HI):
                        continue
                    if sc["ev"] > best["ev"]:
                        best = sc
        if not best:
            row = f"| {cls} | — | — | — | — | — | — | — |"
        else:
            row = (
                f"| {cls} | **{best['tp']:.2f}** | **{best['sl']:.2f}** | "
                f"{best['tp']/best['sl']:.3f} | {best['wr']:.3f} | {best['exp']:.3f} | "
                f"**{best['ev']:+.4f}** | {best['n']} |"
            )
        print(row, flush=True)
        lines.append(row)
    lines += [
        "",
        "No brick integers. No 0.5 steps. True fractional ATR on price levels.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
