#!/usr/bin/env python3
"""Prove XAUUSD's band was set on a close-walk basis the platform no longer uses.

`scripts/vol_atr_solver.py` (which set XAUUSD 4.94/1.00 on 2026-09-30) imports the
platform's own walk — `from ...tiered_atr import EXPIRY_BARS, walk` — so the
arithmetic is identical. What differs is the path:

    solver:  path = (closes - entry) * d / atr      <- CLOSE-ONLY, blind to wicks
    live:    build_envelopes(..., basis=live_basis())  <- intrabar high/low

This replays the 09-30 window on both. If CLOSE reproduces the recorded
EV +1.23 / wr 0.405 and INTRABAR does not, the band was valid on its own day's
basis and the 2026-10-02 cutover to intrabar is what invalidated it.

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/replay_xauusd_basis.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
for line in (ROOT / ".env.local").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import logging  # noqa: E402

logging.disable(logging.INFO)
import httpx  # noqa: E402
import numpy as np  # noqa: E402

from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch  # noqa: E402
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import (  # noqa: E402
    EXPIRY_BARS,
    build_envelopes,
    live_basis,
    walk,
)

SYMBOL, TV = "XAUUSD", "OANDA:XAUUSD"
CELL = (4.94, 1.00)                                  # the shipped pick
SINCE = datetime(2026, 8, 16, tzinfo=timezone.utc)   # 45d window that set the band
UNTIL = datetime(2026, 9, 30, tzinfo=timezone.utc)
RECORDED = "recorded 2026-09-30: EV +1.23  wr 0.405  h1 +2.12  h2 +0.34  n 817"


async def signals() -> list[dict]:
    rows, off = [], 0
    async with httpx.AsyncClient(timeout=30.0) as c:
        while True:
            r = await c.get(_url("nt_signal_sim"), headers=_headers(), params={
                "select": "symbol,signal,ts,entry_price,atr",
                "source": "eq.sweep-light", "signal": "in.(buy,sell)",
                "symbol": f"eq.{SYMBOL}",
                "and": f"(ts.gte.{SINCE.isoformat()},ts.lt.{UNTIL.isoformat()})",
                "order": "ts.asc", "limit": "1000", "offset": str(off)})
            r.raise_for_status()
            batch = r.json() or []
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
        out.append({"symbol": r["symbol"], "signal": r["signal"],
                    "entry_price": entry, "atr": atr, "_ts": ts})
    return out


def close_paths(sig: list[dict], bars: dict) -> np.ndarray:
    """The solver's path — closes only (vol_atr_solver.build_paths_meta)."""
    out = []
    for r in sorted(sig, key=lambda x: x["_ts"]):
        rows = bars.get(r["symbol"]) or []
        t = np.array([float(b["t"]) for b in rows], dtype="float64")
        c = np.array([float(b["c"]) for b in rows], dtype="float64")
        if not len(t):
            continue
        j = int(np.searchsorted(t, r["_ts"], side="right")) - 1
        if j < 0 or j + EXPIRY_BARS > len(t):
            continue
        d = 1.0 if r["signal"] == "buy" else -1.0
        out.append((c[j:j + EXPIRY_BARS] - r["entry_price"]) * d / r["atr"])
    return np.vstack(out) if out else np.empty((0, EXPIRY_BARS))


def intrabar(env: np.ndarray, tp: float, sl: float):
    """First-passage on the live envelope, sl_first (mirrors walk_envelopes)."""
    fav, adv = env[:, :, 0], env[:, :, 1]
    hit_tp, hit_sl = fav >= tp, adv <= -sl
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    sl_first = (si < EXPIRY_BARS) & (si <= ti)
    tp_first = ~sl_first & (ti < EXPIRY_BARS)
    pnl = np.where(tp_first, tp, np.where(sl_first, -sl, 0.0))
    return pnl, tp_first, sl_first


def report(label: str, pnl, tp_f, sl_f, n: int, cost: float = 0.1) -> float:
    net = pnl - cost
    mid = n // 2
    dec = int(tp_f.sum() + sl_f.sum())
    wr = (tp_f.sum() / dec) if dec else float("nan")
    print(f"  {label:>9} n={n:>4}  wr={wr:>6.3f}  e={net.mean():>+7.3f}  "
          f"e1={net[:mid].mean():>+7.3f}  e2={net[mid:].mean():>+7.3f}  sl={sl_f.mean():>5.1%}")
    return float(sl_f.mean())


async def main() -> int:
    sig = await signals()
    bars: dict = {}
    await fetch_tradingview_batch([{"symbol": SYMBOL, "tradingview_symbol": TV}],
                                  hours=24 * 65, interval="1h", ohlcv_out=bars)
    bars = {s: [b for b in v if b.get("t") is not None and b.get("c") is not None]
            for s, v in bars.items()}
    print(f"{SYMBOL} band provenance replay — window {SINCE.date()}..{UNTIL.date()}  "
          f"signals={len(sig)}")
    print(f"cell {CELL[0]:g}/{CELL[1]:g}   {RECORDED}\n")

    cp = close_paths(sig, bars)
    pnl_c, exp_c = walk(cp, CELL[0], CELL[1])
    sl_c = report("CLOSE", pnl_c, (~exp_c) & (pnl_c > 0), (~exp_c) & (pnl_c <= 0), len(cp))

    env = build_envelopes(sig, bars, basis=live_basis())
    pnl_i, tp_i, sl_i = intrabar(env, CELL[0], CELL[1])
    sl_i_share = report("INTRABAR", pnl_i, tp_i, sl_i, len(env))

    if len(cp) == len(env):
        wick_only = int((sl_i & ~((~exp_c) & (pnl_c <= 0))).sum())
        print(f"\n  wick-only stop-outs (intrabar stops out, close never crosses): "
              f"{wick_only}/{len(env)} ({100*wick_only/len(env):.1f}%)")
        print(f"  close-vs-intrabar stop-out gap: {100*(sl_i_share - sl_c):.1f} points "
              f"— this is the basis artefact that set the band")
    print("\n  CLOSE reproducing the recorded figures = the band was solved on a basis "
          "the platform retired at the 2026-10-02 cutover.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
