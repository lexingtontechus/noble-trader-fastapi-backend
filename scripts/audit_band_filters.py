#!/usr/bin/env python3
"""Review check: for EVERY live band, which candidate filter kills the cells?

Settles whether D3's expiry-cap mechanism applies anywhere in the shipped config,
or only to the widest brackets (a 48-bar horizon makes barrier touch near-certain
for a narrow bracket, so expiry only bites when tp+sl is large).
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
from regime_platform.core.feature_flags import get_dynamic_atr_config, get_min_rr  # noqa: E402
from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch  # noqa: E402
from regime_platform.sweep_orchestrator.tiered_atr import (  # noqa: E402
    EXPIRY_BARS,
    _load_class_signals,
    build_envelopes,
    live_basis,
    live_same_bar_policy,
    walk_envelopes,
)

# the scopes worth checking: the widest bands + the forex book + one metal
SCOPES = [("US500", "US500"), ("US30USD", "US30USD"), ("XAGUSD", "XAGUSD"),
          ("XAUUSD", "XAUUSD"), ("BTCUSD", "BTCUSD"), ("AUDUSD", "AUDUSD")]


async def one(sym: str):
    cfg = get_dynamic_atr_config(None, sym)
    until = datetime.now(timezone.utc) - timedelta(hours=EXPIRY_BARS + 2)
    since = until - timedelta(days=10)
    sig = await _load_class_signals([sym], since, until)
    bars: dict = {}
    await fetch_tradingview_batch(
        [{"symbol": sym, "tradingview_symbol": f"OANDA:{sym}"}],
        hours=24 * 15, interval="1h", ohlcv_out=bars)
    bars = {s: [b for b in v if b.get("t") is not None and b.get("c") is not None]
            for s, v in bars.items()}
    env, _ = build_envelopes(sig, bars, basis=live_basis(), return_vol=True)
    if len(env) < 60:
        print(f"  {sym:9} n={len(env):>4}  too thin to judge")
        return
    n = len(env)
    mid = n // 2
    min_rr = get_min_rr()
    kills: dict[str, int] = {}
    max_exp = 0.0
    for tp in cfg["tp_atr"]:
        for sl in cfg["sl_atr"]:
            tp_f, sl_f = float(tp), float(sl)
            if sl_f <= 0 or tp_f / sl_f < min_rr:
                kills["min_rr"] = kills.get("min_rr", 0) + 1
                continue
            if not (cfg["tp_atr_min"] <= tp_f <= cfg["tp_atr_max"]
                    and cfg["sl_atr_min"] <= sl_f <= cfg["sl_atr_max"]):
                kills["clamp"] = kills.get("clamp", 0) + 1
                continue
            pnl, exp = walk_envelopes(env, tp_f, sl_f, same_bar_policy=live_same_bar_policy())
            em = float(exp.mean())
            max_exp = max(max_exp, em)
            net = pnl - cfg["cost_atr"]
            if em > cfg["max_expiry"]:
                k = "expiry"
            elif float(net[:mid].mean()) <= 0 or float(net[mid:].mean()) <= 0:
                k = "both-halves"
            else:
                k = "PASSES"
            kills[k] = kills.get(k, 0) + 1
    width = max(cfg["tp_atr"]) + max(cfg["sl_atr"])
    print(f"  {sym:9} n={len(env):>4}  tp+sl max={width:>5.1f}ATR  "
          f"max walk expiry={max_exp:.4f}  kills={kills}")


async def main():
    print(f"max_expiry cap = {get_dynamic_atr_config(None, 'XAUUSD')['max_expiry']}\n")
    for label, sym in SCOPES:
        await one(sym)


asyncio.run(main())
