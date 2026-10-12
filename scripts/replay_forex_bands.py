#!/usr/bin/env python3
"""Replay: forex geometry under by_symbol.FOREX (tight) vs by_class.forex (wide
reopt proposal) vs GLOBAL — which band does the evidence justify?

Same forex pooled envelope for all three; only the grid+clamps differ.
Runs select_tp_sl on each and dumps the candidate grid (e1/e2/expiry).
"""

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
import logging

logging.disable(logging.INFO)
from regime_platform.core.feature_flags import get_min_rr
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
from regime_platform.sweep_orchestrator.supabase_client import fetch_active_symbols
from regime_platform.sweep_orchestrator.tiered_atr import (
    EXPIRY_BARS,
    _load_class_signals,
    build_envelopes,
    live_basis,
    live_same_bar_policy,
    select_tp_sl,
    walk_envelopes,
)

BANDS = {
    "by_symbol.FOREX  (git, tight)": dict(
        tp=[1.65, 1.71, 1.75, 1.80], sl=[0.30, 0.35, 0.40], tp_min=1.60, tp_max=1.85, sl_min=0.30, sl_max=0.45
    ),
    "by_class.forex   (studio, wide)": dict(
        tp=[4.27, 4.52, 4.77], sl=[2.63, 2.75, 2.87], tp_min=4.02, tp_max=5.02, sl_min=2.51, sl_max=2.99
    ),
    "GLOBAL           (pooled now)": dict(
        tp=[1.71, 2.0, 3.0, 5.0, 8.0, 13.0],
        sl=[0.35, 1.0, 2.0, 3.5, 5.0, 8.0],
        tp_min=0.20,
        tp_max=20.0,
        sl_min=0.15,
        sl_max=12.0,
    ),
}
MIN_RR, MAX_EXP, COST = get_min_rr(), 0.20, 0.1


async def main():
    peers = await fetch_active_symbols()
    members = [p for p in peers if p["asset_class"].lower() == "forex" and p.get("tradingview_symbol")]
    print(f"forex members: {[m['symbol'] for m in members]}")
    lookback = 10
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=lookback)
    signals = await _load_class_signals([m["symbol"] for m in members], since, until)
    bars = {}
    await fetch_tradingview_batch(
        [{"symbol": m["symbol"], "tradingview_symbol": m["tradingview_symbol"]} for m in members],
        hours=24 * (lookback + 5),
        interval="1h",
        ohlcv_out=bars,
    )
    bars = {s: [b for b in rows if b.get("t") is not None and b.get("c") is not None] for s, rows in bars.items()}
    env, _ = build_envelopes(signals, bars, basis=live_basis(), return_vol=True)
    n = len(env)
    mid = n // 2
    print(f"envelopes: {n} (mid={mid})  min_rr={MIN_RR} max_expiry={MAX_EXP} cost_atr={COST}")
    print(f"window: {since.date()} .. {until.date()}\n")

    for label, b in BANDS.items():
        print(f"=== {label}  tp={b['tp']} sl={b['sl']} clamps tp=[{b['tp_min']},{b['tp_max']}] ===")
        best = select_tp_sl(
            envelopes=env,
            same_bar_policy=live_same_bar_policy(),
            tp_grid=[float(x) for x in b["tp"]],
            sl_grid=[float(x) for x in b["sl"]],
            min_rr=MIN_RR,
            max_expiry=MAX_EXP,
            cost_atr=COST,
            min_signals=60,
            tp_atr_min=b["tp_min"],
            tp_atr_max=b["tp_max"],
            sl_atr_min=b["sl_min"],
            sl_atr_max=b["sl_max"],
        )
        print(f"  {'tp':>6} {'sl':>6} {'RR':>6} {'e1':>8} {'e2':>8} {'e_mean':>8} {'expiry':>7}  verdict")
        for tp in b["tp"]:
            for sl in b["sl"]:
                tp_f, sl_f = float(tp), float(sl)
                rr = tp_f / sl_f
                if sl_f <= 0 or rr < MIN_RR:
                    print(f"  {tp_f:>6} {sl_f:>6} {rr:>6.2f} {'':>8} {'':>8} {'':>8} {'':>7}  SKIP RR")
                    continue
                if not (b["tp_min"] <= tp_f <= b["tp_max"] and b["sl_min"] <= sl_f <= b["sl_max"]):
                    print(f"  {tp_f:>6} {sl_f:>6} {rr:>6.2f} {'':>8} {'':>8} {'':>8} {'':>7}  SKIP clamp")
                    continue
                pnl, exp = walk_envelopes(env, tp_f, sl_f, same_bar_policy=live_same_bar_policy())
                em = float(exp.mean())
                net = pnl - COST
                e1, e2, e = float(net[:mid].mean()), float(net[mid:].mean()), float(net.mean())
                v = "SKIP expiry" if em > MAX_EXP else ("PASS ***" if (e1 > 0 and e2 > 0) else "both-halves fail")
                print(f"  {tp_f:>6} {sl_f:>6} {rr:>6.2f} {e1:>8.3f} {e2:>8.3f} {e:>8.3f} {em:>7.3f}  {v}")
        print(f"  -> select_tp_sl: {best.as_log() if best else 'None (no bracket clears both halves)'}\n")


asyncio.run(main())
