"""
Daily Close Snapshot — durable daily-close storage, independent of brick retention
═══════════════════════════════════════════════════════════════════════════════

Root cause (2026-10-01, worklog
20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.1/§13.6, "Track A"):
``v_talaria_daily_close`` (nobletradingapp migration 0011) resamples
``nt_renko_bricks`` directly for correlation's 120-day window
(nobletradingapp-backend's ``fetch_daily_closes``). ``nt_renko_bricks`` also
needs to be prunable to its designed ~2-day live-grid steady state
(``renko_brick_retention_days`` — the dashboard/plugin chart's "last 10
bricks" need, see nobletradingapp/docs/worklog-renko-bricks-retention-contract.md)
AND needs its stale-grid pruning to actually bind across the full symbol
roster (confirmed root cause: ``renko_retention.py::_distinct_symbols()``
only ever sees 2 of 14 active symbols — PostgREST caps GET responses at
1000 rows regardless of the ``limit=10000`` param requested).

These two needs are in direct conflict if correlation keeps reading
``nt_renko_bricks`` directly — fixing retention to finally bind across the
full roster would immediately regress correlation back to "2 complete
sessions across 14 symbols" (the exact bug
nobletradingapp/docs/worklog-portfolio-analytics-cache-and-thin-history.md
already fixed around once, 2026-09-30, from a different root cause).

This module is the nightly snapshot that keeps ``nt_daily_close`` current
(mirrors ``renko_retention.py``'s shape and design rules), run as Part VI
of the eod-lookback cycle (``orchestrator.py::_run_eod_lookback``),
inserted BEFORE the existing renko brick retention part (which becomes
Part VII) — ordering matters: the snapshot must capture a session's close
before that session could ever be pruned by brick retention, even though
in steady state both run the same night with no actual race (retention
only touches ``session_date < cutoff``, never today's/yesterday's rows).

**Design note — per-(symbol, session_date) targeted queries, NOT a flat
scan:** an earlier draft of this module issued one flat
``nt_renko_bricks?session_date=gte.<cutoff>&limit=10000`` query and reduced
to one row per ``(symbol, session_date)`` client-side. That reproduces the
EXACT same bug class this module exists to work around: this Supabase
project caps every GET response at 1000 rows regardless of the requested
``limit``, and confirmed live, a 3-day flat scan across this table's 14
symbols × many stale ``brick_size`` grids returned only 4 of the ~42
expected ``(symbol, session_date)`` pairs before any grid-dedup even
happened. Instead, this module loops over the active symbol roster (via
``fetch_active_symbols()`` — reused rather than re-implemented, same
roster source the eventual ``_distinct_symbols()`` fix will use, see
worklog §13.2) × each day in the lookback window, issuing one
``LIMIT 1`` query per ``(symbol, session_date)`` pair. Each such query
returns at most 1 row, so it can never hit the 1000-row cap regardless of
how many stale grids exist for that symbol/day — correctness no longer
depends on row-count skew at all.

Matches ``v_talaria_daily_close``'s own ``DISTINCT ON (symbol,
session_date) ... ORDER BY brick_index DESC, ts DESC`` semantics exactly
(migration 0011, nobletradingapp repo) — last brick of the day (highest
brick_index, tie-broken by ts) as that day's close, with no brick_size
filter (same known "mixes grids" limitation already documented as a
separate, out-of-scope issue in
nobletradingapp/docs/worklog-renko-chart-grid-mismatch-and-orphan-purge.md
Root cause B — this module intentionally mirrors the existing view's
behavior for parity, not a redesign).

Design rules (mirror eod_retention.py / renko_retention.py):
  • Best-effort only — every failure is caught, logged and counted in the
    summary; this module never raises out of the caller.
  • Uses PostgREST with ``Prefer: count=exact`` so the affected-row count
    is parsed from ``Content-Range``.

This module makes NO changes to ``nt_renko_bricks`` — it only reads from
it and writes to the separate ``nt_daily_close`` table.

CLI:
    python -m regime_platform.sweep_orchestrator.daily_close_snapshot
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    fetch_active_symbols,
)

logger = logging.getLogger("regime.sweep.daily_close_snapshot")

# Rolling lookback window for the nightly snapshot read — short on purpose
# (this is a self-healing top-up, not the backfill; the one-time 120-day
# backfill already ran in migration 190). 3 days comfortably covers a
# single missed run (weekend gap, deploy hiccup) without re-reading
# unnecessary history every night.
SNAPSHOT_LOOKBACK_DAYS = 3


def _parse_content_range_total(content_range: str | None) -> int:
    """Parse the total count from a PostgREST ``Content-Range`` header.

    Shapes: ``0-0/42`` (exact), ``0-999/*`` (capped at 1000 — unknown
    total), ``*/0`` (empty). Returns the numeric total when present,
    else 0.
    """
    if not content_range or "/" not in content_range:
        return 0
    total = content_range.split("/", 1)[1].strip()
    if total == "*":
        return 0
    try:
        return int(total)
    except ValueError:
        return 0


async def _fetch_one_daily_close(
    client: httpx.AsyncClient, symbol: str, session_date: str,
) -> dict[str, Any] | None:
    """Fetch the single latest-brick close for one ``(symbol, session_date)``.

    ``LIMIT 1`` means this can never hit the project's 1000-row GET cap,
    regardless of how many stale ``brick_size`` grids exist for this
    symbol/day. Best-effort: returns ``None`` on any failure or if no rows
    match, never raises.
    """
    try:
        resp = await client.get(
            _url("nt_renko_bricks"),
            headers=_headers(),
            params={
                "select": "symbol,session_date,close_price,ts",
                "symbol": f"eq.{symbol}",
                "session_date": f"eq.{session_date}",
                "order": "brick_index.desc,ts.desc",
                "limit": "1",
            },
        )
        if not resp.is_success:
            logger.warning(
                "[daily_close:snapshot] fetch %s/%s HTTP %d — %s",
                symbol, session_date, resp.status_code, resp.text[:200],
            )
            return None
        rows = resp.json() or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.warning(
            "[daily_close:snapshot] fetch %s/%s failed: %s", symbol, session_date, exc,
        )
        return None


async def _fetch_recent_daily_closes(
    client: httpx.AsyncClient, symbols: list[str], session_dates: list[str],
) -> list[dict[str, Any]]:
    """Fetch one row per ``(symbol, session_date)`` across the full grid,
    concurrently (``asyncio.gather``, mirroring ``fetch_daily_closes``'
    concurrent-chunk pattern in the sibling nobletradingapp-backend repo).

    Returns only the rows that actually matched (skips ``None``s).
    """
    tasks = [
        _fetch_one_daily_close(client, symbol, session_date)
        for symbol in symbols
        for session_date in session_dates
    ]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


async def _upsert_daily_closes(
    client: httpx.AsyncClient, rows: list[dict[str, Any]],
) -> int:
    """Upsert ``(symbol, session_date, close_price, ts)`` rows into
    ``nt_daily_close`` via ``on_conflict=symbol,session_date``.

    Best-effort: returns the upserted-row count, 0 on any failure, never
    raises.
    """
    if not rows:
        return 0
    try:
        resp = await client.post(
            f"{_url('nt_daily_close')}?on_conflict=symbol,session_date",
            headers={
                **_headers(),
                "Prefer": "resolution=merge-duplicates,count=exact",
            },
            json=[
                {
                    "symbol": r["symbol"],
                    "session_date": r["session_date"],
                    "close_price": r["close_price"],
                    "ts": r["ts"],
                }
                for r in rows
            ],
        )
        if not resp.is_success:
            logger.warning(
                "[daily_close:snapshot] upsert HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        return _parse_content_range_total(resp.headers.get("content-range")) or len(rows)
    except Exception as exc:
        logger.warning("[daily_close:snapshot] upsert failed: %s", exc)
        return 0


async def run_daily_close_snapshot() -> dict[str, Any]:
    """Run the nightly ``nt_daily_close`` top-up from ``nt_renko_bricks``.

    Returns ``{rows_read, rows_upserted, symbols_checked, errors}``. Never
    raises — every failure is caught, logged and counted in ``errors``.
    """
    summary: dict[str, Any] = {
        "rows_read": 0,
        "rows_upserted": 0,
        "symbols_checked": 0,
        "errors": 0,
    }
    if not settings.SUPABASE_URL:
        logger.warning("daily_close_snapshot: Supabase not configured — nothing to snapshot")
        return summary

    today = datetime.now(timezone.utc)
    session_dates = [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(SNAPSHOT_LOOKBACK_DAYS)
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            active = await fetch_active_symbols()
            symbols = [s["symbol"] for s in active if s.get("symbol")]
            summary["symbols_checked"] = len(symbols)
            rows = await _fetch_recent_daily_closes(client, symbols, session_dates)
            summary["rows_read"] = len(rows)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[daily_close:snapshot] read pass failed: %s", exc)
            rows = []

        try:
            summary["rows_upserted"] = await _upsert_daily_closes(client, rows)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[daily_close:snapshot] upsert pass failed: %s", exc)

    logger.info(
        "[daily_close:snapshot] complete: rows_read=%d rows_upserted=%d "
        "symbols_checked=%d errors=%d",
        summary["rows_read"], summary["rows_upserted"],
        summary["symbols_checked"], summary["errors"],
    )
    return summary


async def main() -> None:
    """CLI entry: run the snapshot and print the summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = await run_daily_close_snapshot()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
