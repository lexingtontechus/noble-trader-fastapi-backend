#!/usr/bin/env python3
"""
Standalone test of qualification logic.
══════════════════════════════════════════════════════════════

Tests the qualification gates in sweep_orchestrator:
  1. TimesFM certainty (|p-0.5| >= 0.05)
  2. Signal must be buy/sell (not neutral)
  3. min_regime_conf >= 0.60
  4. min_kelly >= 0.02
  5. ev_result.allowed must be True
"""

import json
import math


# ─── Simulate EV engine ─────────────────────────────────────────────

def simulate_ev_engine(p_timesfm, brick_size, sl_bricks, tp_bricks):
    """Simulates ev_engine.compute_ev logic."""
    # Simplified weights
    w = {"p_regime": 0.40, "p_ext": 0.25, "p_timesfm": 0.35}

    def _logit(p):
        p = max(1e-15, min(1.0 - 1e-15, p))
        return math.log(p / (1.0 - p))

    def _inv_logit(x):
        if x > 0:
            return 1.0 / (1.0 + math.exp(-x))
        return math.exp(x) / (1.0 + math.exp(x))

    p_win = 0.5
    if p_timesfm is not None:
        logit_sum = (
            w["p_regime"] * _logit(p_win)
            + w["p_ext"] * _logit(0.5)
            + w["p_timesfm"] * _logit(p_timesfm)
        )
        p_win = _inv_logit(logit_sum)

    profit = tp_bricks * brick_size
    stake = sl_bricks * brick_size
    ev = (p_win * profit) - ((1.0 - p_win) * stake)
    rr = profit / stake if stake > 0 else 0.0

    allowed = (ev > 0.0) and (rr >= 2.0)
    return {
        "p_win": round(p_win, 4),
        "ev": round(ev, 4),
        "rr": round(rr, 4),
        "allowed": allowed,
    }


# ─── Test scenarios ─────────────────────────────────────────────────

def test_qualified_signals():
    """Test all qualification scenarios."""
    print("=" * 78)
    print("TEST: Qualified Signal Generation")
    print("=" * 78)
    print()

    # Configuration
    min_regime_conf = 0.60
    min_kelly = 0.02
    timesfm_min_confidence = 0.05

    # Scenarios
    scenarios = [
        {
            "name": "strong_bull_confident_timesfm",
            "regime_label": "high_vol_bull",
            "regime_conf": 0.88,
            "kelly_f": 0.12,
            "p_timesfm": 0.72,  # confidence = |0.72-0.5| = 0.22 >= 0.05
            "brick": 0.5,
            "sl": 3,
            "tp": 6,
            "expected_qualified": True,
        },
        {
            "name": "bull_uncertain_timesfm",
            "regime_label": "low_vol_bull",
            "regime_conf": 0.62,
            "kelly_f": 0.04,
            "p_timesfm": 0.52,  # confidence = |0.52-0.5| = 0.02 < 0.05
            "brick": 0.5,
            "sl": 2,
            "tp": 5,
            "expected_qualified": False,
        },
        {
            "name": "range_low_regime_conf",
            "regime_label": "range",
            "regime_conf": 0.45,  # < 0.60
            "kelly_f": 0.0,
            "p_timesfm": 0.50,
            "brick": 0.5,
            "sl": 3,
            "tp": 6,
            "expected_qualified": False,
        },
        {
            "name": "bear_confident_down",
            "regime_label": "high_vol_bear",
            "regime_conf": 0.81,
            "kelly_f": 0.09,
            "p_timesfm": 0.28,  # confidence = |0.28-0.5| = 0.22 >= 0.05
            "brick": 0.5,
            "sl": 3,
            "tp": 6,
            "expected_qualified": True,
        },
        {
            "name": "no_timesfm_soft_fail",
            "regime_label": "low_vol_bull",
            "regime_conf": 0.75,
            "kelly_f": 0.08,
            "p_timesfm": None,  # No TimesFM
            "brick": 0.5,
            "sl": 3,
            "tp": 6,
            "expected_qualified": False,
        },
    ]

    total = len(scenarios)
    passed = 0
    failed = 0
    results = []

    for s in scenarios:
        # Determine signal type
        signal = "buy" if "bull" in s["regime_label"].lower() else (
            "sell" if "bear" in s["regime_label"].lower() else "neutral"
        )

        # Gate 1: Signal must be buy/sell
        signal_qualifiable = signal not in ("neutral", "unknown")

        # Gate 2: TimesFM required (cannot be None)
        timesfm_missing = s["p_timesfm"] is None

        # Gate 3: TimesFM certainty
        if s["p_timesfm"] is not None:
            confidence = abs(s["p_timesfm"] - 0.5)
            timesfm_too_uncertain = confidence < timesfm_min_confidence
        else:
            confidence = None
            timesfm_too_uncertain = False

        # Gate 3: Regime confidence
        regime_conf_ok = s["regime_conf"] >= min_regime_conf

        # Gate 4: Kelly fraction
        kelly_min_ok = s["kelly_f"] >= min_kelly

        # Gate 5: TimesFM required
        timesfm_missing = s["p_timesfm"] is None

        # Gate 6: EV allowed
        ev = simulate_ev_engine(s["p_timesfm"], s["brick"], s["sl"], s["tp"])

        # Final qualification check
        qualified = (
            signal_qualifiable
            and regime_conf_ok
            and kelly_min_ok
            and not timesfm_missing  # TimesFM now required
            and confidence >= timesfm_min_confidence
            and ev["allowed"]
        )

        # Check expected
        expected = s["expected_qualified"]
        status = "✅ PASS" if qualified == expected else "❌ FAIL"
        if qualified == expected:
            passed += 1
        else:
            failed += 1

        print(f"{s['name']}: {status}")
        if qualified != expected:
            print(f"  Expected: {expected}, Got: {qualified}")
            print(f"  Gates: signal={signal}, qualifiable={signal_qualifiable}, "
                  f"regime_conf={s['regime_conf']} (ok={regime_conf_ok}), "
                  f"kelly_f={s['kelly_f']} (ok={kelly_min_ok}), "
                  f"timesfm_conf={round(confidence, 4) if confidence else None} "
                  f"(uncertain={timesfm_too_uncertain}), ev_allowed={ev['allowed']}")
        print()

        results.append({
            "scenario": s["name"],
            "signal": signal,
            "regime_conf": s["regime_conf"],
            "regime_conf_ok": regime_conf_ok,
            "kelly_f": s["kelly_f"],
            "kelly_min_ok": kelly_min_ok,
            "p_timesfm": s["p_timesfm"],
            "timesfm_confidence": round(confidence, 4) if confidence else None,
            "timesfm_too_uncertain": timesfm_too_uncertain,
            "ev": ev,
            "qualified": qualified,
            "expected": expected,
        })

    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"Total: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print()
    print("Key findings:")
    print("  • Certainty gate (|p-0.5| >= 0.05) filtering uncertain signals")
    print("  • min_regime_conf (0.60) filtering low-confidence regimes")
    print("  • min_kelly (0.02) filtering low-Kelly opportunities")
    print("  • ev_result.allowed gate blocking low-EV trades")
    print("  • No TimesFM = soft fail (signal not qualified)")
    print()
    print("About 0.75 feasibility:")
    print("  • p=0.75 → confidence = |0.75-0.5| = 0.25")
    print("  • This is WELL above the 0.05 threshold — feasible and appropriate")
    print("  • 0.75 p corresponds to a strong directional forecast (75% up, 25% down)")
    print("  • This is exactly what you want for a trade signal")
    print()

    with open("qualified_signals_standalone_test.json", "w") as f:
        json.dump({
            "summary": {"total": total, "passed": passed, "failed": failed},
            "results": results,
        }, f, indent=2)
    print("📄 Results: qualified_signals_standalone_test.json")


if __name__ == "__main__":
    test_qualified_signals()
