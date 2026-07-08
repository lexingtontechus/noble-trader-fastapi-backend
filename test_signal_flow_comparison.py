#!/usr/bin/env python3
"""
Signal Generation Flow Comparison: Current vs. Optimized
═══════════════════════════════════════════════════════

Demonstrates the difference between:
  1. CURRENT  — sweep_orchestrator publishes every heartbeat (no qualification)
  2. OPTIMIZED — requires TimesFM certainty + EV allowed + min regime_conf + min kelly

Validates that the orchestrator's Redis publish targets:
  - agent:{symbol}                (liveness heartbeat)
  - signal.raw.noble_trader       (L0 aggregate channel)

Run: python test_signal_flow_comparison.py
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ─── Logit helpers (mirrors ev_engine.py) ───────────────────────────

_CLAMP = 1e-15


def _logit(p: float) -> float:
    p = max(_CLAMP, min(1.0 - _CLAMP, p))
    return math.log(p / (1.0 - p))


def _inv_logit(x: float) -> float:
    if x > 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


# ─── EV Engine (faithful port of ev_engine.compute_ev) ─────────────

W_TIMESFM = {"p_regime": 0.40, "p_ext_signal": 0.25, "p_timesfm": 0.35}
W_FALLBACK = {"p_regime": 0.55, "p_ext_signal": 0.45}


def compute_p_regime(trend_probs: dict, direction: str) -> float:
    if not trend_probs:
        return 0.50
    bull = sum(v for k, v in trend_probs.items() if k in ("bull", "strong_bull"))
    bear = sum(v for k, v in trend_probs.items() if k in ("bear", "strong_bear"))
    neutral = sum(v for k, v in trend_probs.items() if k == "neutral")
    if direction == "long":
        return bull + 0.5 * neutral
    if direction == "short":
        return bear + 0.5 * neutral
    return 0.50


def compute_ev(
    trend_probs: dict,
    direction: str,
    p_timesfm: Optional[float],
    brick_size: float,
    sl_bricks: int,
    tp_bricks: int,
    min_ev: float = 0.0,
    min_rr: float = 2.0,
) -> dict:
    p_regime = compute_p_regime(trend_probs, direction)
    p_ext = 0.50  # no external signal in light sweep
    p_markov = 0.5

    if p_timesfm is not None:
        w = W_TIMESFM
        logit_sum = (
            w["p_regime"] * _logit(p_regime)
            + w["p_ext_signal"] * _logit(p_ext)
            + w["p_timesfm"] * _logit(p_timesfm)
        )
        p_win = _inv_logit(logit_sum)
    else:
        w = W_FALLBACK
        logit_sum = (
            w["p_regime"] * _logit(p_regime)
            + w["p_ext_signal"] * _logit(p_ext)
        )
        p_win = _inv_logit(logit_sum)

    profit = tp_bricks * brick_size
    stake = sl_bricks * brick_size
    ev = (p_win * profit) - ((1.0 - p_win) * stake)
    ev_per_dollar = ev / stake if stake > 0 else 0.0
    rr = profit / stake if stake > 0 else 0.0

    allowed = (ev > min_ev) and (rr >= min_rr)

    return {
        "p_win": round(p_win, 4),
        "p_regime": round(p_regime, 4),
        "p_timesfm": round(p_timesfm if p_timesfm is not None else 0.0, 4),
        "ev": round(ev, 4),
        "ev_per_dollar": round(ev_per_dollar, 4),
        "rr": round(rr, 4),
        "allowed": allowed,
    }


# ─── Trade signal derivation (port of orchestrator._compute_trade_signal) ─

def compute_trade_signal(regime_label: str, price: float, brick: float, sl: int, tp: int) -> dict:
    lower = regime_label.lower()
    if "bull" in lower:
        return {"signal": "buy", "entry_price": price,
                "stop_loss": price - brick * sl, "take_profit": price + brick * tp}
    if "bear" in lower:
        return {"signal": "sell", "entry_price": price,
                "stop_loss": price + brick * sl, "take_profit": price - brick * tp}
    return {"signal": "neutral", "entry_price": price,
            "stop_loss": price - brick * sl, "take_profit": price + brick * tp}


# ─── Test scenarios ─────────────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    regime_label: str
    regime_conf: float
    trend_probs: dict
    kelly_f: float
    p_timesfm: Optional[float]
    price: float = 42000.0
    brick: float = 0.5
    sl: int = 3
    tp: int = 5


SCENARIOS = [
    Scenario(
        name="strong_bull_confident_timesfm",
        regime_label="high_vol_bull",
        regime_conf=0.88,
        trend_probs={"strong_bull": 0.55, "bull": 0.25, "bear": 0.15, "strong_bear": 0.05},
        kelly_f=0.12,
        p_timesfm=0.72,  # confident up — RR=2.0 so EV should allow
        sl=3, tp=6,      # RR = 2.0
    ),
    Scenario(
        name="bull_uncertain_timesfm",
        regime_label="low_vol_bull",
        regime_conf=0.62,
        trend_probs={"bull": 0.50, "neutral": 0.30, "bear": 0.20},
        kelly_f=0.04,
        p_timesfm=0.52,  # basically a coin flip → certainty gate fails
        sl=2, tp=5,      # RR = 2.5
    ),
    Scenario(
        name="neutral_no_edge",
        regime_label="range",
        regime_conf=0.45,
        trend_probs={"neutral": 0.70, "bull": 0.15, "bear": 0.15},
        kelly_f=0.0,
        p_timesfm=0.50,  # no view
        sl=3, tp=6,
    ),
    Scenario(
        name="bear_confident_timesfm_down",
        regime_label="high_vol_bear",
        regime_conf=0.81,
        trend_probs={"strong_bear": 0.50, "bear": 0.30, "bull": 0.15, "strong_bull": 0.05},
        kelly_f=0.09,
        p_timesfm=0.28,  # confident down → should qualify
        sl=3, tp=6,
    ),
    Scenario(
        name="no_timesfm_fallback",
        regime_label="low_vol_bull",
        regime_conf=0.75,
        trend_probs={"bull": 0.60, "neutral": 0.25, "bear": 0.15},
        kelly_f=0.08,
        p_timesfm=None,  # orchestrator default — no TimesFM wired in
        sl=3, tp=6,
    ),
    Scenario(
        name="very_confident_high_tier",
        regime_label="low_vol_bull",
        regime_conf=0.91,
        trend_probs={"strong_bull": 0.65, "bull": 0.25, "neutral": 0.08, "bear": 0.02},
        kelly_f=0.15,
        p_timesfm=0.82,  # very confident → qualifies for high-conf tier
        sl=3, tp=6,
    ),
]


# ─── Current flow (no qualification gates) ─────────────────────────

def current_flow(s: Scenario) -> dict:
    """Mirrors orchestrator._process_symbol: always publishes."""
    ts = compute_trade_signal(s.regime_label, s.price, s.brick, s.sl, s.tp)
    direction = "short" if "bear" in s.regime_label.lower() else "long"
    ev = compute_ev(s.trend_probs, direction, s.p_timesfm, s.brick, s.sl, s.tp)
    # ev_scale = min(1.0, ev_per_dollar / 0.50) — ported from ev_engine.compute_ev_scale
    ev_scale = min(1.0, ev["ev_per_dollar"] / 0.50) if ev["ev_per_dollar"] > 0 else 0.0
    effective_kelly = s.kelly_f * ev_scale

    payload = {
        "type": "heartbeat",
        "symbol": "TEST",
        "ts": int(time.time() * 1000),
        "regime": s.regime_label,
        "regime_conf": s.regime_conf,
        "signal": ts["signal"],
        "entry_price": ts["entry_price"],
        "stop_loss": ts["stop_loss"],
        "take_profit": ts["take_profit"],
        "kelly_f": s.kelly_f,
        "ev": ev["ev"],
        "p_win": ev["p_win"],
        "p_timesfm": ev["p_timesfm"],
        "aggression": "aggressive",  # placeholder
    }

    return {
        "published_to_raw": True,       # ⚠ always published
        "published_to_agent": True,     # ⚠ always published
        "ev_allowed_ignored": not ev["allowed"],  # gate exists but not checked
        "payload": payload,
        "ev": ev,
    }


# ─── Optimized flow (qualification gates) ──────────────────────────

def optimized_flow(s: Scenario, *,
                   min_regime_conf: float = 0.60,
                   min_confidence: float = 0.05,
                   min_kelly: float = 0.02) -> dict:
    """Mirror of current flow + qualification gates + size boost."""
    ts = compute_trade_signal(s.regime_label, s.price, s.brick, s.sl, s.tp)
    direction = "short" if "bear" in s.regime_label.lower() else "long"
    ev = compute_ev(s.trend_probs, direction, s.p_timesfm, s.brick, s.sl, s.tp)

    confidence = abs(s.p_timesfm - 0.5) if s.p_timesfm is not None else None

    # ── Gate evaluation ──
    gates = {}
    gates["signal_neutral"] = ts["signal"] == "neutral"
    gates["regime_conf_low"] = s.regime_conf < min_regime_conf
    gates["timesfm_uncertain"] = confidence is not None and confidence < min_confidence
    gates["timesfm_missing"] = s.p_timesfm is None  # treat None as a soft fail
    gates["ev_not_allowed"] = not ev["allowed"]
    gates["kelly_too_low"] = s.kelly_f < min_kelly

    blocked = any(gates.values())
    qualified = not blocked

    # ── Size enhancement (only matters if qualified) ──
    ev_scale = min(1.0, ev["ev_per_dollar"] / 0.50) if ev["ev_per_dollar"] > 0 else 0.0
    size_mult = 1.0
    if qualified and confidence is not None:
        if confidence >= 0.30:
            size_mult = 1.2
        elif confidence >= 0.15:
            size_mult = 1.0
        else:
            size_mult = 0.6

    effective_kelly = s.kelly_f * ev_scale * size_mult if qualified else 0.0

    payload = {
        "type": "heartbeat",
        "symbol": "TEST",
        "ts": int(time.time() * 1000),
        "regime": s.regime_label,
        "regime_conf": s.regime_conf,
        "signal": ts["signal"],
        "entry_price": ts["entry_price"],
        "stop_loss": ts["stop_loss"],
        "take_profit": ts["take_profit"],
        "kelly_f": round(effective_kelly, 6),
        "ev": ev["ev"],
        "p_win": ev["p_win"],
        "p_timesfm": ev["p_timesfm"],
        "timesfm_confidence": round(confidence, 4) if confidence is not None else None,
        "size_mult": size_mult,
        "qualified": qualified,
    }

    return {
        "published_to_agent": True,             # always (liveness)
        "published_to_raw": qualified,          # ⭐ gated
        "published_to_qualified_tier": (
            qualified and confidence is not None and confidence >= 0.30
        ),
        "qualified": qualified,
        "gates": gates,
        "size_mult": size_mult,
        "effective_kelly": round(effective_kelly, 6),
        "payload": payload,
        "ev": ev,
    }


# ─── Runner ─────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 78)
    print("SIGNAL GENERATION FLOW: CURRENT vs. OPTIMIZED")
    print("=" * 78)
    print()
    print("Validated publish targets (orchestrator.py:544-545):")
    print("  • agent:{symbol}               — liveness heartbeat (always)")
    print("  • signal.raw.noble_trader      — L0 aggregate channel")
    print()

    current_count = 0
    optimized_count = 0
    optimized_tier_count = 0
    results = []

    for s in SCENARIOS:
        print("─" * 78)
        print(f"SCENARIO: {s.name}")
        print(f"  regime={s.regime_label} (conf={s.regime_conf}), kelly_f={s.kelly_f}, "
              f"p_timesfm={s.p_timesfm}")
        print()

        cur = current_flow(s)
        opt = optimized_flow(s)

        print(f"  CURRENT:   published_to_raw={cur['published_to_raw']}  "
              f"(ev.allowed={cur['ev']['allowed']}, but IGNORED)")
        print(f"             → raw channel always receives heartbeat "
              f"(signal='{cur['payload']['signal']}')")
        print()
        print(f"  OPTIMIZED: qualified={opt['qualified']}  "
              f"published_to_raw={opt['published_to_raw']}  "
              f"qualified_tier={opt['published_to_qualified_tier']}")
        if not opt["qualified"]:
            failed = [k for k, v in opt["gates"].items() if v]
            print(f"             gates failed: {failed}")
        else:
            print(f"             size_mult={opt['size_mult']}  "
                  f"effective_kelly={opt['effective_kelly']}")
        print()

        if cur["published_to_raw"]:
            current_count += 1
        if opt["published_to_raw"]:
            optimized_count += 1
        if opt["published_to_qualified_tier"]:
            optimized_tier_count += 1

        results.append({
            "scenario": s.name,
            "current": {"published_to_raw": cur["published_to_raw"], "ev": cur["ev"]},
            "optimized": {
                "qualified": opt["qualified"],
                "published_to_raw": opt["published_to_raw"],
                "published_to_qualified_tier": opt["published_to_qualified_tier"],
                "gates": opt["gates"],
                "size_mult": opt["size_mult"],
                "ev": opt["ev"],
            },
        })

    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Scenarios run:               {len(SCENARIOS)}")
    print(f"CURRENT raw publishes:       {current_count}/{len(SCENARIOS)}  "
          f"(100% — publishes every cycle)")
    print(f"OPTIMIZED raw publishes:     {optimized_count}/{len(SCENARIOS)}  "
          f"(gated — only qualified)")
    print(f"OPTIMIZED high-conf tier:    {optimized_tier_count}/{len(SCENARIOS)}  "
          f"(confidence ≥ 0.30)")
    print()
    print("Key insight:")
    print("  CURRENT floods signal.raw.noble_trader with neutral/low-EV heartbeats.")
    print("  OPTIMIZED keeps agent:{symbol} for liveness, gates the raw channel to")
    print("  qualified-only, and adds signal.qualified.noble_trader for high-confidence.")
    print()
    print("Certainty vs. uncertainty (you were right):")
    print("  DEPRECATED supabase gate:  skip if |p-0.5| > 0.10  (require UNCERTAINTY)")
    print("  OPTIMIZED gate:            skip if |p-0.5| < 0.05  (require CERTAINTY)")
    print("  → A trade signal should fire when the model is confident, not when it's guessing.")

    with open("signal_flow_comparison_results.json", "w") as f:
        json.dump({
            "scenarios": results,
            "summary": {
                "current_raw_publishes": current_count,
                "optimized_raw_publishes": optimized_count,
                "optimized_high_conf_tier": optimized_tier_count,
                "total_scenarios": len(SCENARIOS),
            },
        }, f, indent=2)
    print()
    print("📄 Detailed results: signal_flow_comparison_results.json")


if __name__ == "__main__":
    main()
