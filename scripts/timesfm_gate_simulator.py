#!/usr/bin/env python3
"""
TimesFM Gate Threshold Simulator.

Subscribes to "signal.debug.noble_trader", collects N signals, then
simulates what the qualification rate would be under different gate
configurations:

  1. Hard gate at various MIN_TIMESFM_CONFIDENCE thresholds (0.01-0.10)
  2. Soft gate (reduce TimesFM blend weight proportionally to confidence)
  3. No gate (rely entirely on the blended EV)

This helps you choose the right gate configuration without redeploying.
The simulator does NOT affect production — it only reads debug signals
and computes what WOULD have happened.

Usage:
  export REDIS_URL=rediss://:token@host:6379
  python3 scripts/timesfm_gate_simulator.py --collect 50

  # Then see the simulation results for 50 collected signals.
  # Press Ctrl+C early to see results with whatever you've collected.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

REPO = "/home/z/my-project/noble-trader-fastapi-backend"
sys.path.insert(0, REPO)

# ── Log-odds helpers (must match ev_engine.py exactly) ────────────────────
_LOGIT_CLAMP = 1e-15

def _logit(p: float) -> float:
    p = max(_LOGIT_CLAMP, min(1.0 - _LOGIT_CLAMP, p))
    return math.log(p / (1.0 - p))

def _inv_logit(x: float) -> float:
    if x > 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)

# ── Default blend weights (must match ev_engine.py) ──────────────────────
DEFAULT_WEIGHTS_TIMESFM = {"p_regime": 0.40, "p_ext_signal": 0.25, "p_timesfm": 0.35}
DEFAULT_WEIGHTS_FALLBACK = {"p_regime": 0.55, "p_ext_signal": 0.45}

# ── Default gate thresholds (must match orchestrator.py) ─────────────────
DEFAULT_MIN_REGIME_CONF = 0.60
DEFAULT_MIN_KELLY = 0.02
DEFAULT_MIN_EV = 0.0
DEFAULT_MIN_RR = 1.5  # user lowered this from 2.0


def evaluate_hard_gate(data: dict, min_confidence: float, min_rr: float) -> bool:
    """Simulate qualification with a HARD TimesFM confidence gate.

    This is the current behavior: reject if |p_timesfm - 0.5| < min_confidence.
    """
    p_timesfm = data.get("p_timesfm")
    if p_timesfm is None:
        return False  # TimesFM unavailable = rejected

    confidence = abs(p_timesfm - 0.5)
    if confidence < min_confidence:
        return False

    # Check other gates
    if data.get("regime_conf", 0) < DEFAULT_MIN_REGIME_CONF:
        return False
    if data.get("kelly_f", 0) < DEFAULT_MIN_KELLY:
        return False
    if data.get("ev", 0) <= DEFAULT_MIN_EV:
        return False
    if data.get("rr", 0) < min_rr:
        return False

    return True


def evaluate_soft_gate(data: dict, threshold: float, min_rr: float) -> dict:
    """Simulate qualification with a SOFT TimesFM confidence gate.

    Instead of hard-rejecting when confidence is low, reduce TimesFM's
    blend weight proportionally:

        effective_weight = base_weight * min(1.0, confidence / threshold)

    So at confidence=threshold, full weight is used.
    At confidence=threshold/2, half weight is used.
    At confidence=0, TimesFM contributes nothing (like fallback mode).

    This preserves the blended EV philosophy: one uncertain source
    shouldn't veto the entire signal — it should just contribute less.

    Returns dict with qualification result + recomputed p_win + ev.
    """
    p_timesfm = data.get("p_timesfm")
    p_regime = data.get("p_regime", 0.5)  # from EV result
    p_ext_signal = data.get("p_ext_signal", 0.5)  # from EV result

    # We don't have p_regime/p_ext_signal in the debug payload, but we
    # have p_win (the blended result) and p_timesfm. We can back-compute
    # what p_win would be with a reduced TimesFM weight.
    #
    # Original: logit(p_win) = w_r * logit(p_regime) + w_e * logit(p_ext) + w_t * logit(p_timesfm)
    # We know: p_win (original), p_timesfm, and the weights.
    # So: w_r * logit(p_regime) + w_e * logit(p_ext) = logit(p_win) - w_t * logit(p_timesfm)
    #
    # With soft gate, new_timesfm_weight = w_t * min(1.0, conf/threshold)
    # The regime+ext contribution stays the same, so:
    #   new_logit = (original_logit - w_t * logit(p_timesfm)) + new_w_t * logit(p_timesfm)
    #             = original_logit + (new_w_t - w_t) * logit(p_timesfm)

    original_p_win = data.get("p_win", 0.5)
    original_logit = _logit(original_p_win)

    if p_timesfm is not None:
        confidence = abs(p_timesfm - 0.5)
        base_weight = DEFAULT_WEIGHTS_TIMESFM["p_timesfm"]
        scale = min(1.0, confidence / threshold) if threshold > 0 else 1.0
        new_timesfm_weight = base_weight * scale

        # Adjust the logit: remove old TimesFM contribution, add new one
        timesfm_logit = _logit(p_timesfm)
        adjusted_logit = original_logit + (new_timesfm_weight - base_weight) * timesfm_logit
        adjusted_p_win = _inv_logit(adjusted_logit)
    else:
        # TimesFM unavailable — soft gate treats this as weight=0 (same as fallback)
        adjusted_p_win = original_p_win

    # Recompute EV with adjusted p_win
    # EV = (p_win * profit) - ((1 - p_win) * stake)
    # profit = tp_bricks * brick_size, stake = sl_bricks * brick_size
    # We have rr = profit / stake, and stake = sl_bricks * brick_size
    sl_bricks = data.get("sl_bricks", 3)
    tp_bricks = data.get("tp_bricks", 5)
    brick_size = data.get("brick_size", 0.5)
    profit = tp_bricks * brick_size
    stake = sl_bricks * brick_size
    adjusted_ev = (adjusted_p_win * profit) - ((1.0 - adjusted_p_win) * stake)

    # Check gates — NO hard TimesFM confidence gate in soft mode
    qualified = True
    if data.get("regime_conf", 0) < DEFAULT_MIN_REGIME_CONF:
        qualified = False
    if data.get("kelly_f", 0) < DEFAULT_MIN_KELLY:
        qualified = False
    if adjusted_ev <= DEFAULT_MIN_EV:
        qualified = False
    if data.get("rr", 0) < min_rr:
        qualified = False

    return {
        "qualified": qualified,
        "adjusted_p_win": round(adjusted_p_win, 4),
        "adjusted_ev": round(adjusted_ev, 4),
        "timesfm_weight_scale": round(scale, 4) if p_timesfm is not None else None,
    }


def evaluate_no_gate(data: dict, min_rr: float) -> bool:
    """Simulate qualification with NO TimesFM gate at all.

    Rely entirely on the blended EV. If TimesFM is uncertain, the blend
    naturally pulls p_win toward 0.5, reducing EV. The EV gate handles it.
    """
    if data.get("regime_conf", 0) < DEFAULT_MIN_REGIME_CONF:
        return False
    if data.get("kelly_f", 0) < DEFAULT_MIN_KELLY:
        return False
    if data.get("ev", 0) <= DEFAULT_MIN_EV:
        return False
    if data.get("rr", 0) < min_rr:
        return False
    return True


def run_simulation(signals: list, min_rr: float):
    """Run all simulations and print results."""
    total = len(signals)
    if total == 0:
        print("No signals collected.")
        return

    print("\n" + "=" * 80)
    print(f"  Gate Simulation Results — {total} signals collected")
    print(f"  Other gates: MIN_REGIME_CONF={DEFAULT_MIN_REGIME_CONF}, "
          f"MIN_KELLY={DEFAULT_MIN_KELLY}, MIN_EV={DEFAULT_MIN_EV}, "
          f"MIN_RR={min_rr}")
    print("=" * 80)

    # ── Collect TimesFM confidence distribution ─────────────────────────
    confidences = []
    timesfm_available = 0
    for s in signals:
        p_t = s.get("p_timesfm")
        if p_t is not None:
            confidences.append(abs(p_t - 0.5))
            timesfm_available += 1

    print(f"\n  TimesFM availability: {timesfm_available}/{total} signals had TimesFM forecasts")
    if confidences:
        confidences.sort()
        print(f"  TimesFM confidence distribution:")
        print(f"    min:    {confidences[0]:.4f}")
        print(f"    25th:   {confidences[len(confidences)//4]:.4f}")
        print(f"    median: {confidences[len(confidences)//2]:.4f}")
        print(f"    75th:   {confidences[3*len(confidences)//4]:.4f}")
        print(f"    max:    {confidences[-1]:.4f}")

    # ── Simulation 1: Hard gate at various thresholds ──────────────────
    print(f"\n  ── HARD GATE (current behavior) ──────────────────────────")
    print(f"  {'Threshold':<12} {'Qualified':<12} {'Rate':<8} {'Change':<10}")
    print(f"  {'-'*12} {'-'*12} {'-'*8} {'-'*10}")

    baseline = None
    for threshold in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
        qualified = sum(1 for s in signals if evaluate_hard_gate(s, threshold, min_rr))
        rate = qualified / total * 100
        change = ""
        if baseline is None:
            baseline = qualified
        elif qualified != baseline:
            change = f"{'+' if qualified > baseline else ''}{qualified - baseline}"
        print(f"  {threshold:<12.2f} {qualified:<12} {rate:<8.1f}% {change}")

    # ── Simulation 2: Soft gate ────────────────────────────────────────
    print(f"\n  ── SOFT GATE (reduce TimesFM weight, don't reject) ───────")
    print(f"  {'Threshold':<12} {'Qualified':<12} {'Rate':<8} {'Avg Weight Scale':<18}")
    print(f"  {'-'*12} {'-'*12} {'-'*8} {'-'*18}")

    for threshold in [0.02, 0.03, 0.04, 0.05]:
        qualified_count = 0
        weight_scales = []
        for s in signals:
            result = evaluate_soft_gate(s, threshold, min_rr)
            if result["qualified"]:
                qualified_count += 1
            if result["timesfm_weight_scale"] is not None:
                weight_scales.append(result["timesfm_weight_scale"])
        rate = qualified_count / total * 100
        avg_scale = sum(weight_scales) / len(weight_scales) if weight_scales else 0
        print(f"  {threshold:<12.2f} {qualified_count:<12} {rate:<8.1f}% {avg_scale:<18.4f}")

    # ── Simulation 3: No gate ──────────────────────────────────────────
    print(f"\n  ── NO GATE (rely entirely on blended EV) ─────────────────")
    qualified = sum(1 for s in signals if evaluate_no_gate(s, min_rr))
    rate = qualified / total * 100
    print(f"  {'—':<12} {qualified:<12} {rate:<8.1f}%")

    # ── Show sample signals that would flip ────────────────────────────
    print(f"\n  ── SAMPLE SIGNALS THAT FLIP BETWEEN MODES ────────────────")
    print(f"  {'Symbol':<8} {'p_timesfm':<12} {'conf':<10} {'Hard@0.05':<12} {'Soft@0.05':<12} {'No Gate':<10}")
    print(f"  {'-'*8} {'-'*12} {'-'*10} {'-'*12} {'-'*12} {'-'*10}")

    shown = 0
    for s in signals:
        p_t = s.get("p_timesfm")
        if p_t is None:
            continue
        conf = abs(p_t - 0.5)
        hard = evaluate_hard_gate(s, 0.05, min_rr)
        soft = evaluate_soft_gate(s, 0.05, min_rr)["qualified"]
        none = evaluate_no_gate(s, min_rr)

        # Show signals where the modes disagree
        if hard != soft or soft != none:
            print(f"  {s.get('symbol','?'):<8} {p_t:<12.4f} {conf:<10.4f} "
                  f"{'✓' if hard else '✗':<12} {'✓' if soft else '✗':<12} {'✓' if none else '✗':<10}")
            shown += 1
            if shown >= 15:
                print(f"  ... ({total - 15} more)")
                break

    if shown == 0:
        print(f"  (no signals flipped between modes — all agree)")

    print("\n" + "=" * 80)
    print("  Recommendations:")
    print("  • If hard gate at 0.05 blocks everything but 0.03 looks reasonable,")
    print("    set MIN_TIMESFM_CONFIDENCE=0.03")
    print("  • If you want to honor the blended EV philosophy, use soft gate:")
    print("    set TIMESFM_GATE_MODE=soft")
    print("  • If you want no TimesFM gate at all (pure EV blend):")
    print("    set TIMESFM_GATE_MODE=off")
    print("=" * 80)


async def main():
    parser = argparse.ArgumentParser(description="TimesFM Gate Threshold Simulator")
    parser.add_argument("--collect", type=int, default=50,
                        help="Number of signals to collect before simulating (default: 50)")
    parser.add_argument("--min-rr", type=float, default=1.5,
                        help="Min RR for simulation (default: 1.5, matching your current setting)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Max seconds to wait for signals (default: 600)")
    args = parser.parse_args()

    from regime_platform.core.settings import settings
    import redis.asyncio as aioredis

    redis_url = settings.redis_url_effective
    if not redis_url:
        print("ERROR: Set REDIS_URL or UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN")
        sys.exit(1)

    print("=" * 80)
    print(f"  TimesFM Gate Threshold Simulator")
    print(f"  Collecting {args.collect} signals (timeout: {args.timeout}s)...")
    print(f"  Make sure DEBUG_SIGNALS=1 is set on the orchestrator!")
    print("=" * 80)
    print()

    client = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe("signal.debug.noble_trader")

    signals = []
    start_time = time.time()

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
            except json.JSONDecodeError:
                continue

            signals.append(data)
            symbol = data.get("symbol", "?")
            ts = datetime.fromtimestamp(data.get("ts", 0) / 1000).strftime("%H:%M:%S")
            p_t = data.get("p_timesfm")
            conf = abs(p_t - 0.5) if p_t is not None else None
            print(f"  [{ts}] {symbol:8} collected ({len(signals)}/{args.collect})  "
                  f"p_timesfm={p_t}  conf={f'{conf:.4f}' if conf is not None else 'N/A'}")

            if len(signals) >= args.collect:
                break

            if time.time() - start_time > args.timeout:
                print(f"\n  Timeout reached — simulating with {len(signals)} signals")
                break

    except KeyboardInterrupt:
        print(f"\n  Interrupted — simulating with {len(signals)} signals")

    finally:
        await pubsub.unsubscribe("signal.debug.noble_trader")
        await client.aclose()

    # Run the simulation
    if signals:
        run_simulation(signals, args.min_rr)
    else:
        print("\nNo signals collected. Make sure:")
        print("  1. DEBUG_SIGNALS=1 is set on the orchestrator")
        print("  2. The orchestrator is running and sweeping symbols")
        print("  3. Redis is reachable")


if __name__ == "__main__":
    asyncio.run(main())
