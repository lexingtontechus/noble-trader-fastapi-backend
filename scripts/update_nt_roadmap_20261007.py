#!/usr/bin/env python3
"""
update_nt_roadmap_20261007.py — Update roadmap after pulse check + corrective actions.

Changes:
1. p_win_bracket_mode (id=58): HOLD shadow, push review to 2026-10-14
2. calibration_evidence_pool (id=70): FLIP live, mark resolved
3. heavy_sweep_reachability_mode (id=4): DO NOT FLIP, push review to 2026-10-14
4. dynamic_atr_vol_ratio_gate (id=81): Update with post-mortem, push review to 2026-10-14
5. Add new row: table_freshness_fix (this session's fix)
6. Add new row: dynamic_tier_realtime_optimization (design doc)
"""

import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env.local")

from regime_platform.core.settings import Settings
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

import httpx

s = Settings()
url = s.SUPABASE_URL.rstrip('/')
key = s.SUPABASE_KEY
headers = {**_headers(), "Content-Type": "application/json"}
now = datetime.now(timezone.utc)


async def patch_roadmap(mechanism: str, updates: dict) -> bool:
    """Patch a single nt_roadmap row by mechanism name."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{url}/rest/v1/nt_roadmap?mechanism=eq.{mechanism}",
                headers=headers,
                json=updates,
            )
            if resp.is_success:
                print(f"  ✓ {mechanism}: updated")
                return True
            else:
                print(f"  ✗ {mechanism}: HTTP {resp.status_code} — {resp.text[:200]}")
                return False
    except Exception as exc:
        print(f"  ✗ {mechanism}: {exc}")
        return False


async def insert_roadmap(row: dict) -> bool:
    """Insert a new nt_roadmap row."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{url}/rest/v1/nt_roadmap",
                headers={**headers, "Prefer": "return=minimal"},
                json=row,
            )
            if resp.is_success:
                print(f"  ✓ {row['mechanism']}: inserted")
                return True
            else:
                print(f"  ✗ {row['mechanism']}: HTTP {resp.status_code} — {resp.text[:200]}")
                return False
    except Exception as exc:
        print(f"  ✗ {row['mechanism']}: {exc}")
        return False


async def main():
    print("=" * 70)
    print("Update nt_roadmap — 2026-10-07 pulse check follow-up")
    print("=" * 70)

    # 1. p_win_bracket_mode (id=58): HOLD shadow
    print("\n1. p_win_bracket_mode — HOLD shadow")
    await patch_roadmap("p_win_bracket_mode", {
        "current_state": (
            "HOLD shadow 2026-10-07. 7-day observation window (10-01..10-07) "
            "shows bracket_accept expectancy -0.79 vs bracket_reject -0.83. "
            "Marginal improvement, not enough to justify live gate. "
            "Both cohorts deeply unprofitable — gate is not the bottleneck."
        ),
        "review_at": "2026-10-14T00:00:00+00:00",
        "notes": (
            "2026-10-07 pulse: 7d data shows bracket_accept n=1853 wr=9.8% exp=-0.79; "
            "bracket_reject n=460 wr=6.5% exp=-0.83. Gate helps marginally but "
            "both cohorts negative. Underlying signal quality is the bottleneck, "
            "not the gate. Re-evaluate after calibration_evidence_pool goes live."
        ),
    })

    # 2. calibration_evidence_pool (id=70): FLIP live, mark resolved
    print("\n2. calibration_evidence_pool — FLIP live, mark resolved")
    await patch_roadmap("calibration_evidence_pool", {
        "current_state": (
            "FLIPPED LIVE 2026-10-07. Shadow ran 11 days (since 09-26). "
            "Bias data confirms severe overconfidence (bias 0.4-0.8 across "
            "most cells). Evidence pool (qualified+suppressed) will give "
            "more accurate enforcement input."
        ),
        "status": "resolved",
        "review_condition": "Flipped live 2026-10-07. Monitor enforcement actions for 7 days.",
        "notes": (
            "2026-10-07: Flipped live after 11 days of shadow. "
            "All-tier bias includes gate_fail (~84% of pool) which dilutes "
            "the qualified-signal overconfidence signal. "
            "Evidence pool excludes gate_fail, giving a purer bias reading."
        ),
    })

    # 3. heavy_sweep_reachability_mode (id=4): DO NOT FLIP
    print("\n3. heavy_sweep_reachability_mode — DO NOT FLIP")
    await patch_roadmap("heavy_sweep_reachability_mode", {
        "current_state": (
            "DO NOT FLIP 2026-10-07. Shadow data shows 13/14 symbols have "
            "n_nothing_survived > 0. XAUEUR worst at 63%. Filter is too "
            "aggressive — correctly identifies that most buckets have "
            "negative expectancy, but has nothing to fall back to."
        ),
        "review_at": "2026-10-14T00:00:00+00:00",
        "notes": (
            "2026-10-07 pulse: n_nothing_survived rates: XAUEUR 63%, "
            "USDCAD 58%, XAUUSD 48%, US30USD 49%, USDJPY 45%. "
            "GBPUSD filtered Sharpe went negative (-0.53). "
            "USDCAD filtered Sharpe dropped 1.89→0.92. "
            "Design flaw: filter assumes positive-expectancy bucket exists. "
            "When it doesn't, should fail-open to best available, not return "
            "'nothing survived'. Needs redesign, not tuning."
        ),
    })

    # 4. dynamic_atr_vol_ratio_gate (id=81): Update with post-mortem
    print("\n4. dynamic_atr_vol_ratio_gate — Update with post-mortem")
    await patch_roadmap("dynamic_atr_vol_ratio_gate", {
        "current_state": (
            "POST-MORTEM 2026-10-07. Dynamic ATR is live and changing picks, "
            "but outcomes are poor. Replay was in-sample overfit (best geometry "
            "on same data it was evaluated on). Tight by_symbol grids too "
            "restrictive. Replay used close-only walk but live uses intrabar "
            "(flipped 2026-10-02). Next: widen grids, re-run replay with "
            "intrabar + walk-forward split."
        ),
        "review_at": "2026-10-14T00:00:00+00:00",
        "notes": (
            "2026-10-07 post-mortem: "
            "(1) Replay's 'best' geometries are out-of-sample overfit — "
            "picked on same 8,252 signals it was evaluated on. "
            "(2) Tight by_symbol grids (e.g. XAUUSD tp_atr [4.94,5.0,5.10]) "
            "have almost no room to find +EV cell. "
            "(3) Replay used close-only walk, but live resolver flipped to "
            "intrabar on 2026-10-02 — geometry rankings may not transfer. "
            "(4) Replay didn't account for signal quality changes "
            "(changing geometry changes which signals pass gates). "
            "Next: widen grids, re-run replay with intrabar + walk-forward."
        ),
    })

    # 5. Add new row: table_freshness_fix
    print("\n5. Add new row: table_freshness_fix")
    await insert_roadmap({
        "mechanism": "table_freshness_fix",
        "current_state": (
            "RESOLVED 2026-10-07. Root cause: upsert without delete-then-insert "
            "caused old rows to accumulate. limit=50 query returned arbitrary "
            "subset that looked stale. Fix: added _delete_old_rows() call "
            "before upsert in bracket_reachability.py and "
            "calibration_bias_bucket.py. Also created v_bracket_reachability_latest "
            "and v_calibration_bias_by_bucket_latest views (migration 202)."
        ),
        "status": "resolved",
        "review_condition": "Resolved 2026-10-07. Monitor computed_at freshness in next pulse check.",
        "worklog_ref": "worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md",
        "review_at": "2026-10-14T00:00:00+00:00",
    })

    # 6. Add new row: dynamic_tier_realtime_optimization
    print("\n6. Add new row: dynamic_tier_realtime_optimization")
    await insert_roadmap({
        "mechanism": "dynamic_tier_realtime_optimization",
        "current_state": (
            "DESIGN COMPLETE 2026-10-07. Pre-sweep optimization process "
            "designed to run 5 min before each light/heavy sweep. "
            "Recomputes dynamic ATR, dynamic deadline, regime edge, "
            "and geometry incubation from latest resolved signals. "
            "Recommendation: run in Studio as asyncio task (not edge function). "
            "See worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md §2."
        ),
        "status": "pending",
        "review_condition": "Design review. Then Phase 1: dynamic ATR real-time optimization in shadow.",
        "worklog_ref": "worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md",
        "review_at": "2026-10-14T00:00:00+00:00",
    })

    print("\n" + "=" * 70)
    print("Done.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
