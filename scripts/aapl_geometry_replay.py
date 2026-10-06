#!/usr/bin/env python3
"""AAPL geometry replay — test TP/ATR × SL/ATR combos using the same
first-passage machinery as tiered_atr.py. No guessing: replay every combo
against AAPL's actual resolved signals and report expectancy.

Usage:
    PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/aapl_geometry_replay.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load .env.local
env_path = Path(__file__).resolve().parents[1] / ".env.local"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx
import numpy as np

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
now = datetime.now(timezone.utc)


def query(table, params, timeout=30):
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


async def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch
    from regime_platform.sweep_orchestrator.tiered_atr import build_paths, walk, EXPIRY_BARS

    # ── Step 1: Fetch AAPL signals ────────────────────────────────────────
    print("=== Step 1: Fetch AAPL signals ===")
    signals = query("nt_signal_sim", {
        "select": "symbol,ts,signal,outcome,entry_price,atr",
        "symbol": "eq.AAPL",
        "source": "eq.sweep-light",
        "signal": "in.(buy,sell)",
        "ts": f"gte.{(now - timedelta(days=90)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
        "limit": "1000",
    })
    if not isinstance(signals, list):
        print(f"  ERROR: {signals}")
        sys.exit(1)
    print(f"  Fetched {len(signals)} signals")

    # Deduplicate by (symbol, signal, hour) and add _ts for build_paths
    seen = set()
    deduped = []
    for r in signals:
        try:
            ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
            key = (r["signal"], int(ts.timestamp() // 3600))
            if key in seen:
                continue
            seen.add(key)
            r["_ts"] = ts.timestamp()
            deduped.append(r)
        except:
            continue
    print(f"  Deduped to {len(deduped)} episodes")

    # ── Step 2: Fetch AAPL 1h bars ────────────────────────────────────────
    print("\n=== Step 2: Fetch AAPL 1h bars ===")
    bars = {}
    await fetch_tradingview_batch(
        [{"symbol": "AAPL", "tradingview_symbol": "NASDAQ:AAPL"}],
        hours=24 * 95,
        interval="1h",
        ohlcv_out=bars,
    )
    aapl_bars = bars.get("AAPL", [])
    print(f"  Fetched {len(aapl_bars)} 1h bars")

    if not aapl_bars:
        print("  ERROR: No bars fetched")
        sys.exit(1)

    # ── Step 3: Build ATR-scaled paths ────────────────────────────────────
    print("\n=== Step 3: Build ATR-scaled paths ===")
    paths = build_paths(deduped, {"AAPL": aapl_bars})
    if paths is None or len(paths) == 0:
        print("  ERROR: No paths built")
        sys.exit(1)
    print(f"  Built {len(paths)} paths of {paths.shape[1]} bars")

    # ── Step 4: Test TP/ATR × SL/ATR combos ───────────────────────────────
    print("\n=== Step 4: Replay TP/ATR × SL/ATR grid ===")

    COST = 0.1
    TP_GRID = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]
    SL_GRID = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]

    results = []
    for tp in TP_GRID:
        for sl in SL_GRID:
            if tp / sl < 1.0:
                continue
            pnl, exp = walk(paths, float(tp), float(sl))
            tp_hit = (~exp) & (pnl > 0)
            sl_hit = (~exp) & (pnl <= 0)
            dec = int(tp_hit.sum() + sl_hit.sum())
            if dec < 20:
                continue
            wr = float(tp_hit.sum() / dec)
            net = pnl - COST
            mid = len(paths) // 2
            ev_h1 = float(net[:mid].mean()) if mid else float("nan")
            ev_h2 = float(net[mid:].mean()) if mid else float("nan")
            expiry = float(exp.mean())
            results.append({
                "tp_atr": tp,
                "sl_atr": sl,
                "rr": tp / sl,
                "n": int(paths.shape[0]),
                "dec": dec,
                "wr": wr,
                "exp": expiry,
                "ev": float(net.mean()),
                "ev_h1": ev_h1,
                "ev_h2": ev_h2,
            })

    # ── Step 5: Report ────────────────────────────────────────────────────
    print(f"\n=== Step 5: Results ({len(results)} combos with dec>=20) ===")
    print(f"{'TP/ATR':>7} {'SL/ATR':>7} {'RR':>5} {'n':>5} {'dec':>5} {'WR':>6} {'Exp':>6} {'EV':>7} {'EV_h1':>7} {'EV_h2':>7}")
    print("-" * 80)

    # Sort by EV descending
    results.sort(key=lambda r: -r["ev"])
    for r in results[:30]:
        print(f"{r['tp_atr']:>7.1f} {r['sl_atr']:>7.1f} {r['rr']:>5.2f} {r['n']:>5} {r['dec']:>5} {r['wr']:>5.1%} {r['exp']:>5.1%} {r['ev']:>+7.3f} {r['ev_h1']:>+7.3f} {r['ev_h2']:>+7.3f}")

    # ── Step 6: Best combo ────────────────────────────────────────────────
    print("\n=== Step 6: Best combo ===")
    if results:
        best = results[0]
        print(f"  TP/ATR = {best['tp_atr']}, SL/ATR = {best['sl_atr']}")
        print(f"  RR = {best['rr']:.2f}")
        print(f"  n = {best['n']}, dec = {best['dec']}")
        print(f"  WR = {best['wr']:.1%}")
        print(f"  EV = {best['ev']:+.3f}R")
        print(f"  EV_h1 = {best['ev_h1']:+.3f}R, EV_h2 = {best['ev_h2']:+.3f}R")
        print(f"  Expiry = {best['exp']:.1%}")

        # Convert to bricks
        atr = 0.51  # AAPL ATR from nt_brick_params_state
        brick_size = best['sl_atr'] * atr / 3  # sl_bricks=3
        tp_bricks = round(best['tp_atr'] / best['sl_atr'] * 3)
        print(f"\n  Current: brick_size=0.627, sl_bricks=3, tp_bricks=5 (TP/ATR=6.15)")
        print(f"  Proposed: brick_size={brick_size:.4f}, sl_bricks=3, tp_bricks={tp_bricks} (TP/ATR={best['tp_atr']:.1f})")
        print(f"\n  Config for feature_flags.yaml:")
        print(f"    AAPL: {{sma_window_days: 200, enabled: false, brick_size: {brick_size:.4f}, sl_bricks: 3, tp_bricks: {tp_bricks}}}")
    else:
        print("  No combos with dec>=20 found")

    # ── Step 7: Sensitivity ───────────────────────────────────────────────
    print("\n=== Step 7: Sensitivity (best TP, vary SL) ===")
    if results:
        best_tp = best['tp_atr']
        for r in results:
            if r['tp_atr'] == best_tp:
                print(f"  SL/ATR={r['sl_atr']:.1f}: EV={r['ev']:+.3f}R, WR={r['wr']:.1%}, dec={r['dec']}")

    print("\n=== Step 8: Sensitivity (best SL, vary TP) ===")
    if results:
        best_sl = best['sl_atr']
        for r in results:
            if r['sl_atr'] == best_sl:
                print(f"  TP/ATR={r['tp_atr']:.1f}: EV={r['ev']:+.3f}R, WR={r['wr']:.1%}, dec={r['dec']}")


if __name__ == "__main__":
    asyncio.run(main())
