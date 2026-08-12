#!/usr/bin/env python3
"""
One-time nt_signal_sim backlog drain (2026-08-12).

The EOD resolver historically resolved only 200 pending rows/night while the
sweep captured ~3,200 signals/day — by 2026-08-12 the ledger had 16,792
pending rows (93% of 17,992). This script runs the resolver repeatedly with a
high per-pass limit until the pending backlog is exhausted (or a safety cap
on iterations is hit).

Usage (repo venv, .env.local provides SUPABASE_URL/KEY):

    PYTHONPATH="$PWD" ./.venv/Scripts/python.exe scripts/drain_signal_backlog.py

Flags:
    --limit N      per-pass resolver limit (default 2000)
    --max-passes N stop after N passes even if backlog remains (default 20)

The script is read-only except for the resolver's own PATCH/insert writes
(outcome/exit fields + nt_calibration_records). It NEVER boots the sweep
engine. Re-run any time the pending count grows large again.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from regime_platform.sweep_orchestrator.eod_resolver import resolve_pending_signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def _count_pending() -> int:
    """Return the number of unresolved nt_signal_sim rows (best-effort)."""
    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    url = _url("nt_signal_sim")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.head(
                url,
                headers={**_headers(), "Prefer": "count=exact", "Range": "0-0"},
                params={"select": "signal_id", "outcome": "is.null"},
            )
            cr = resp.headers.get("content-range", "")
            if "/" in cr:
                total = cr.split("/", 1)[1]
                return int(total) if total.isdigit() else 0
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[drain] pending count failed: {exc}", flush=True)
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Drain the nt_signal_sim backlog")
    parser.add_argument("--limit", type=int, default=2000, help="per-pass resolver limit")
    parser.add_argument("--max-passes", type=int, default=20, help="max passes before giving up")
    args = parser.parse_args()

    print(f"[drain] backlog drain start — limit={args.limit} max_passes={args.max_passes}", flush=True)

    pending = await _count_pending()
    print(f"[drain] pending before: {pending}", flush=True)
    if pending <= 0:
        print("[drain] nothing pending — done", flush=True)
        return 0

    total_resolved = 0
    for pass_no in range(1, args.max_passes + 1):
        summary = await resolve_pending_signals(limit=args.limit)
        resolved = summary.get("resolved", 0)
        total_resolved += resolved
        print(
            f"[drain] pass {pass_no}: resolved={resolved} "
            f"by_outcome={summary.get('by_outcome')} "
            f"windowed_out={summary.get('windowed_out')} errors={summary.get('errors')}",
            flush=True,
        )
        remaining = await _count_pending()
        print(f"[drain] pass {pass_no}: pending after={remaining}", flush=True)
        if remaining <= 0:
            print(f"[drain] backlog cleared — {total_resolved} resolved over {pass_no} pass(es)", flush=True)
            return 0
        # Guard against hot loops when the resolver can't make progress.
        if resolved == 0 and summary.get("errors", 0) > 0:
            print("[drain] no progress (errors) — stopping", flush=True)
            return 1

    remaining = await _count_pending()
    print(
        f"[drain] max passes reached — {total_resolved} resolved, {remaining} still pending. "
        f"Re-run or raise --max-passes.",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
