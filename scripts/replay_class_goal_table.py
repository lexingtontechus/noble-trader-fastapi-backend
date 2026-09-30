"""Table: class / goal / best EV — exact ATR vs brick-quantised."""
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

OUT = ROOT / "worklog" / "kairos" / "reports" / "20260930_replay_class_goal.md"


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


def score(paths, tp, sl, cost=0.1):
    pnl, exp = walk(paths, float(tp), float(sl))
    e = float(exp.mean())
    tp_hit = (~exp) & (pnl > 0)
    sl_hit = (~exp) & (pnl <= 0)
    n_tp, n_sl = int(tp_hit.sum()), int(sl_hit.sum())
    dec = n_tp + n_sl
    if dec < 30:
        return None
    return {
        "tp": float(tp),
        "sl": float(sl),
        "wr": n_tp / dec,
        "exp": e,
        "ev": float((pnl - cost).mean()),
        "n": int(paths.shape[0]),
    }


async def main():
    peers = await fetch_active_symbols()
    by = defaultdict(list)
    for p in peers:
        if p.get("tradingview_symbol"):
            by[asset_class_key(p.get("asset_class"))].append(p)
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=14)
    lines = [
        "# Replay — class / goal / best · exact ATR vs brick-quantised",
        "",
        f"Window {since.date()} → {until.date()} · cost 0.1 ATR · band w∈[0.40,0.60] exp≤0.20",
        "",
        "| class | TP | SL | RR | wr | exp | EV | goal | note |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    print(lines[-1])
    for cls in sorted(by):
        mem = by[cls]
        syms = [m["symbol"] for m in mem]
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
        best = None
        for tp in [round(x, 1) for x in np.arange(0.5, 15.1, 0.5)] + [20.0, 25.0, 30.0]:
            for sl in [round(x, 1) for x in np.arange(0.5, 15.1, 0.5)] + [20.0, 25.0, 30.0]:
                if tp / sl < 1.0:
                    continue
                sc = score(paths, tp, sl)
                if not sc or sc["exp"] > 0.20:
                    continue
                if not (0.40 <= sc["wr"] <= 0.60):
                    continue
                if best is None or sc["ev"] > best["ev"]:
                    best = sc
        if not best:
            row = f"| {cls} | — | — | — | — | — | — | NO | no cell in band |"
            print(row)
            lines.append(row)
            continue
        goal = "YES" if best["wr"] >= 0.5 and best["exp"] <= 0.20 else "BAL"
        note = (
            "GOAL+EV"
            if best["ev"] > 0 and best["wr"] >= 0.5
            else ("BAL+EV" if best["ev"] > 0 else "EV&lt;0")
        )
        row = (
            f"| {cls} | {best['tp']:g} | {best['sl']:g} | {best['tp']/best['sl']:.2f} | "
            f"{best['wr']:.3f} | {best['exp']:.3f} | **{best['ev']:+.3f}** | {goal} | {note} |"
        )
        print(row)
        lines.append(row)
        # brick-quantised: sl_bricks=3 → tp_bricks=round(tp/sl*3); TP ATR becomes sl*tp_b/3
        tp_b = max(1, int(round(best["tp"] / best["sl"] * 3)))
        q_tp = best["sl"] * tp_b / 3.0
        q_sl = best["sl"]
        scq = score(paths, q_tp, q_sl)
        if scq:
            rowq = (
                f"| {cls} (brick) | {q_tp:.2f} | {q_sl:.2f} | {q_tp/q_sl:.2f} | "
                f"{scq['wr']:.3f} | {scq['exp']:.3f} | {scq['ev']:+.3f} | — | "
                f"quantised tp_bricks={tp_b} |"
            )
            print(rowq)
            lines.append(rowq)
    lines += [
        "",
        "## Crypto / fractional ATR",
        "",
        "Crypto best in-band is a **small TP/SL** (often 1–2 ATR). Brick snap with",
        "sl_bricks=3 moves TP by up to **0.5 × sl_atr** (round(tp/sl*3)). On 1 ATR",
        "TP that is a **large relative error** — live geometry ≠ validated geometry.",
        "Exact price brackets (`entry ± tp_atr×ATR`) keep the walk’s EV.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
