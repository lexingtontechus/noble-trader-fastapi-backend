"""
sim_atr_goal.py — solve TP/SL ATR so wr>=0.5 and exp<=0.2 (goal).

True first-passage replay on historical paths (same walk() as live
dynamic_atr_pick), not bucketing of observed geometry.

Run: PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/sim_atr_goal.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
from regime_platform.sweep_orchestrator.tiered_atr import (
    EXPIRY_BARS,
    asset_class_key,
    build_paths,
    walk,
)

OUT = ROOT / "worklog" / "kairos" / "reports" / "20260929_sim_atr_goal.md"

# dense grid (ATR units)
TP_GRID = [round(x, 1) for x in np.arange(0.5, 20.1, 0.5)] + [22, 25, 30, 35, 40, 50]
SL_GRID = [round(x, 1) for x in np.arange(0.5, 12.1, 0.5)] + [15, 20, 25, 30]
MIN_RR = 1.0
COST = 0.1
GOAL_WR = 0.50
GOAL_EXP = 0.20


async def load_signals(symbols: list[str], since: datetime, until: datetime):
    import httpx

    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    rows = []
    off = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={
                    "select": "symbol,signal,ts,entry_price,atr",
                    "source": "eq.sweep-light",
                    "signal": "in.(buy,sell)",
                    "symbol": f"in.({','.join(symbols)})",
                    "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                    "order": "ts.asc",
                    "limit": "1000",
                    "offset": str(off),
                },
            )
            resp.raise_for_status()
            batch = resp.json() or []
            rows += batch
            off += len(batch)
            if len(batch) < 1000:
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
        key = (r["symbol"], r["signal"], int(ts // 3600))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "symbol": r["symbol"],
                "signal": r["signal"],
                "entry_price": entry,
                "atr": atr,
                "_ts": ts,
            }
        )
    return out


def score_paths(paths: np.ndarray, tp: float, sl: float) -> dict | None:
    if paths.size == 0 or paths.shape[0] < 30:
        return None
    if sl <= 0 or tp / sl < MIN_RR:
        return None
    pnl, expired = walk(paths, float(tp), float(sl))
    exp = float(expired.mean())
    # TP if pnl close to +tp, SL if close to -sl
    tp_hit = (~expired) & (pnl > 0)
    sl_hit = (~expired) & (pnl <= 0)
    n_tp, n_sl = int(tp_hit.sum()), int(sl_hit.sum())
    dec = n_tp + n_sl
    wr = n_tp / dec if dec else 0.0
    mean_pnl = float((pnl - COST).mean())
    return {
        "tp": float(tp),
        "sl": float(sl),
        "n": int(paths.shape[0]),
        "exp": exp,
        "wr": wr,
        "pnl": mean_pnl,
        "n_tp": n_tp,
        "n_sl": n_sl,
        "dec": dec,
        "goal": wr >= GOAL_WR and exp <= GOAL_EXP,
    }


async def main() -> None:
    peers = await fetch_active_symbols()
    by_class = defaultdict(list)
    for p in peers:
        if not p.get("tradingview_symbol"):
            continue
        by_class[asset_class_key(p.get("asset_class"))].append(p)

    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=14)
    lines = [
        "# ATR goal simulation — wr>=0.5, exp<=0.2",
        "",
        f"Window {since.date()} → {until.date()} · walk() first-passage · cost {COST} ATR · min_rr {MIN_RR}",
        "",
    ]

    for cls in sorted(by_class):
        members = by_class[cls]
        syms = [m["symbol"] for m in members]
        print(f"=== {cls} {syms}", flush=True)
        signals = await load_signals(syms, since, until)
        if len(signals) < 30:
            lines.append(f"## {cls} — insufficient signals ({len(signals)})")
            lines.append("")
            continue
        bars: dict[str, list] = {}
        await fetch_tradingview_batch(
            [
                {"symbol": m["symbol"], "tradingview_symbol": m["tradingview_symbol"]}
                for m in members
            ],
            hours=24 * 20,
            interval="1h",
            ohlcv_out=bars,
        )
        bars = {
            s: [b for b in rows if b.get("t") is not None and b.get("c") is not None]
            for s, rows in bars.items()
        }
        paths = build_paths(signals, bars)
        print(f"  signals={len(signals)} paths={paths.shape}", flush=True)

        results = []
        for tp in TP_GRID:
            for sl in SL_GRID:
                sc = score_paths(paths, tp, sl)
                if sc:
                    results.append(sc)
        goals = [r for r in results if r["goal"]]
        goals.sort(key=lambda r: -r["pnl"])
        # also top by wr among exp<=0.2
        tight = [r for r in results if r["exp"] <= GOAL_EXP]
        tight.sort(key=lambda r: (-r["wr"], -r["pnl"]))

        lines.append(f"## class `{cls}` · paths={paths.shape[0]} · signals={len(signals)}")
        lines.append("")
        if goals:
            lines.append(f"**MEETS GOAL: {len(goals)} cells** — top 8 by pnl:")
            lines.append("")
            lines.append("| TP | SL | n | wr | exp | pnl | tp | sl |")
            lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
            for r in goals[:8]:
                lines.append(
                    f"| {r['tp']:g} | {r['sl']:g} | {r['n']} | {r['wr']:.3f} | "
                    f"{r['exp']:.3f} | {r['pnl']:+.3f} | {r['n_tp']} | {r['n_sl']} |"
                )
        else:
            lines.append("**No cell meets wr>=0.5 & exp<=0.2** — best under exp<=0.2 by wr:")
            lines.append("")
            lines.append("| TP | SL | n | wr | exp | pnl | tp | sl |")
            lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
            for r in tight[:8]:
                lines.append(
                    f"| {r['tp']:g} | {r['sl']:g} | {r['n']} | {r['wr']:.3f} | "
                    f"{r['exp']:.3f} | {r['pnl']:+.3f} | {r['n_tp']} | {r['n_sl']} |"
                )
        lines.append("")

        # top 5 overall by wr with n>=50
        big = [r for r in results if r["n"] >= 50]
        big.sort(key=lambda r: (-r["wr"], -r["exp"]))
        lines.append("Top by wr (n>=50):")
        lines.append("")
        lines.append("| TP | SL | n | wr | exp | pnl |")
        lines.append("|---:|---:|---:|---:|---:|---:|")
        for r in big[:5]:
            lines.append(
                f"| {r['tp']:g} | {r['sl']:g} | {r['n']} | {r['wr']:.3f} | {r['exp']:.3f} | {r['pnl']:+.3f} |"
            )
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {OUT}", flush=True)
    print("\n".join(lines), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
