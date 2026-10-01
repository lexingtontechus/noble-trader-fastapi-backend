#!/usr/bin/env python3
"""
backup_retention_tables.py — one-off full-table export before retention cleanup.

Worklog: 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13 (backup before
Track A / Track B cleanup). Tables targeted:

  - nt_renko_bricks   (305.7 MB, 50.51% of DB — Track A, blocked)
  - nt_sweep_result   (103.41 MB, 17.09% of DB — Track B)
  - nt_regime_log     (79.47 MB, 13.13% of DB — Track B)

No live feature needs more than ~7-30 days of nt_sweep_result/nt_regime_log
history, and nt_renko_bricks' retention design only needs ~2 days once
correlation's daily-close dependency is separated out (see worklog §4c) —
but "no feature needs it" isn't the same as "never useful again" (future
research, audit trail, debugging a regression that correlates with old
data). This is pure insurance: a full point-in-time export of all 3 tables
as they exist RIGHT NOW, before either cleanup track's DELETEs run.

Output: one gzip-compressed JSON-lines file per table (one JSON object per
line — streams row-by-row, no need to hold a whole table in memory, and
every row is independently parseable without re-reading the whole file).

Run ON THE STUDIO (not locally) — same convention as every other
Supabase-REST script in this repo (control_geometry_replay.py,
dynamic_atr_class_replay.py, etc.): the service-role key (.env.local)
never has to leave its existing environment, and the Studio is already the
trusted execution context for this project's cron/background jobs.

Usage:
  ssh noble-trader-unified
  cd /teamspace/studios/this_studio/noble-trader
  ./.venv/bin/python scripts/backup_retention_tables.py

Output directory (created if missing):
  /teamspace/studios/this_studio/noble-trader/backups/<run-date>/
    nt_renko_bricks.jsonl.gz
    nt_sweep_result.jsonl.gz
    nt_regime_log.jsonl.gz
    manifest.json   — row counts, byte sizes, start/end timestamps per table

After the Studio run, copy the backups/ directory down to a durable
location outside the Studio's own persistent disk (it survives restarts
but not a Studio deletion/recreation) — e.g.:
  scp -r noble-trader-unified:/teamspace/studios/this_studio/noble-trader/backups/<date> ./backups/
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env.local")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

# PostgREST page size. 1000 matches every other script in this repo
# (control_geometry_replay.py, dynamic_atr_class_replay.py, etc.) — the
# platform's de facto max row count per request regardless of `limit`.
PAGE_SIZE = 1000

# (table, select columns, order column) — order matters for stable
# pagination: PostgREST's offset/limit pagination is only guaranteed
# stable against an explicit ORDER BY (an unordered scan can shift rows
# between pages as concurrent writes land, same class of bug flagged in
# this worklog's §3 for fetch_daily_closes' missing `order` param).
TABLES: list[tuple[str, str, str]] = [
    ("nt_renko_bricks", "*", "brick_id"),
    ("nt_sweep_result", "*", "id"),
    ("nt_regime_log", "*", "id"),
]

RUN_DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
OUT_DIR = ROOT / "backups" / f"{RUN_DATE}_pre_retention_cleanup"


def fetch_page(client: httpx.Client, table: str, select: str, order_col: str, offset: int) -> list[dict]:
    """One PostgREST page, retried up to 3x on transient failure.

    Never silently drops a page — raises after exhausting retries so a
    partial/corrupt backup is never mistaken for a complete one.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = client.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=HEADERS,
                params={
                    "select": select,
                    "order": f"{order_col}.asc",
                    "limit": str(PAGE_SIZE),
                    "offset": str(offset),
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as exc:  # noqa: BLE001 - deliberately broad, retried then re-raised
            last_exc = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def backup_table(client: httpx.Client, table: str, select: str, order_col: str) -> dict:
    """Paginate the full table to a gzip-compressed JSON-lines file.

    Returns a manifest entry: {table, rows, bytes, started_at, finished_at}.
    """
    out_path = OUT_DIR / f"{table}.jsonl.gz"
    started_at = datetime.now(timezone.utc).isoformat()
    row_count = 0
    offset = 0

    print(f"[{table}] starting export -> {out_path}", flush=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as fh:
        while True:
            page = fetch_page(client, table, select, order_col, offset)
            if not page:
                break
            for row in page:
                fh.write(json.dumps(row, default=str))
                fh.write("\n")
            row_count += len(page)
            offset += PAGE_SIZE
            if row_count % 50_000 < PAGE_SIZE:
                print(f"[{table}] ... {row_count} rows so far", flush=True)
            if len(page) < PAGE_SIZE:
                break

    finished_at = datetime.now(timezone.utc).isoformat()
    byte_size = out_path.stat().st_size
    print(
        f"[{table}] done: {row_count} rows, {byte_size / 1_048_576:.1f} MB compressed",
        flush=True,
    )
    return {
        "table": table,
        "rows": row_count,
        "bytes_compressed": byte_size,
        "started_at": started_at,
        "finished_at": finished_at,
        "file": out_path.name,
    }


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE_URL / SUPABASE_KEY not set — aborting.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Backup output directory: {OUT_DIR}")

    manifest: dict = {
        "run_started_at": datetime.now(timezone.utc).isoformat(),
        "supabase_url": SUPABASE_URL,
        "tables": [],
    }

    with httpx.Client() as client:
        for table, select, order_col in TABLES:
            entry = backup_table(client, table, select, order_col)
            manifest["tables"].append(entry)

    manifest["run_finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path = OUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written: {manifest_path}")
    print("\nSummary:")
    for entry in manifest["tables"]:
        print(f"  {entry['table']:<20} {entry['rows']:>10} rows  {entry['bytes_compressed'] / 1_048_576:>8.1f} MB")

    print(
        "\nNext step: copy this directory off the Studio's own disk to a "
        "durable location, e.g.:\n"
        f"  scp -r noble-trader-unified:{OUT_DIR} ./backups/"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
