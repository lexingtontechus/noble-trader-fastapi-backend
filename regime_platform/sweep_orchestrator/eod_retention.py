"""
EOD Retention / Prune — keep ``nt_signal_sim`` bounded
══════════════════════════════════════════════════════

The signal ledger captures EVERY non-neutral signal from both sweep paths
(light ~5-min + weekly heavy) at ~3,200 rows/day. Without a prune the table
grows unbounded (17,992 rows / 16,792 pending observed 2026-08-12) and the
pending partial index keeps growing. This module is the nightly hygiene pass:

  1. **Delete resolved** — ``outcome IS NOT NULL`` AND ``resolved_at`` older
     than ``EOD_RETENTION_RESOLVED_DAYS`` (default 30). Calibration bias data
     lives separately in ``nt_calibration_records``, so deleting old resolved
     ledger rows loses no bias signal.
  2. **Expire stale pending** — ``outcome IS NULL`` AND ``ts`` older than
     ``EOD_RETENTION_STALE_DAYS`` (default 7) are PATCHed to ``expired``.
     They have sat past the resolver's 48-bar expiry window with no touch;
     a timeout is neither a win nor a loss, so no calibration record is
     written (same convention as ``eod_resolver``). This frees the pending
     partial index and stops the resolver re-picking stale rows forever.

Design rules (mirror eod_resolver.py / eod_correction.py):
  • Best-effort only — every failure is caught, logged and counted in the
    summary; this module never raises out of the caller.
  • Uses PostgREST with ``Prefer: count=exact`` so the affected-row count is
    parsed from ``Content-Range`` (the 1,000-row cap applies to GET body
    rows; count=exact gives the true total via the header).

CLI:
    python -m regime_platform.sweep_orchestrator.eod_retention
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.eod_retention")


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


async def _count_rows(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> int:
    """HEAD-count matching rows via ``Prefer: count=exact`` + ``Range: 0-0``.

    Raises on non-success so the caller counts it as an error (a HEAD 400
    must NOT silently mean "0 rows to prune").
    """
    resp = await client.head(
        url,
        headers={**_headers(), "Prefer": "count=exact", "Range": "0-0"},
        params=params,
    )
    if not resp.is_success:
        raise RuntimeError(
            f"count HEAD HTTP {resp.status_code}: {resp.text[:200]}"
        )
    return _parse_content_range_total(resp.headers.get("content-range"))


async def _delete_resolved_older_than(client: httpx.AsyncClient, cutoff_iso: str) -> int:
    """DELETE resolved nt_signal_sim rows with resolved_at < cutoff.

    Returns the number of rows deleted (best-effort; the pre-count is the
    upper bound — a live table may resolve more rows mid-pass, which is
    fine: they are caught by the next nightly run).
    """
    url = _url("nt_signal_sim")
    params = {
        "select": "signal_id",
        "resolved_at": f"lt.{cutoff_iso}",
        "outcome": "not.is.null",
    }
    total = await _count_rows(client, url, params)
    if total <= 0:
        return 0
    try:
        resp = await client.delete(
            url,
            headers={**_headers(), "Prefer": "count=exact"},
            params=params,
        )
        if not resp.is_success:
            logger.warning(
                "[eod:retention] DELETE resolved HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        deleted = _parse_content_range_total(resp.headers.get("content-range"))
        logger.info("[eod:retention] deleted %d resolved rows older than %s", deleted, cutoff_iso)
        return deleted if deleted > 0 else total
    except Exception as exc:
        logger.warning("[eod:retention] DELETE resolved failed: %s", exc)
        return 0


async def _expire_stale_pending(client: httpx.AsyncClient, cutoff_iso: str) -> int:
    """PATCH unresolved nt_signal_sim rows (ts < cutoff) to outcome='expired'.

    Returns the number of rows updated.
    """
    url = _url("nt_signal_sim")
    params = {
        "select": "signal_id",
        "outcome": "is.null",
        "ts": f"lt.{cutoff_iso}",
    }
    total = await _count_rows(client, url, params)
    if total <= 0:
        return 0
    try:
        resp = await client.patch(
            url,
            headers={**_headers(), "Prefer": "count=exact"},
            params=params,
            json={"outcome": "expired"},
        )
        if not resp.is_success:
            logger.warning(
                "[eod:retention] expire stale HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        expired = _parse_content_range_total(resp.headers.get("content-range"))
        logger.info("[eod:retention] expired %d stale pending rows older than %s", expired, cutoff_iso)
        return expired if expired > 0 else total
    except Exception as exc:
        logger.warning("[eod:retention] expire stale failed: %s", exc)
        return 0


async def run_eod_retention() -> dict[str, Any]:
    """Run the nightly retention pass over nt_signal_sim.

    Returns ``{deleted_resolved, expired_stale, errors}``. Never raises —
    every failure is caught, logged and counted in ``errors``.
    """
    summary: dict[str, Any] = {"deleted_resolved": 0, "expired_stale": 0, "errors": 0}
    if not settings.SUPABASE_URL:
        logger.warning("eod_retention: Supabase not configured — nothing to prune")
        return summary

    now_utc = datetime.now(timezone.utc)
    resolved_cutoff = (now_utc - timedelta(days=settings.EOD_RETENTION_RESOLVED_DAYS)).isoformat()
    stale_cutoff = (now_utc - timedelta(days=settings.EOD_RETENTION_STALE_DAYS)).isoformat()

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            summary["deleted_resolved"] = await _delete_resolved_older_than(client, resolved_cutoff)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[eod:retention] resolved-delete pass failed: %s", exc)

        try:
            summary["expired_stale"] = await _expire_stale_pending(client, stale_cutoff)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[eod:retention] stale-expire pass failed: %s", exc)

    logger.info(
        "[eod:retention] complete: deleted_resolved=%d expired_stale=%d errors=%d",
        summary["deleted_resolved"], summary["expired_stale"], summary["errors"],
    )
    return summary


async def main() -> None:
    """CLI entry: run the retention pass and print the summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = await run_eod_retention()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
