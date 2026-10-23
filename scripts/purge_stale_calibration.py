#!/usr/bin/env python3
"""
Purge stale calibration records — pre-fix deployment contamination.

The EV engine fix (p_regime cap + Markov degenerate fix) was deployed at
2026-08-19T00:26:52Z (Studio restart). All nt_calibration_records with
entered_at before that timestamp were computed with the broken logit pooling
(p_regime=1.0 → logit(1.0)=34.5 dominating the blend) and contaminate the
rolling 7-day bias window.

This script deletes them so the calibration bias window only sees post-fix data.

Usage:
    cd /c/Users/aloys/OneDrive/Documents/GitHub/noble-trader-workspace/noble-trader-fastapi-backend
    PYTHONPATH="$PWD/src" .venv/Scripts/python.exe scripts/purge_stale_calibration.py

Safety:
    - Runs in DRY RUN mode by default (--execute to commit)
    - Reports row count before deletion
    - Never raises — all errors are logged and reported
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url


POST_FIX_TS = "2026-08-19T00:26:52Z"  # Studio restart timestamp with EV fix deployed


async def count_stale_records(client: httpx.AsyncClient) -> int:
    """Count calibration records older than the post-fix deployment."""
    if not settings.SUPABASE_URL:
        print("ERROR: SUPABASE_URL not configured in settings")
        return -1

    url = _url("nt_calibration_records")
    params = {
        "select": "id",
        "entered_at": f"lt.{POST_FIX_TS}",
    }

    try:
        resp = await client.head(
            url,
            headers={**_headers(), "Prefer": "count=exact", "Range": "0-0"},
            params=params,
        )
        if not resp.is_success:
            print(f"ERROR: HEAD request failed (HTTP {resp.status_code}): {resp.text[:300]}")
            return -1

        content_range = resp.headers.get("content-range", "")
        if "/" in content_range:
            total_str = content_range.split("/", 1)[1].strip()
            if total_str != "*":
                return int(total_str)
        return 0
    except Exception as exc:
        print(f"ERROR counting stale records: {exc}")
        return -1


async def delete_stale_records(client: httpx.AsyncClient) -> int:
    """DELETE calibration records older than the post-fix deployment."""
    if not settings.SUPABASE_URL:
        print("ERROR: SUPABASE_URL not configured in settings")
        return -1

    url = _url("nt_calibration_records")
    params = {
        "entered_at": f"lt.{POST_FIX_TS}",
    }

    try:
        resp = await client.delete(
            url,
            headers={**_headers(), "Prefer": "count=exact"},
            params=params,
        )
        if not resp.is_success:
            print(f"ERROR: DELETE failed (HTTP {resp.status_code}): {resp.text[:300]}")
            return -1

        content_range = resp.headers.get("content-range", "")
        if "/" in content_range:
            total_str = content_range.split("/", 1)[1].strip()
            if total_str != "*":
                return int(total_str)
        return 0
    except Exception as exc:
        print(f"ERROR deleting stale records: {exc}")
        return -1


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purge stale nt_calibration_records (pre-EV-fix deployment)"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually commit the DELETE (default is dry-run)"
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("=" * 70)
    print(f"PURGE STALE CALIBRATION RECORDS — {now}")
    print(f"Delete boundary: entered_at < {POST_FIX_TS}")
    print(f"Mode: {'EXECUTE (DELETE committed)' if args.execute else 'DRY RUN (no changes)'}")
    print("=" * 70)

    if not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
        print("ERROR: Supabase credentials not configured in settings.")
        print("       Set SUPABASE_URL and SUPABASE_KEY in .env.local or Studio secrets.")
        return 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Phase 1: Count stale records
        count = await count_stale_records(client)
        if count < 0:
            print("FATAL: Could not count stale records — aborting.")
            return 2
        if count == 0:
            print("No stale records found — nothing to purge.")
            return 0

        print(f"\nStale records to delete: {count}")
        print(f"  Table: nt_calibration_records")
        print(f"  Condition: entered_at < '{POST_FIX_TS}'")

        if args.execute:
            # Phase 2: Delete
            deleted = await delete_stale_records(client)
            if deleted < 0:
                print("FATAL: Delete operation failed.")
                return 3
            print(f"\n✅ Deleted {deleted} stale calibration records.")
            print(f"\nNext steps:")
            print(f"  1. The rolling 7-day bias window now only contains post-fix data.")
            print(f"  2. Calibration bias should recompute correctly on next sweep.")
            print(f"  3. Run: PYTHONPATH=\"$PWD/src\" .venv/Scripts/python.exe scripts/purge_stale_calibration.py")
            print(f"    (without --execute) to verify count is now 0.")
            return 0
        else:
            print(f"\n📋 DRY RUN — no records deleted.")
            print(f"\nTo commit the purge, re-run with --execute:")
            print(f"  PYTHONPATH=\"$PWD/src\" .venv/Scripts/python.exe scripts/purge_stale_calibration.py --execute")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
