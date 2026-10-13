#!/usr/bin/env python3
"""Re-decide the 2026-10-12 band reconciliation on a CLEAN dataset.

The original decision used `_load_class_signals`, which applies neither the
cutover clamp nor `contaminated=false`. This re-runs every band the
reconciliation CHANGED (FOREX, XAGUSD, XAUAUD) on clean evidence, comparing the
git band against the reconciled one.

Clean = `contaminated=eq.false` AND `ts >= nt_calibration_cutover`.
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

from regime_platform.core.feature_flags import get_dynamic_atr_config, get_min_rr  # noqa: E402
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch  # noqa: E402
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import (  # noqa: E402
    EXPIRY_BARS,
    build_envelopes,
    live_basis,
    live_same_bar_policy,
    walk_envelopes,
)

FOREX = ["AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY"]
# scope -> (symbols, git band, reconciled band)   (tp, sl, tp_min, tp_max, sl_min, sl_max)
B = lambda tp, sl, tmin, tmax, smin, smax: (tp, sl, tmin, tmax, smin, smax)  # noqa: E731
SCOPES = {
    "FOREX": (FOREX,
              B([1.65, 1.71, 1.75, 1.80], [0.30, 0.35, 0.40], 1.60, 1.85, 0.30, 0.45),
              B([4.27, 4.52, 4.77], [2.63, 2.75, 2.87], 4.02, 5.02, 2.51, 2.99)),
    "XAGUSD": (["XAGUSD"],
               B([7.80, 7.96, 8.10], [7.60, 7.76, 7.90], 7.5, 8.3, 7.4, 8.1),
               B([6.14, 6.39, 6.64], [2.57, 2.69, 2.81], 5.89, 6.89, 2.45, 2.93)),
    "XAUAUD": (["XAUAUD"],
               B([6.20, 6.31, 6.45], [4.80, 4.91, 5.05], 6.0, 6.6, 4.7, 5.2),
               B([6.32, 6.57, 6.82], [4.19, 4.31, 4.43], 6.07, 7.07, 4.07, 4.55)),
}
TV = {"XAGUSD": "OANDA:XAGUSD", "XAUAUD": "OANDA:XAUAUD"}


async def cutover():
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(_url("nt_calibration_cutover"), headers=_headers(),
                        params={"select": "cutover_at", "limit": "1"})
        r.raise_for_status()
        rows = r.json() or []
    return (datetime.fromisoformat(str(rows[0]["cutover_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            if rows else None)


async def clean_signals(syms, since, until):
    rows, off = [], 0
    async with httpx.AsyncClient(timeout=30.0) as c:
        while True:
            r = await c.get(_url("nt_signal_sim"), headers=_headers(), params={
                "select": "symbol,signal,ts,entry_price,atr",
                "source": "eq.sweep-light", "signal": "in.(buy,sell)",
                "symbol": f"in.({','.join(syms)})",
                "and": f"(ts.gte.{since.isoformat()},ts.lt.{until.isoformat()})",
                "contaminated": "eq.false",
                "order": "ts.asc", "limit": "1000", "offset": str(off),
            })
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


def best(env, band, min_rr, max_exp, cost):
    tp_g, sl_g, tmin, tmax, smin, smax = band
    n = len(env)
    if n < 60:
        return None
    mid = n // 2
    out = []
    for tp in tp_g:
        for sl in sl_g:
            tp_f, sl_f = float(tp), float(sl)
            if sl_f <= 0 or tp_f / sl_f < min_rr:
                continue
            if not (tmin <= tp_f <= tmax and smin <= sl_f <= smax):
                continue
            pnl, exp = walk_envelopes(env, tp_f, sl_f, same_bar_policy=live_same_bar_policy())
            net = pnl - cost
            out.append((float(net.mean()), float(net[:mid].mean()), float(net[mid:].mean()),
                        tp_f, sl_f, float(exp.mean())))
    return max(out) if out else None


async def main():
    cut = await cutover()
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = max(cut, until - timedelta(days=10)) if cut else until - timedelta(days=10)
    cfg = get_dynamic_atr_config("forex", "EURUSD")
    min_rr, max_exp, cost = get_min_rr(), cfg["max_expiry"], cfg["cost_atr"]
    print(f"CLEAN dataset: contaminated=false AND ts>={since.date()}  "
          f"(cutover {cut.date() if cut else '?'})")
    print(f"min_rr={min_rr} max_expiry={max_exp} cost_atr={cost}\n")
    print(f"{'scope':8} {'band':11} {'best tp/sl':>13} {'e1':>8} {'e2':>8} {'e_mean':>8}  verdict")
    for scope, (syms, git_b, rec_b) in SCOPES.items():
        sig = await clean_signals(syms, since, until)
        bars: dict = {}
        await fetch_tradingview_batch(
            [{"symbol": s, "tradingview_symbol": TV.get(s, f"OANDA:{s}")} for s in syms],
            hours=24 * 20, interval="1h", ohlcv_out=bars)
        bars = {s: [b for b in v if b.get("t") is not None and b.get("c") is not None]
                for s, v in bars.items()}
        env, _ = build_envelopes(sig, bars, basis=live_basis(), return_vol=True)
        res = {"git": best(env, git_b, min_rr, max_exp, cost),
               "reconciled": best(env, rec_b, min_rr, max_exp, cost)}
        for name in ("git", "reconciled"):
            r = res[name]
            if r is None:
                print(f"{scope:8} {name:11} {'no in-band cell':>13}")
            else:
                e, e1, e2, tp, sl, em = r
                print(f"{scope:8} {name:11} {f'{tp:g}/{sl:g}':>13} {e1:>8.3f} {e2:>8.3f} {e:>8.3f}")
        g, r = res["git"], res["reconciled"]
        if g is None and r is None:
            v = "NO_DATA"
        elif g is None:
            v = "RECONCILED (git unusable)"
        elif r is None:
            v = "GIT (reconciled unusable)"
        elif abs(g[0] - r[0]) < 5e-4:
            v = "TIE"
        else:
            v = "GIT" if g[0] > r[0] else "RECONCILED"
        print(f"{'':8} {'':11} {'':>13} {'':>8} {'':>8} {'':>8}  -> {v}   (n={len(env)})\n")


asyncio.run(main())
