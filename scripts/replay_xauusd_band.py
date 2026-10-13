#!/usr/bin/env python3
"""Replay-justify (or refute) widening XAUUSD's stop.

Context: XAUUSD's shipped band is tp=[4.8, 4.94, 5.1] sl=[0.95, 1.0, 1.1]. Its
sl=1.0 ATR sits in the worst bucket of the platform's own distance->outcome
curve (66.9% of historical sl_hits at 1.0 ATR land within one bar), so it is the
one deployed symbol still exposed to immediate stop-outs. BUT the band is not
arbitrary: config/feature_flags.yaml records it as the 2026-09-30 vol-regime
45-day pooled walk (EV +1.23, wr 0.405). A tight stop at ~5:1 RR can be
EV-positive *because* of a high immediate-stop rate, so "it stops out fast" is
not by itself an argument to widen it.

This replays the full RR-valid grid on two datasets and reports, per cell:
stop-out share, the share that stop out within one bar, win rate, mean net
expectancy and both halves. Then it says which side the evidence is on.

Datasets:
  era-clean  contaminated=false AND ts >= nt_calibration_cutover  (~11 days)
  45d        the original decision's window, unfiltered, for comparison

  PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/replay_xauusd_band.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
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

from regime_platform.core.feature_flags import get_dynamic_atr_config, get_min_rr  # noqa: E402
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch  # noqa: E402
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import (  # noqa: E402
    EXPIRY_BARS,
    SAME_BAR_SL_FIRST,
    build_envelopes,
    live_basis,
    live_same_bar_policy,
)

SYMBOL = "XAUUSD"
TV = "OANDA:XAUUSD"
CURRENT = (4.94, 1.0)          # the shipped pick

TP_GRID = [3.0, 3.5, 4.0, 4.5, 4.94, 5.5, 6.5, 8.0, 10.0]
SL_GRID = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]


async def cutover() -> datetime | None:
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(_url("nt_calibration_cutover"), headers=_headers(),
                        params={"select": "cutover_at", "limit": "1"})
        r.raise_for_status()
        rows = r.json() or []
    return (datetime.fromisoformat(str(rows[0]["cutover_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            if rows else None)


async def signals(since: datetime, until: datetime, *, clean: bool) -> list[dict]:
    rows, off = [], 0
    async with httpx.AsyncClient(timeout=30.0) as c:
        while True:
            p = {
                "select": "symbol,signal,ts,entry_price,atr",
                "source": "eq.sweep-light", "signal": "in.(buy,sell)",
                "symbol": f"eq.{SYMBOL}",
                "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                "order": "ts.asc", "limit": "1000", "offset": str(off),
            }
            if clean:
                p["contaminated"] = "eq.false"
            r = await c.get(_url("nt_signal_sim"), headers=_headers(), params=p)
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
        k = (r["symbol"], r["signal"], int(ts // 3600))
        if k in seen:
            continue
        seen.add(k)
        out.append({"symbol": r["symbol"], "signal": r["signal"],
                    "entry_price": entry, "atr": atr, "_ts": ts})
    return out


def walk(env: np.ndarray, tp: float, sl: float, policy: str):
    """(net-atr pnl, tp_first, sl_first, bars_to_exit) — mirrors walk_envelopes."""
    n = len(env)
    if n == 0:
        z = np.zeros(0, dtype=bool)
        return np.zeros(0), z, z, np.zeros(0, dtype=int)
    fav, adv = env[:, :, 0], env[:, :, 1]
    hit_tp, hit_sl = fav >= tp, adv <= -sl
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    if str(policy).strip().lower() == SAME_BAR_SL_FIRST:
        sl_first = (si < EXPIRY_BARS) & (si <= ti)
        tp_first = ~sl_first & (ti < EXPIRY_BARS)
    else:
        tp_first = (ti < EXPIRY_BARS) & (ti <= si)
        sl_first = ~tp_first & (si < EXPIRY_BARS)
    pnl = np.where(tp_first, tp, np.where(sl_first, -sl, 0.0))
    bars = np.where(tp_first, ti, np.where(sl_first, si, EXPIRY_BARS - 1))
    return pnl, tp_first, sl_first, bars


def grid(env: np.ndarray, min_rr: float, cost: float):
    n = len(env)
    mid = n // 2
    policy = live_same_bar_policy()
    out = []
    for tp in TP_GRID:
        for sl in SL_GRID:
            if sl <= 0 or tp / sl < min_rr:
                continue
            pnl, tp_f, sl_f, bars = walk(env, tp, sl, policy)
            net = pnl - cost
            decided = tp_f | sl_f
            out.append({
                "tp": tp, "sl": sl,
                "sl_share": float(sl_f.mean()),
                "imm": float((sl_f & (bars <= 1)).mean()),
                "wr": (float(tp_f.sum() / decided.sum()) if decided.sum() else float("nan")),
                "e": float(net.mean()),
                "e1": float(net[:mid].mean()),
                "e2": float(net[mid:].mean()),
                "n": n,
            })
    return out


def show(rows, title, n):
    print(f"\n=== {title}  (n={n} envelopes) ===")
    print(f"  {'tp/sl':>10} {'RR':>5} {'sl%':>7} {'imm<=1bar%':>11} {'wr':>6} "
          f"{'e_mean':>8} {'e1':>7} {'e2':>7}  flag")
    best = max(rows, key=lambda r: r["e"]) if rows else None
    for r in sorted(rows, key=lambda r: (-r["e"])):
        flag = ""
        if (r["tp"], r["sl"]) == CURRENT:
            flag += "CURRENT "
        if best and r is best:
            flag += "BEST-E "
        if r["e1"] > 0 and r["e2"] > 0:
            flag += "both-halves "
        cell = f"{r['tp']:g}/{r['sl']:g}"
        print(f"  {cell:>10} "
              f"{r['tp']/r['sl']:>5.2f} {100*r['sl_share']:>6.1f}% {100*r['imm']:>10.1f}% "
              f"{r['wr']:>6.3f} {r['e']:>8.3f} {r['e1']:>7.3f} {r['e2']:>7.3f}  {flag}")
    return best


async def main() -> int:
    cut = await cutover()
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    cfg = get_dynamic_atr_config(None, SYMBOL)
    min_rr, cost = get_min_rr(), cfg["cost_atr"]
    print(f"XAUUSD shipped band: tp={cfg['tp_atr']} sl={cfg['sl_atr']} "
          f"clamps tp=[{cfg['tp_atr_min']},{cfg['tp_atr_max']}] sl=[{cfg['sl_atr_min']},{cfg['sl_atr_max']}]")
    print(f"min_rr={min_rr}  cost_atr={cost}  max_expiry={cfg['max_expiry']}  "
          f"cutover={cut.date() if cut else '?'}")

    windows = {
        "era-clean": (max(cut, until - timedelta(days=10)) if cut else until - timedelta(days=10), True),
        "45d": (until - timedelta(days=45), False),
    }
    for name, (since, clean) in windows.items():
        sig = await signals(since, until, clean=clean)
        bars: dict = {}
        await fetch_tradingview_batch(
            [{"symbol": SYMBOL, "tradingview_symbol": TV}],
            hours=24 * (50 if name == "45d" else 20), interval="1h", ohlcv_out=bars)
        bars = {s: [b for b in v if b.get("t") is not None and b.get("c") is not None]
                for s, v in bars.items()}
        env = build_envelopes(sig, bars, basis=live_basis())
        rows = grid(env, min_rr, cost)
        best = show(rows, f"{name} (since {since.date()})", len(env))
        cur = next((r for r in rows if (r["tp"], r["sl"]) == CURRENT), None)
        if cur and best:
            print(f"  -> current {CURRENT[0]}/{CURRENT[1]}: e={cur['e']:+.3f} "
                  f"e1={cur['e1']:+.3f} e2={cur['e2']:+.3f} imm={100*cur['imm']:.1f}%")
            print(f"  -> best-e    {best['tp']}/{best['sl']}: e={best['e']:+.3f} "
                  f"e1={best['e1']:+.3f} e2={best['e2']:+.3f} imm={100*best['imm']:.1f}%")
            wider = [r for r in rows if r["sl"] > CURRENT[1] and r["e"] > cur["e"]]
            print(f"  -> wider-stop cells that BEAT the current one on e_mean: {len(wider)}")
            if wider:
                w = max(wider, key=lambda r: r["e"])
                print(f"     best wider: tp={w['tp']}/sl={w['sl']} e={w['e']:+.3f} "
                      f"(imm {100*w['imm']:.1f}% vs {100*cur['imm']:.1f}%)")
            else:
                print("     NONE — widening the stop does not pay on this dataset")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
