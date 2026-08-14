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


# URL-safety constants for the resolved-delete pass (2026-08-13).
# A single ``not.in.(1000 UUIDs)`` protect-list URL (~38KB) exceeds the
# Supabase gateway URL limit and 400s every nightly run. The protected path
# uses small keyset pages + ``in.(...)`` delete chunks instead.
_DELETE_PAGE = 1000   # resolved rows fetched per keyset page
_DELETE_CHUNK = 100   # signal_ids per DELETE in.(...) call


async def _fetch_protected_signal_ids(client: httpx.AsyncClient) -> set[str]:
    """signal_ids referenced by OPEN nt_paper_positions (never delete these)."""
    protected: set[str] = set()
    try:
        pos_url = f"{settings.SUPABASE_URL}/rest/v1/nt_paper_positions"
        pos_params = {"select": "signal_id", "status": "eq.open", "limit": "1000"}
        resp = await client.get(pos_url, headers=_headers(), params=pos_params)
        if resp.is_success:
            for row in resp.json() or []:
                sid = row.get("signal_id")
                if sid:
                    protected.add(str(sid))
    except Exception as exc:
        logger.warning("[eod:retention] open-position protection query failed: %s", exc)
    return protected


async def _delete_resolved_older_than(client: httpx.AsyncClient, cutoff_iso: str) -> int:
    """DELETE resolved nt_signal_sim rows with resolved_at < cutoff.

    Skips rows whose signal_id still has an OPEN nt_paper_positions row —
    deleting the resolved signal would orphan the position (its close pass
    matches on signal_id) and leave it unclosable (M4).

    Two paths:
      * No open positions → single HEAD count + one DELETE (small URL).
      * Open positions exist → the protect list can reach 1000 UUIDs and a
        single ``not.in.(...)`` URL 400s (observed nightly 2026-08-13). The
        protected path keyset-paginates the resolved rows (stable under
        concurrent deletes), filters protected ids client-side, and deletes
        in small ``in.(...)`` chunks.

    Returns the number of rows deleted (best-effort; the pre-count is the
    upper bound — a live table may resolve more rows mid-pass, which is
    fine: they are caught by the next nightly run).
    """
    url = _url("nt_signal_sim")
    protected = await _fetch_protected_signal_ids(client)

    if not protected:
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

    # Protected path: keyset pagination + chunked deletes.
    logger.info(
        "[eod:retention] protecting %d signal_id(s) referenced by open positions",
        len(protected),
    )
    deleted_total = 0
    last_id: str | None = None
    while True:
        params: dict[str, Any] = {
            "select": "signal_id",
            "resolved_at": f"lt.{cutoff_iso}",
            "outcome": "not.is.null",
            "order": "signal_id.asc",
            "limit": str(_DELETE_PAGE),
        }
        if last_id is not None:
            params["signal_id"] = f"gt.{last_id}"
        try:
            resp = await client.get(url, headers=_headers(), params=params)
        except Exception as exc:
            logger.warning("[eod:retention] delete-page GET failed: %s", exc)
            return deleted_total
        if not resp.is_success:
            logger.warning(
                "[eod:retention] delete-page GET HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return deleted_total
        rows = resp.json() or []
        if not rows:
            break
        last_id = str(rows[-1].get("signal_id"))
        to_delete = [
            str(r.get("signal_id")) for r in rows
            if r.get("signal_id") and str(r.get("signal_id")) not in protected
        ]
        for i in range(0, len(to_delete), _DELETE_CHUNK):
            chunk = to_delete[i:i + _DELETE_CHUNK]
            try:
                dresp = await client.delete(
                    url,
                    headers={**_headers(), "Prefer": "count=exact"},
                    params={"select": "signal_id", "signal_id": f"in.({','.join(chunk)})"},
                )
            except Exception as exc:
                logger.warning("[eod:retention] DELETE chunk failed: %s", exc)
                continue
            if not dresp.is_success:
                logger.warning(
                    "[eod:retention] DELETE chunk HTTP %d — %s",
                    dresp.status_code, dresp.text[:200],
                )
                continue
            n = _parse_content_range_total(dresp.headers.get("content-range"))
            deleted_total += n if n > 0 else len(chunk)
        if len(rows) < _DELETE_PAGE:
            break
    logger.info("[eod:retention] deleted %d resolved rows older than %s", deleted_total, cutoff_iso)
    return deleted_total


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
