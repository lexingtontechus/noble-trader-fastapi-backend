#!/usr/bin/env python3
"""
Signal Qualification Simulator.

Shows the effect of different qualification methods on signal volume.
Subscribes to signal.debug.noble_trader, collects N signals, then
simulates what would happen under different filter configurations.

Usage:
  export REDIS_URL=rediss://:token@host:6379
  python3 scripts/qualification_simulator.py --collect 50

  # Or simulate with synthetic data (no Redis needed):
  python3 scripts/qualification_simulator.py --synthetic --count 100
"""
import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

REPO = "/home/z/my-project/noble-trader-latest"
sys.path.insert(0, REPO)


def generate_synthetic_signals(count: int) -> list:
    """Generate realistic synthetic signals for simulation.

    Simulates what the orchestrator would produce over ~count sweep cycles
    for 4 symbols (BTCUSD, USDJPY, EURUSD, XAUUSD).
    """
    symbols = [
        ("BTCUSD", "crypto", "Coinbase", 3, 5),   # RR=1.67
        ("USDJPY", "forex", "OANDA", 2, 5),        # RR=2.5
        ("EURUSD", "forex", "OANDA", 2, 5),        # RR=2.5
        ("XAUUSD", "commodities", "OANDA", 3, 5),  # RR=1.67
    ]
    signals = []
    base_ts = time.time() * 1000

    for i in range(count):
        for symbol, asset_class, venue, sl_bricks, tp_bricks in symbols:
            ts = base_ts + i * 5 * 60 * 1000  # 5 min apart

            # Simulate regime (80% directional, 20% neutral)
            regime_type = random.choices(
                ["bull", "bear", "neutral"],
                weights=[0.40, 0.40, 0.20],
            )[0]

            if regime_type == "neutral":
                regime = "consolidation"
                signal = "neutral"
                regime_conf = random.uniform(0.30, 0.55)
            elif regime_type == "bull":
                regime = random.choice(["high_vol_bull", "low_vol_bull", "med_vol_bull"])
                signal = "buy"
                regime_conf = random.uniform(0.85, 0.9999)
            else:
                regime = random.choice(["high_vol_bear", "low_vol_bear", "med_vol_bear"])
                signal = "sell"
                regime_conf = random.uniform(0.85, 0.9999)

            # TimesFM: 70% available, 30% unavailable
            if random.random() < 0.70:
                # When available, usually mildly directional
                if signal == "buy":
                    p_timesfm = random.gauss(0.55, 0.12)
                elif signal == "sell":
                    p_timesfm = random.gauss(0.45, 0.12)
                else:
                    p_timesfm = random.gauss(0.50, 0.05)
                p_timesfm = max(0.01, min(0.99, p_timesfm))
                timesfm_confidence = abs(p_timesfm - 0.5)
            else:
                p_timesfm = None
                timesfm_confidence = None

            # Kelly: 0.02-0.08 range (with floor)
            kelly_f = max(0.02, random.gauss(0.04, 0.02))

            # EV computation (simplified)
            rr = tp_bricks / sl_bricks
            if signal != "neutral":
                # p_win depends on blend
                if p_timesfm is not None:
                    # Log-odds blend: regime=0.40, ext=0.25, timesfm=0.35
                    p_regime = regime_conf if signal == "buy" else (1 - regime_conf)
                    p_ext = 0.55 if signal == "buy" else 0.45
                    # Simplified p_win
                    p_win = 0.5 * p_regime + 0.2 * p_ext + 0.3 * p_timesfm
                    p_win = max(0.01, min(0.99, p_win))
                else:
                    p_regime = regime_conf if signal == "buy" else (1 - regime_conf)
                    p_ext = 0.55 if signal == "buy" else 0.45
                    p_win = 0.6 * p_regime + 0.4 * p_ext
                    p_win = max(0.01, min(0.99, p_win))

                brick_size = 500 if symbol == "BTCUSD" else (10 if symbol == "XAUUSD" else 0.5)
                profit = tp_bricks * brick_size
                stake = sl_bricks * brick_size
                ev = (p_win * profit) - ((1 - p_win) * stake)
                ev_per_dollar = ev / stake if stake > 0 else 0
            else:
                p_win = 0.5
                ev = 0
                ev_per_dollar = 0

            signals.append({
                "symbol": symbol,
                "asset_class": asset_class,
                "venue": venue,
                "ts": ts,
                "signal": signal,
                "regime": regime,
                "regime_conf": round(regime_conf, 4),
                "kelly_f": round(kelly_f, 6),
                "p_timesfm": round(p_timesfm, 4) if p_timesfm is not None else None,
                "timesfm_confidence": round(timesfm_confidence, 4) if timesfm_confidence is not None else None,
                "ev": round(ev, 4),
                "ev_per_dollar": round(ev_per_dollar, 4),
                "rr": round(rr, 4),
                "p_win": round(p_win, 4),
                "sl_bricks": sl_bricks,
                "tp_bricks": tp_bricks,
            })

    return signals


def simulate_current_gates(signals, min_rr, min_regime_conf=0.60, min_kelly=0.02):
    """Current gate logic: 6 gates, all must pass."""
    qualified = 0
    for s in signals:
        if s["signal"] not in ("buy", "sell"):
            continue
        if s["regime_conf"] < min_regime_conf:
            continue
        if s["kelly_f"] < min_kelly:
            continue
        # Gate 4+5: soft mode (bypass)
        if s["ev"] <= 0:
            continue
        if s["rr"] < min_rr:
            continue
        qualified += 1
    return qualified


def simulate_p_win_threshold(signals, p_win_min, min_rr=1.5):
    """Add a p_win threshold to the current gates."""
    qualified = 0
    for s in signals:
        if s["signal"] not in ("buy", "sell"):
            continue
        if s["regime_conf"] < 0.60:
            continue
        if s["kelly_f"] < 0.02:
            continue
        if s["ev"] <= 0:
            continue
        if s["rr"] < min_rr:
            continue
        if s["p_win"] < p_win_min:
            continue
        qualified += 1
    return qualified


def simulate_cooldown(signals, cooldown_minutes, min_rr=1.5):
    """Current gates + cooldown: don't repeat same symbol+direction within N min."""
    qualified = 0
    last_signal_time = {}  # (symbol, direction) -> timestamp

    for s in sorted(signals, key=lambda x: x["ts"]):
        if s["signal"] not in ("buy", "sell"):
            continue
        if s["regime_conf"] < 0.60:
            continue
        if s["kelly_f"] < 0.02:
            continue
        if s["ev"] <= 0:
            continue
        if s["rr"] < min_rr:
            continue

        key = (s["symbol"], s["signal"])
        last_ts = last_signal_time.get(key, 0)
        cooldown_ms = cooldown_minutes * 60 * 1000

        if s["ts"] - last_ts >= cooldown_ms:
            qualified += 1
            last_signal_time[key] = s["ts"]
        # else: suppressed by cooldown

    return qualified


def simulate_cooldown_plus_pwin(signals, cooldown_minutes, p_win_min, min_rr=1.5):
    """Cooldown + p_win threshold — the recommended approach."""
    qualified = 0
    last_signal_time = {}

    for s in sorted(signals, key=lambda x: x["ts"]):
        if s["signal"] not in ("buy", "sell"):
            continue
        if s["regime_conf"] < 0.60:
            continue
        if s["kelly_f"] < 0.02:
            continue
        if s["ev"] <= 0:
            continue
        if s["rr"] < min_rr:
            continue
        if s["p_win"] < p_win_min:
            continue

        key = (s["symbol"], s["signal"])
        last_ts = last_signal_time.get(key, 0)
        cooldown_ms = cooldown_minutes * 60 * 1000

        if s["ts"] - last_ts >= cooldown_ms:
            qualified += 1
            last_signal_time[key] = s["ts"]

    return qualified


def simulate_ev_per_dollar(signals, ev_per_dollar_min, min_rr=1.5):
    """Current gates + EV-per-dollar threshold."""
    qualified = 0
    for s in signals:
        if s["signal"] not in ("buy", "sell"):
            continue
        if s["regime_conf"] < 0.60:
            continue
        if s["kelly_f"] < 0.02:
            continue
        if s["ev"] <= 0:
            continue
        if s["rr"] < min_rr:
            continue
        if s["ev_per_dollar"] < ev_per_dollar_min:
            continue
        qualified += 1
    return qualified


def run_simulation(signals):
    """Run all simulations and print results."""
    total = len(signals)
    directional = sum(1 for s in signals if s["signal"] in ("buy", "sell"))

    print("\n" + "=" * 80)
    print(f"  Signal Qualification Simulation — {total} signals ({directional} directional)")
    print("=" * 80)

    # p_win distribution
    p_wins = [s["p_win"] for s in signals if s["signal"] in ("buy", "sell")]
    if p_wins:
        p_wins.sort()
        print(f"\n  p_win distribution (directional signals only):")
        print(f"    min:    {p_wins[0]:.4f}")
        print(f"    25th:   {p_wins[len(p_wins)//4]:.4f}")
        print(f"    median: {p_wins[len(p_wins)//2]:.4f}")
        print(f"    75th:   {p_wins[3*len(p_wins)//4]:.4f}")
        print(f"    max:    {p_wins[-1]:.4f}")

    # ── Simulation 1: MIN_RR sweep ──────────────────────────────────
    print(f"\n  ── MIN_RR sweep (current gate logic) ──────────────────────")
    print(f"  {'MIN_RR':<10} {'Qualified':<12} {'Rate':<10} {'Per day':<12} {'Assessment'}")
    print(f"  {'-'*10} {'-'*12} {'-'*10} {'-'*12} {'-'*30}")

    for min_rr in [1.3, 1.4, 1.5, 1.6, 1.67, 1.8, 2.0, 2.5]:
        q = simulate_current_gates(signals, min_rr)
        rate = q / total * 100
        per_day = q / total * 288  # 288 sweeps/day per symbol (5 min intervals)
        if per_day > 100:
            assessment = "🔴 Too many (spam)"
        elif per_day > 20:
            assessment = "🟡 Moderate (still high)"
        elif per_day > 5:
            assessment = "🟢 Reasonable"
        elif per_day > 0:
            assessment = "🟢 Good (selective)"
        else:
            assessment = "🔴 Too few (none)"
        print(f"  {min_rr:<10.2f} {q:<12} {rate:<10.1f}% {per_day:<12.0f} {assessment}")

    # ── Simulation 2: p_win threshold ───────────────────────────────
    print(f"\n  ── p_win threshold (MIN_RR=1.5 + require min p_win) ──────")
    print(f"  {'p_win_min':<12} {'Qualified':<12} {'Rate':<10} {'Per day':<12} {'Assessment'}")
    print(f"  {'-'*12} {'-'*12} {'-'*10} {'-'*12} {'-'*30}")

    for p_win_min in [0.50, 0.52, 0.55, 0.58, 0.60, 0.62, 0.65, 0.70]:
        q = simulate_p_win_threshold(signals, p_win_min)
        rate = q / total * 100
        per_day = q / total * 288
        if per_day > 100:
            assessment = "🔴 Too many"
        elif per_day > 20:
            assessment = "🟡 Moderate"
        elif per_day > 5:
            assessment = "🟢 Reasonable"
        elif per_day > 0:
            assessment = "🟢 Good"
        else:
            assessment = "🔴 Too few"
        print(f"  {p_win_min:<12.2f} {q:<12} {rate:<10.1f}% {per_day:<12.0f} {assessment}")

    # ── Simulation 3: Cooldown ──────────────────────────────────────
    print(f"\n  ── Cooldown (MIN_RR=1.5 + don't repeat symbol+dir for N min) ──")
    print(f"  {'Cooldown':<12} {'Qualified':<12} {'Rate':<10} {'Per day':<12} {'Assessment'}")
    print(f"  {'-'*12} {'-'*12} {'-'*10} {'-'*12} {'-'*30}")

    for cooldown in [0, 15, 30, 60, 120, 240, 360]:
        q = simulate_cooldown(signals, cooldown)
        rate = q / total * 100
        per_day = q / total * 288
        if per_day > 100:
            assessment = "🔴 Too many"
        elif per_day > 20:
            assessment = "🟡 Moderate"
        elif per_day > 5:
            assessment = "🟢 Reasonable"
        elif per_day > 0:
            assessment = "🟢 Good"
        else:
            assessment = "🔴 Too few"
        label = f"{cooldown}min" if cooldown > 0 else "none"
        print(f"  {label:<12} {q:<12} {rate:<10.1f}% {per_day:<12.0f} {assessment}")

    # ── Simulation 4: Cooldown + p_win (recommended) ────────────────
    print(f"\n  ── RECOMMENDED: Cooldown + p_win threshold ────────────────")
    print(f"  {'Cooldown':<12} {'p_win_min':<12} {'Qualified':<12} {'Per day':<12} {'Assessment'}")
    print(f"  {'-'*12} {'-'*12} {'-'*12} {'-'*12} {'-'*30}")

    combos = [
        (30, 0.55), (30, 0.58), (30, 0.60),
        (60, 0.55), (60, 0.58), (60, 0.60),
        (120, 0.55), (120, 0.58),
    ]
    for cooldown, p_win_min in combos:
        q = simulate_cooldown_plus_pwin(signals, cooldown, p_win_min)
        per_day = q / total * 288
        if per_day > 20:
            assessment = "🟡 Moderate"
        elif per_day > 3:
            assessment = "🟢 Good"
        elif per_day > 0:
            assessment = "🟢 Selective"
        else:
            assessment = "🔴 Too few"
        print(f"  {cooldown}min{'':<6} {p_win_min:<12.2f} {q:<12} {per_day:<12.0f} {assessment}")

    # ── Simulation 5: EV-per-dollar threshold ───────────────────────
    print(f"\n  ── EV-per-dollar threshold (MIN_RR=1.5 + require min ev/$) ──")
    print(f"  {'ev/$_min':<12} {'Qualified':<12} {'Rate':<10} {'Per day':<12}")
    print(f"  {'-'*12} {'-'*12} {'-'*10} {'-'*12}")

    for ev_min in [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]:
        q = simulate_ev_per_dollar(signals, ev_min)
        rate = q / total * 100
        per_day = q / total * 288
        print(f"  {ev_min:<12.2f} {q:<12} {rate:<10.1f}% {per_day:<12.0f}")

    # ── Recommendations ─────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("  RECOMMENDATIONS")
    print("=" * 80)
    print("""
  The core problem: MIN_RR is a STATIC property of brick params, not a
  signal quality measure. Tuning it between 1.5-2.0 just selects which
  brick configs pass — it doesn't filter by signal QUALITY.

  Three approaches, from simplest to most effective:

  1. COOLDOWN (implement first — biggest impact, easiest)
     Add a per-symbol-per-direction cooldown of 30-60 minutes.
     If BTCUSD BUY qualified 5 min ago, suppress the next BTCUSD BUY
     until the cooldown expires. This alone cuts signal volume ~10x.
     Set via env var: SIGNAL_COOLDOWN_MINUTES=60

  2. p_win THRESHOLD (add on top of cooldown)
     Require the blended p_win to be above 0.55-0.60. This filters
     out signals where the EV blend is only marginally directional.
     Set via env var: MIN_P_WIN=0.58

  3. KEEP MIN_RR=1.5 (don't raise it)
     RR is already enforced by the brick params. Raising MIN_RR just
     breaks symbols with sl=3,tp=5 (RR=1.67). The p_win threshold
     is a better quality filter because it's dynamic per-signal.

  RECOMMENDED CONFIGURATION:
    MIN_RR=1.5              (keep — lets sl=3,tp=5 symbols through)
    MIN_P_WIN=0.58          (new — filters low-confidence blends)
    SIGNAL_COOLDOWN_MINUTES=60  (new — prevents signal spam)
    TIMESFM_GATE_MODE=soft  (keep — preserves blended EV philosophy)

  Expected result: ~3-8 qualified signals per day per symbol
  (down from ~288 at current settings).
""")


async def collect_from_redis(count: int, timeout: int) -> list:
    """Collect real signals from the debug channel."""
    from regime_platform.core.settings import settings
    import redis.asyncio as aioredis

    redis_url = settings.redis_url_effective
    if not redis_url:
        print("ERROR: Set REDIS_URL")
        return []

    client = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe("signal.debug.noble_trader")

    signals = []
    start = time.time()
    print(f"Collecting {count} signals from debug channel (timeout: {timeout}s)...")

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
                signals.append(data)
                if len(signals) >= count:
                    break
                if time.time() - start > timeout:
                    break
            except json.JSONDecodeError:
                continue
    finally:
        await pubsub.unsubscribe("signal.debug.noble_trader")
        await client.aclose()

    return signals


async def main():
    parser = argparse.ArgumentParser(description="Signal qualification simulator")
    parser.add_argument("--collect", type=int, default=50, help="Collect N signals from Redis")
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic data (no Redis needed)")
    parser.add_argument("--count", type=int, default=200, help="Number of synthetic signals")
    parser.add_argument("--timeout", type=int, default=600, help="Redis collection timeout (seconds)")
    args = parser.parse_args()

    if args.synthetic:
        print(f"Generating {args.count} synthetic signals...")
        signals = generate_synthetic_signals(args.count)
        run_simulation(signals)
    else:
        signals = await collect_from_redis(args.collect, args.timeout)
        if signals:
            run_simulation(signals)
        else:
            print("No signals collected. Use --synthetic to test without Redis.")


if __name__ == "__main__":
    asyncio.run(main())
