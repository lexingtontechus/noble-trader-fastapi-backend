#!/usr/bin/env python3
"""
stage1_atr_anchored_review.py — Stage 1 evidence for ATR-anchored flip

Uses **updated_at** (not computed_at) per migration 135.

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/stage1_atr_anchored_review.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from regime_platform.core.settings import settings  # noqa: E402


async def main() -> int:
    if not settings.SUPABASE_URL:
        print("SUPABASE_URL not set")
        return 1
    headers = {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
    }
    base = settings.SUPABASE_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=40.0) as c:
        r = await c.get(
            f"{base}/rest/v1/nt_heavy_sweep_atr_anchored_shadow",
            headers=headers,
            params={"select": "*", "order": "updated_at.desc", "limit": "50"},
        )
        shadow = r.json() if r.status_code == 200 else []
        r2 = await c.get(
            f"{base}/rest/v1/nt_bracket_reachability",
            headers=headers,
            params={"select": "symbol,distance_atr_bucket,n_stored,pct_expired", "limit": "300"},
        )
        reach = r2.json() if r2.status_code == 200 else []
        lag = {}
        for x in reach:
            if int(x.get("n_stored") or 0) < 3:
                continue
            lag[(x["symbol"], x["distance_atr_bucket"])] = x.get("pct_expired")

    print("=== Stage 1 ATR-anchored (updated_at) ===")
    print(f"{'sym':<9} {'n_cmp':>5} {'chg':>4} {'base':<6} {'wide':<6} {'lagB':>6} {'lagW':>6} {'promote?':>8} {'updated_at':<20}")
    print("-" * 90)
    if not isinstance(shadow, list) or not shadow:
        print("no shadow rows", r.status_code if hasattr(r, "status_code") else "")
        return 0
    from regime_platform.sweep_orchestrator.tiered_atr import should_promote_atr_candidates
    for x in shadow:
        sym = x.get("symbol")
        bb = x.get("last_baseline_bucket")
        wb = x.get("last_widened_bucket")
        lb = lag.get((sym, bb))
        lw = lag.get((sym, wb))
        prom = should_promote_atr_candidates(
            selection_changed=bool(x.get("n_changed")),
            baseline_bucket=bb,
            widened_bucket=wb,
            pct_expired_by_bucket={bb: lb, wb: lw} if bb or wb else {},
        )
        print(
            f"{sym:<9} {x.get('n_compared'):>5} {x.get('n_changed'):>4} "
            f"{str(bb):<6} {str(wb):<6} "
            f"{(lb if lb is not None else -1):>6.2f} {(lw if lw is not None else -1):>6.2f} "
            f"{str(prom):>8} {str(x.get('updated_at'))[:19]:<20}"
        )
    print(
        "\nPromote=true + atr_anchored_evidence_mode=live → ATR candidates enter deadline pool."
        "\nGlobal heavy_sweep_atr_anchored_bricks_mode=live still roadmap-aligned (2026-10-05)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
