"""
Sweep Result / Regime Log Retention — keep ``nt_sweep_result`` and
``nt_regime_log`` bounded
═══════════════════════════════════════════════════════════════════════

Root cause (2026-10-01, worklog
20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §7/§10/§11): a Supabase
table/index size audit found ``nt_sweep_result`` at 103.41 MB (17.09% of
the whole database) and ``nt_regime_log`` at 79.47 MB (13.13%) — both
append-only, written on every sweep tick (~174K/~178K inserts per
migration 177's 2026-09-27 note), with **no retention pass of any kind**.
Compare ``nt_signal_sim``, which got a dedicated nightly retention module
(``eod_retention.py``) back on 2026-08-12 when IT hit this same
unbounded-growth problem first — these two tables never got the
equivalent treatment.

A full-repo audit (same worklog §10) checked every replay/research script
plus every live app query path against both tables and confirmed: nothing
anywhere reads more than ~7-30 days back. Unlike ``nt_renko_bricks``
(whose retention is in genuine tension with correlation's need for 120
days of resampled daily closes, same worklog §4c), there is no such
conflict here — a plain age-cutoff prune is safe.

Neither table is referenced by another table's rows the way
``nt_signal_sim`` is protected from deletion by open
``nt_paper_positions.signal_id`` references (``eod_retention.py``'s
``_fetch_protected_signal_ids``) — no protect-list step is needed.

This module is the nightly hygiene pass (mirrors ``renko_retention.py`` /
``eod_retention.py``'s shape and design rules), run as Part VII of the
eod-lookback cycle (``orchestrator.py::_run_eod_lookback``, after the
existing Part VI renko brick retention):

  1. **nt_sweep_result** — delete rows with ``sweep_timestamp`` older than
     ``sweep_result_retention_days`` (``config/feature_flags.yaml``,
     default 30).
  2. **nt_regime_log** — delete rows with ``detected_at`` older than
     ``regime_log_retention_days`` (``config/feature_flags.yaml``,
     default 30).

Design rules (mirror eod_retention.py / renko_retention.py):
  • Best-effort only — every failure is caught, logged and counted in the
    summary; this module never raises out of the caller.
  • Each table's delete runs in its own try/except — one table failing
    does not prevent the other from running.
  • Uses PostgREST with ``Prefer: count=exact`` so the affected-row count is
    parsed from ``Content-Range`` (the 1,000-row cap applies to GET body
    rows; count=exact gives the true total via the header).

CLI:
    python -m regime_platform.sweep_orchestrator.sweep_result_retention
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from regime_platform.core.feature_flags import (
    get_regime_log_retention_days,
    get_sweep_result_retention_days,
)
from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.sweep_result_retention")


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


async def _delete_old_sweep_results(client: httpx.AsyncClient, cutoff_iso: str) -> int:
    """DELETE ``nt_sweep_result`` rows with ``sweep_timestamp`` < ``cutoff_iso``."""
    url = _url("nt_sweep_result")
    params = {"sweep_timestamp": f"lt.{cutoff_iso}", "select": "id"}
    try:
        resp = await client.delete(
            url, headers={**_headers(), "Prefer": "count=exact"}, params=params,
        )
        if not resp.is_success:
            logger.warning(
                "[sweep_result:retention] nt_sweep_result DELETE HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        return _parse_content_range_total(resp.headers.get("content-range"))
    except Exception as exc:
        logger.warning("[sweep_result:retention] nt_sweep_result DELETE failed: %s", exc)
        return 0


async def _delete_old_regime_log(client: httpx.AsyncClient, cutoff_iso: str) -> int:
    """DELETE ``nt_regime_log`` rows with ``detected_at`` < ``cutoff_iso``."""
    url = _url("nt_regime_log")
    params = {"detected_at": f"lt.{cutoff_iso}", "select": "id"}
    try:
        resp = await client.delete(
            url, headers={**_headers(), "Prefer": "count=exact"}, params=params,
        )
        if not resp.is_success:
            logger.warning(
                "[sweep_result:retention] nt_regime_log DELETE HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        return _parse_content_range_total(resp.headers.get("content-range"))
    except Exception as exc:
        logger.warning("[sweep_result:retention] nt_regime_log DELETE failed: %s", exc)
        return 0


async def run_sweep_result_retention() -> dict[str, Any]:
    """Run the nightly retention pass over ``nt_sweep_result`` + ``nt_regime_log``.

    Returns ``{deleted_sweep_result, deleted_regime_log, errors}``. Never
    raises — every failure is caught, logged and counted in ``errors``.
    """
    summary: dict[str, Any] = {
        "deleted_sweep_result": 0,
        "deleted_regime_log": 0,
        "errors": 0,
    }
    if not settings.SUPABASE_URL:
        logger.warning("sweep_result_retention: Supabase not configured — nothing to prune")
        return summary

    now_utc = datetime.now(timezone.utc)

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            cutoff_iso = (
                now_utc - timedelta(days=get_sweep_result_retention_days())
            ).isoformat()
            summary["deleted_sweep_result"] = await _delete_old_sweep_results(client, cutoff_iso)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[sweep_result:retention] nt_sweep_result pass failed: %s", exc)

        try:
            cutoff_iso = (
                now_utc - timedelta(days=get_regime_log_retention_days())
            ).isoformat()
            summary["deleted_regime_log"] = await _delete_old_regime_log(client, cutoff_iso)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[sweep_result:retention] nt_regime_log pass failed: %s", exc)

    logger.info(
        "[sweep_result:retention] complete: deleted_sweep_result=%d deleted_regime_log=%d errors=%d",
        summary["deleted_sweep_result"], summary["deleted_regime_log"], summary["errors"],
    )
    return summary


async def main() -> None:
    """CLI entry: run the retention pass and print the summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = await run_sweep_result_retention()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
