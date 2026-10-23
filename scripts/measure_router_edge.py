#!/usr/bin/env python3
"""measure_router_edge.py — Router Edge Model skill report (R2, 2026-10-23).

The TSFM-vs-router competition, measured from the live param_store cells.
Every light-sweep tick stamps a next-candle prediction for BOTH the router
edge model (p_router = P(up | symbol, archetype, direction)) and TimesFM's
p_timesfm; the identical outcome stream (close-vs-close on the first newer
bar) updates decay-weighted cells and BOTH skill accumulators. This script
renders that store:

    per cell        n_eff, posterior p_up, raw counts, acc, skill = acc - majority
    router_skill    aggregate directional-call accuracy of p_router
    timesfm_skill   aggregate directional-call accuracy of p_timesfm (SAME outcomes)

skill uses the W-X2 convention from regime_edge/hmm_ref.py (acc minus the
symbol base rate's majority side) — the same measurement that proved the HMM's
negative skill 14/14, now pointed at the two next-candle sources.

Usage:
    python -m scripts.measure_router_edge                 # all symbols
    python -m scripts.measure_router_edge --symbol XAUUSD # one symbol
    python -m scripts.measure_router_edge --json          # machine-readable

Decision gates (docs/scopes/20261023_TSFM_VS_REASONING_LLM_AND_ROUTER_EDGE.md):
    promote router_edge_mode shadow -> live:
        cell n_eff >= router_edge_min_n_eff AND router_skill > timesfm_skill
    demote timesfm_blend_mode live -> shadow/off:
        timesfm_skill < 0 on meaningful n_eff (the never-measured 50% blend
        weight finally has its number; a negative-skill source should not
        hold half the directional blend)
"""

from __future__ import annotations

import argparse
import json
import sys

from regime_platform.sweep_orchestrator.router_edge import (
    format_report,
    router_edge_report,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", default=None, help="restrict to one symbol")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit the raw report dict as JSON")
    args = ap.parse_args()

    report = router_edge_report(args.symbol)
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(format_report(report))

    # Verdict banner: the two decision gates, stated against the numbers.
    syms = report.get("symbols") or {}
    r_n = sum((s.get("router_skill") or {}).get("n_eff") or 0.0 for s in syms.values())
    t_n = sum((s.get("timesfm_skill") or {}).get("n_eff") or 0.0 for s in syms.values())
    r_acc = [ (s.get("router_skill") or {}).get("acc") for s in syms.values() ]
    t_acc = [ (s.get("timesfm_skill") or {}).get("acc") for s in syms.values() ]
    r_skill = [ (s.get("router_skill") or {}).get("skill_vs_majority") for s in syms.values() ]
    t_skill = [ (s.get("timesfm_skill") or {}).get("skill_vs_majority") for s in syms.values() ]
    mean = lambda xs: (sum(x for x in xs if x is not None) / len([x for x in xs if x is not None])) if any(x is not None for x in xs) else None  # noqa: E731
    print("\n" + "=" * 60)
    print(f"aggregate n_eff: router={r_n:.0f}  timesfm={t_n:.0f}")
    print(f"mean acc:       router={mean(r_acc)}  timesfm={mean(t_acc)}")
    print(f"mean skill:     router={mean(r_skill)}  timesfm={mean(t_skill)}")
    if r_n < 30 and t_n < 30:
        print("\nVERDICT: insufficient evidence (n_eff < 30) — keep accumulating stamps.")
        print("         Every light sweep tick adds one stamp per active symbol.")
    else:
        rs, ts = mean(r_skill), mean(t_skill)
        if rs is not None and ts is not None:
            if rs > ts:
                print("\nVERDICT: router edge OUTPERFORMS TimesFM on the same outcomes.")
                print("         Promotion candidate (router_edge_mode live) — check per-cell")
                print("         n_eff >= router_edge_min_n_eff before flipping.")
            elif ts > rs:
                print("\nVERDICT: TimesFM outperforms the router edge on the same outcomes.")
                print("         Keep timesfm_blend_mode live; the router edge stays shadow")
                print("         until its conditioning improves (vol_tier key, more archetypes).")
            else:
                print("\nVERDICT: parity on the same outcomes — keep both, keep measuring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
