#!/usr/bin/env python
"""publish_edge_lab.py — push the edge-lab artifact to Supabase.

Called by the orchestrator (post-run) or manually:

    .venv/bin/python scripts/publish_edge_lab.py /path/to/edge_lab.json

The function `upsert_edge_lab_run` lives in the NOBLESERV application
(nobletradingapp), so kairos writes via the Supabase REST API using the
service-role key.  This is the same pattern as:
    • eod-report-generate (Edge Function writes v_eod_report)
    • kairos writes nt_regime_log, nt_signal_sim, etc.

The table schema is migration 0031: nt_edge_lab_run + v_edge_lab_run.

Idempotent: same run_id overwrites — safe for retries.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from regime_platform.core.settings import settings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("json_path", type=Path, help="edge_lab.json to upload")
    p.add_argument(
        "--note", default=None, help="short human summary (optional)"
    )
    args = p.parse_args(argv)

    if not settings.SUPABASE_URL or not settings.SUPABASE_SECRET_KEY:
        print("[publish] SUPABASE_URL/SECRET_KEY not set — not an error, skipping")
        return 0

    import httpx

    json_path = args.json_path
    if not json_path.exists():
        print(f"[publish] file not found: {json_path}")
        return 1

    payload = json.loads(json_path.read_text())
    meta = payload.get("meta", {})

    run_id = meta.get("run_id", json_path.stem)
    symbols = meta.get("symbols", [])
    bars = meta.get("bars_per_symbol", {})
    total_bars = sum(bars.values()) if bars else len(symbols)

    now = settings.datetime.datetime.now(tz=settings.datetime.timezone.utc)

    upsert_url = f"{settings.SUPABASE_URL}/rest/v1/nt_edge_lab_run"
    headers = {
        "apikey": settings.SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
        # run_id is the table's PRIMARY KEY, so a re-run with the same run_id
        # upserts instead of 409-ing. Without this the docstring's promised
        # idempotency is false and the orchestrator aborts the whole run.
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    body = {
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "engine": meta.get("engine", "regime_edge v1"),
        "symbols": symbols,
        "period_start": meta.get("period_start"),
        "period_end": meta.get("period_end"),
        "bars_total": total_bars,
        "payload": payload,
        "note": args.note,
    }

    print(f"[publish] uploading edge lab run {run_id} ({len(payload.get('fixes',[]))} fixes)")
    try:
        resp = httpx.post(upsert_url, headers=headers, json=body, timeout=60.0)
        if resp.status_code >= 400:
            print(f"[publish] HTTP {resp.status_code}: {resp.text}")
            return 1
        print(f"[publish] {run_id} stored in nt_edge_lab_run")
    except httpx.HTTPStatusError as e:
        print(f"[publish] HTTP error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())