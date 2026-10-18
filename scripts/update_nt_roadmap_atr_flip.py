"""
Upsert nt_roadmap rows for dynamic/ATR-anchored flip follow-ups (2026-09-25).

Run:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_atr_flip.py

Best-effort via SUPABASE_URL + SUPABASE_KEY; prints SQL if HTTP fails.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_env = Path(__file__).resolve().parents[1] / ".env.local"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SQL = """
INSERT INTO nt_roadmap (
  mechanism, current_state, set_at, review_at, review_condition,
  status, worklog_ref, notes, updated_at
) VALUES
(
  'atr_anchored_evidence_mode_flip',
  'shadow → recommend live (hybrid promote only)',
  now(),
  now() + interval '3 days',
  'Confirm atr_anchored_evidence_mode=live in feature_flags + Studio deploy. Next heavy: promote/pool admission visible for ≥1 symbol (pick changed + long-lag/Sharpe rule). No full-grid flip.',
  'pending',
  'worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md',
  '2026-09-25 assessment GO for hybrid only. Kill: no promote logs and no symbol admits ATR pool after 2 heavies → investigate should_promote_atr_candidates wiring. Rollback flag to shadow.',
  now()
),
(
  'dynamic_atr_mode_flip',
  'shadow',
  now(),
  now() + interval '10 days',
  'After hybrid promote live + 1 heavy cycle + ~7d light: promoted vs control symbols show tp_distance_atr DOWN toward 0.5-2, pct_expired DOWN, closed pnl_dollars UP or flat at similar n. Then flip dynamic_atr_mode=live (risk-capped pick).',
  'pending',
  'worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md',
  'Kill bar: exp% flat/up or closed expectancy worse on promoted set → keep shadow. Cap stays dynamic_atr_risk_cap=0.35.',
  now()
),
(
  'heavy_sweep_atr_anchored_full_flip',
  'shadow (full grid)',
  now(),
  now() + interval '17 days',
  'P0.4 (20260919_HEAVY_GEOMETRY_PROMOTE): Stage 1 green OR hybrid already live with improved tp_atr/long-lag on promoted symbols for one cycle. Then heavy_sweep_atr_anchored_bricks_mode=live (all symbols).',
  'pending',
  'worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md',
  'Do not flip on shadow Sharpe alone (13/14 is necessary not sufficient). Prefer hybrid permanent if some symbols fail the rule.',
  now()
),
(
  'min_rr_geometry_guard',
  'inert (min_rr=1.5, light non-binding)',
  now(),
  now() + interval '17 days',
  'Spot-check winning ATR-anchored combos still have tp_bricks/sl_bricks >= 1.5 so heavy quality_filter min_rr pre-filter does not drop them. No MIN_RR code change unless fail.',
  'pending',
  'worklog/20260925_DYNAMIC_ATR_LIVE_FLIP_ASSESSMENT.md',
  '2026-09-25 clarification: tight ATR is brick_size/ATR distance, not RR counts. min_rr remains inert on light qualification (2026-09-20). Do not raise min_rr as a quality lever.',
  now()
)
ON CONFLICT (mechanism) DO UPDATE SET
  current_state = EXCLUDED.current_state,
  set_at = EXCLUDED.set_at,
  review_at = EXCLUDED.review_at,
  review_condition = EXCLUDED.review_condition,
  status = EXCLUDED.status,
  worklog_ref = EXCLUDED.worklog_ref,
  notes = EXCLUDED.notes,
  updated_at = EXCLUDED.updated_at;
"""

SQL_EQUIV = """-- Paste into Supabase SQL editor if the script cannot reach PostgREST:
{sql}
"""


async def main() -> int:
    import httpx

    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE")
    )
    if not url or not key:
        print(SQL_EQUIV.format(sql=SQL))
        print("Missing SUPABASE_URL/KEY — SQL printed above.", file=sys.stderr)
        return 1

    # Prefer management API like pulse_check when access token present
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    # Ref parsed from SUPABASE_URL (the variable .env.local carries).
    ref = os.environ.get("SUPABASE_URL", "").rstrip("/").split("//")[-1].split(".")[0]
    async with httpx.AsyncClient(timeout=45.0) as client:
        if token:
            resp = await client.post(
                f"https://api.supabase.com/v1/projects/{ref}/database/query",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"query": SQL},
            )
        else:
            resp = await client.post(
                f"{url.rstrip('/')}/rest/v1/rpc/exec_sql",
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
                json={"query": SQL},
            )
        print(f"HTTP {resp.status_code}")
        print(resp.text[:2000])
        if resp.status_code >= 400:
            print(SQL_EQUIV.format(sql=SQL))
            return 1

    # Verify
    check = """
        select mechanism, status, review_at::text, left(current_state, 80) as state
        from nt_roadmap
        where mechanism in (
          'atr_anchored_evidence_mode_flip','dynamic_atr_mode_flip',
          'heavy_sweep_atr_anchored_full_flip','min_rr_geometry_guard'
        )
        order by mechanism;
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        if token:
            r = await client.post(
                f"https://api.supabase.com/v1/projects/{ref}/database/query",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"query": check},
            )
            print("=== verify ===")
            print(r.text[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
