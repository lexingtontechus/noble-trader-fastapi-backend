#!/usr/bin/env python3
"""
update_nt_roadmap_edge_lab.py — register the edge-lab proof-run mechanism.

The orchestrator's ``_run_edge_lab`` heartbeats ``nt_roadmap(mechanism='edge_lab_run')``
after every daily run (``write_job_heartbeat``).  ``write_job_heartbeat`` is a
PATCH — a missing row is a silent no-op, so without this row the daily run is
invisible in the roadmap and there is no way to see it ran (or failed).

Idempotent: re-running detects the existing row and updates it instead of
inserting a duplicate (``nt_roadmap.mechanism`` is not UNIQUE, so a blind POST
would accumulate rows).

Run:
    .venv/Scripts/python.exe scripts/update_nt_roadmap_edge_lab.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.local")

import httpx  # noqa: E402

from regime_platform.core.settings import Settings  # noqa: E402
from regime_platform.sweep_orchestrator.supabase_client import _headers  # noqa: E402

MECHANISM = "edge_lab_run"
STATE = (
    "SHADOW 2026-10-21. Daily APScheduler job (edge-lab-daily, 18:30 UTC, 24 min "
    "after the heavy sweep) runs the regime_edge proof engine on TDVA data — the "
    "live sweep's own TradingView feed, symbols resolved via "
    "nt_symbol.tradingview_symbol. Produces worklog/kairos/reports/regime_edge/"
    "<runid>/edge_lab.json. edge_lab_run_mode is a SEPARATE lever from "
    "regime_edge_mode: shadow writes the artifact to the worklog only; live also "
    "publishes it to nt_edge_lab_run for the nobletradingapp edge-lab page. "
    "Cadence is DAILY, not a single duration — the 14-symbol run is ~30 min wall "
    "and the TDVA batch endpoint caps one fetch at 5000 1h bars (~208 days), so "
    "the sample is shorter than the old 730-day yfinance baseline."
)
REVIEW_CONDITION = (
    "Promote to live only after ≥7 consecutive daily runs complete without error "
    "and the published artifact renders correctly on the nobletradingapp edge-lab "
    "page. Do NOT gate this on regime_edge_mode — the run is the gate's evidence."
)


async def main() -> int:
    s = Settings()
    url = s.SUPABASE_URL.rstrip("/")
    headers = {**_headers(), "Content-Type": "application/json"}
    now = datetime.now(timezone.utc)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. existing row? (mechanism is not UNIQUE — never blind-POST)
        resp = await client.get(
            f"{url}/rest/v1/nt_roadmap",
            headers=headers,
            params={"mechanism": f"eq.{MECHANISM}", "select": "id"},
        )
        if resp.status_code >= 400:
            print(f"✗ select failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return 1
        existing = resp.json()

        payload = {
            "mechanism": MECHANISM,
            "current_state": STATE,
            "review_condition": REVIEW_CONDITION,
            "status": "pending",
            "worklog_ref": "worklog/kairos/20261021_EDGE_LAB_FULL_ACTIVE_SET.md",
            "set_at": now.isoformat(),
            "review_at": (now + timedelta(days=7)).isoformat(),
        }

        if existing:
            resp = await client.patch(
                f"{url}/rest/v1/nt_roadmap",
                headers=headers,
                params={"mechanism": f"eq.{MECHANISM}"},
                json=payload,
            )
            verb = "updated"
        else:
            resp = await client.post(
                f"{url}/rest/v1/nt_roadmap",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload,
            )
            verb = "inserted"

        if resp.is_success:
            print(f"✓ {MECHANISM}: {verb}")
        else:
            print(f"✗ {MECHANISM}: HTTP {resp.status_code} — {resp.text[:300]}")
            return 1

        # 2. re-select to confirm (the idempotency check)
        resp = await client.get(
            f"{url}/rest/v1/nt_roadmap",
            headers=headers,
            params={"mechanism": f"eq.{MECHANISM}", "select": "id,mechanism,status"},
        )
        rows = resp.json()
        print(f"✓ rows for {MECHANISM}: {len(rows)} -> {rows}")
        if len(rows) != 1:
            print("✗ expected exactly 1 row")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
