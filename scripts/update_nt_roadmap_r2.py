"""
Upsert nt_roadmap rows for p_win_in_ev_mode / p_win_bracket_mode (R2 flip tracking).

Run:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe scripts/update_nt_roadmap_r2.py

Uses SUPABASE_URL + SUPABASE_KEY from env / .env.local (service role).
Best-effort; prints the SQL equivalent if HTTP fails so it can be pasted
into Supabase SQL editor.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env.local if present (Windows-safe UTF-8)
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
  'p_win_in_ev_mode',
  'shadow',
  now(),
  now() + interval '7 days',
  'After ≥7d of nt_live_expectancy_daily at min_p_win_bracket=0.20: bracket_accept vs all_qualified shows expired% DOWN and expectancy_r UP or flat. Then flip p_win_in_ev_mode to live first.',
  'pending',
  'worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md',
  '2026-09-23: R2 wired light+heavy (apply_bracket_to_ev/shadow). min_p_win_bracket=0.20 live in feature_flags.yaml. P2 nt_live_expectancy_daily (m163) is the flip-evidence table. HOLD shadow until the rollup bar is met. Rollback = flag to shadow/off.',
  now()
),
(
  'p_win_bracket_mode',
  'shadow',
  now(),
  now() + interval '14 days',
  'Only after p_win_in_ev_mode is live and |bias_raw| improves on the accepted set for +7d. Then flip p_win_bracket_mode to live (bracket gate participates in qualification).',
  'pending',
  'worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md',
  '2026-09-23: min_p_win_bracket=0.20 (was 0.45 — wrong scale, max p_win_bracket~0.31). Shadow gate log active. Flip SECOND, after EV mode. Rollback = flag to shadow/off.',
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
    url = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ACCESS_TOKEN")
    if not url or not key:
        print(SQL_EQUIV.format(sql=SQL))
        print("Missing SUPABASE_URL/KEY — SQL printed above.", file=sys.stderr)
        return 1

    import httpx

    # PostgREST cannot run arbitrary SQL. Prefer management API if token present;
    # else print SQL for manual apply (matches 161/162/163 workflow).
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    # Ref parsed from SUPABASE_URL (the variable .env.local carries).
    project_ref = os.environ.get("SUPABASE_URL", "").rstrip("/").split("//")[-1].split(".")[0]
    if token:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"https://api.supabase.com/v1/projects/{project_ref}/database/query",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"query": SQL},
                )
                print(f"management API HTTP {resp.status_code}: {resp.text[:400]}")
                return 0 if resp.is_success else 2
        except Exception as exc:
            print(f"management API failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    # Fallback: upsert via PostgREST (rows only — no raw SQL).
    rows = [
        {
            "mechanism": "p_win_in_ev_mode",
            "current_state": "shadow",
            "review_at": (datetime.now(timezone.utc)).isoformat(),
            "review_condition": (
                "After ≥7d of nt_live_expectancy_daily at min_p_win_bracket=0.20: "
                "bracket_accept vs all_qualified shows expired% DOWN and expectancy_r "
                "UP or flat. Then flip p_win_in_ev_mode to live first."
            ),
            "status": "pending",
            "worklog_ref": "worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md",
            "notes": (
                "2026-09-23: R2 wired light+heavy. min_p_win_bracket=0.20. "
                "P2 nt_live_expectancy_daily (m163) is flip evidence. HOLD shadow."
            ),
        },
        {
            "mechanism": "p_win_bracket_mode",
            "current_state": "shadow",
            "review_at": (datetime.now(timezone.utc)).isoformat(),
            "review_condition": (
                "Only after p_win_in_ev_mode is live and |bias_raw| improves on the "
                "accepted set for +7d. Then flip p_win_bracket_mode to live."
            ),
            "status": "pending",
            "worklog_ref": "worklog/20260922_R2_ADDENDUM_LIVE_DIST_AND_NEXT.md",
            "notes": (
                "2026-09-23: min_p_win_bracket=0.20 (was 0.45 wrong scale). "
                "Flip SECOND after EV mode."
            ),
        },
    ]
    # Fix review_at to +7d / +14d
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    rows[0]["review_at"] = (now + timedelta(days=7)).isoformat()
    rows[1]["review_at"] = (now + timedelta(days=14)).isoformat()
    rows[0]["set_at"] = now.isoformat()
    rows[1]["set_at"] = now.isoformat()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{url.rstrip('/')}/rest/v1/nt_roadmap?on_conflict=mechanism",
                headers={
                    "Authorization": f"Bearer {key}",
                    "apikey": key,
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json=rows,
            )
            print(f"PostgREST upsert HTTP {resp.status_code}: {resp.text[:400]}")
            return 0 if resp.is_success else 3
    except Exception as exc:
        print(f"PostgREST failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(SQL_EQUIV.format(sql=SQL))
        return 4


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
