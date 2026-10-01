"""
Renko Brick Retention — keep ``nt_renko_bricks`` bounded to live grids
═══════════════════════════════════════════════════════════════════════

Root cause (2026-09-09, worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7 "Finding 2"):
every light-sweep tick rebuilds the ENTIRE brick series from the current
price window and upserts it keyed on ``(symbol, brick_index, session_date)``
— with no ``brick_size`` in that key (see migration that added it alongside
this module). Heavy sweep now re-optimizes ``brick_size`` DAILY (migration
``heavy_sweep_daily_cadence``), so a symbol's grid geometry can change every
day; every previous ``brick_size``'s rows for that symbol simply sit in the
table forever with nothing to ever clean them up. Confirmed live 2026-09-09:
129,583 rows across 14 symbols, oldest ``session_date`` a full month back
(e.g. AAPL alone carrying 8+ distinct historical ``brick_size`` values).

**Second, previously-undiagnosed root cause, confirmed 2026-10-01 (worklog
20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.0/§4b):** the symbol
roster this module used to drive the stale-brick_size pass was discovered
by scanning ``nt_renko_bricks`` itself — ``select=symbol&limit=10000`` with
no ``order``. This Supabase project caps every PostgREST GET response at
1000 rows regardless of the requested ``limit``. Confirmed live: with
924,375 rows spread unevenly across 14 symbols, those 1000 unordered rows
covered only 2 of 14 active symbols (``AAPL``, ``AUDUSD``) — the other 12
received **zero stale-brick_size pruning, every single night, since this
module shipped**, which is the dominant driver of the table's row count
(e.g. ``US30USD`` alone: 145 distinct historical ``brick_size`` grids,
102,306 rows, confirmed live). Fixed by sourcing the roster from
``fetch_active_symbols()`` (``supabase_client.py`` — reads ``nt_symbol
WHERE active = true``, the same roster source every other orchestrator
pass already uses) instead of scanning the very table being pruned.

This module is the nightly hygiene pass (mirrors ``eod_retention.py``'s
shape and design rules exactly), run as Part VII of the eod-lookback cycle
(``orchestrator.py::_run_eod_lookback``, after the new Part VI daily-close
snapshot — see ``daily_close_snapshot.py``'s module docstring for why that
ordering matters):

  1. **Stale brick_size grids** — for each symbol currently present in the
     table, delete every row whose ``brick_size`` does NOT match the
     symbol's CURRENT ``brick_size`` (``config/brick_params.yaml`` via
     ``load_brick_params`` — the same source of truth the light/heavy sweep
     themselves read). A mismatched grid is guaranteed orphaned: nothing
     will ever upsert onto it again, because every future sweep tick writes
     under the live YAML value.
  2. **Stale sessions** — delete rows older than
     ``renko_brick_retention_days`` (``config/feature_flags.yaml``,
     default 2 — the same "today + yesterday" steady state migration 111
     established for this exact table: "Keep at most 2 sessions ... so the
     chart's 10-brick window is always the latest session"). Catches rows
     that DO match the current brick_size but are simply old.

Design rules (mirror eod_retention.py):
  • Best-effort only — every failure is caught, logged and counted in the
    summary; this module never raises out of the caller.
  • Uses PostgREST with ``Prefer: count=exact`` so the affected-row count is
    parsed from ``Content-Range`` (the 1,000-row cap applies to GET body
    rows; count=exact gives the true total via the header).

CLI:
    python -m regime_platform.sweep_orchestrator.renko_retention
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from regime_platform.core.feature_flags import get_renko_brick_retention_days
from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    fetch_active_symbols,
)

logger = logging.getLogger("regime.sweep.renko_retention")


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


async def _distinct_symbols() -> list[str]:
    """Active symbol roster to drive the stale-brick_size pass (best-effort).

    Sourced from ``fetch_active_symbols()`` (``nt_symbol WHERE active =
    true``) — NOT from scanning ``nt_renko_bricks`` itself. See this
    module's docstring for why: scanning the brick table directly hits
    this project's 1000-row PostgREST GET cap and silently truncates the
    roster to whichever symbols happen to sort first in an unordered scan,
    leaving every symbol outside that window permanently unpruned.
    """
    try:
        rows = await fetch_active_symbols()
        return sorted({r.get("symbol") for r in rows if r.get("symbol")})
    except Exception as exc:
        logger.warning("[renko:retention] active-symbol roster fetch failed: %s", exc)
        return []


async def _delete_stale_brick_size(client: httpx.AsyncClient, symbol: str, current_brick_size: float) -> int:
    """DELETE rows for ``symbol`` whose ``brick_size`` != the live YAML value.

    Uses a small numeric tolerance (relative, not absolute — brick_size
    spans orders of magnitude across symbols, e.g. AUDUSD ~0.00004 vs.
    BTC ~250) so a float round-trip through PostgREST/JSON never causes a
    row matching the live config to be misclassified as stale.
    """
    url = _url("nt_renko_bricks")
    # PostgREST has no "not approximately equal" operator, so express the
    # tolerance band as two half-open ranges and delete rows OUTSIDE it via
    # `or=(brick_size.lt.LOW,brick_size.gt.HIGH)`. Tolerance: 0.1% of the
    # current value (or a tiny absolute floor for the near-zero case) —
    # this only needs to survive float round-tripping, not represent a
    # real config-tolerance decision.
    tol = max(abs(current_brick_size) * 1e-3, 1e-9)
    low = current_brick_size - tol
    high = current_brick_size + tol
    # Format as fixed-point, not Python's default repr — brick_size can be
    # as small as ~0.00004 (AUDUSD), and Python's default float-to-str
    # switches to scientific notation ("3.996e-05") below 1e-4, which
    # PostgREST's numeric filter parser is not guaranteed to accept in a
    # query-string literal. 10 decimal places comfortably covers every
    # brick_size currently seen live (smallest ~4e-5, largest ~2000).
    params = {
        "symbol": f"eq.{symbol}",
        "or": f"(brick_size.lt.{low:.10f},brick_size.gt.{high:.10f})",
        "select": "brick_id",
    }
    try:
        resp = await client.delete(
            url, headers={**_headers(), "Prefer": "count=exact"}, params=params,
        )
        if not resp.is_success:
            logger.warning(
                "[renko:retention] stale-brick_size DELETE %s HTTP %d — %s",
                symbol, resp.status_code, resp.text[:200],
            )
            return 0
        return _parse_content_range_total(resp.headers.get("content-range"))
    except Exception as exc:
        logger.warning("[renko:retention] stale-brick_size DELETE %s failed: %s", symbol, exc)
        return 0


async def _delete_old_sessions(client: httpx.AsyncClient, cutoff_date: str) -> int:
    """DELETE rows with ``session_date`` older than ``cutoff_date`` (YYYY-MM-DD)."""
    url = _url("nt_renko_bricks")
    params = {"session_date": f"lt.{cutoff_date}", "select": "brick_id"}
    try:
        resp = await client.delete(
            url, headers={**_headers(), "Prefer": "count=exact"}, params=params,
        )
        if not resp.is_success:
            logger.warning(
                "[renko:retention] old-session DELETE HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return 0
        return _parse_content_range_total(resp.headers.get("content-range"))
    except Exception as exc:
        logger.warning("[renko:retention] old-session DELETE failed: %s", exc)
        return 0


async def run_renko_brick_retention() -> dict[str, Any]:
    """Run the nightly retention pass over ``nt_renko_bricks``.

    Returns ``{deleted_stale_brick_size, deleted_old_sessions, symbols_checked,
    errors}``. Never raises — every failure is caught, logged and counted in
    ``errors``.
    """
    summary: dict[str, Any] = {
        "deleted_stale_brick_size": 0,
        "deleted_old_sessions": 0,
        "symbols_checked": 0,
        "errors": 0,
    }
    if not settings.SUPABASE_URL:
        logger.warning("renko_retention: Supabase not configured — nothing to prune")
        return summary

    from regime_platform.config.brick_params import load_brick_params

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            symbols = await _distinct_symbols()
            summary["symbols_checked"] = len(symbols)
            for symbol in symbols:
                try:
                    current_brick_size, _sl, _tp = load_brick_params(symbol)
                    deleted = await _delete_stale_brick_size(client, symbol, current_brick_size)
                    summary["deleted_stale_brick_size"] += deleted
                except Exception as exc:
                    summary["errors"] += 1
                    logger.error(
                        "[renko:retention] stale-brick_size pass failed for %s: %s",
                        symbol, exc,
                    )
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[renko:retention] stale-brick_size pass failed: %s", exc)

        try:
            cutoff_date = (
                datetime.now(timezone.utc) - timedelta(days=get_renko_brick_retention_days())
            ).strftime("%Y-%m-%d")
            summary["deleted_old_sessions"] = await _delete_old_sessions(client, cutoff_date)
        except Exception as exc:
            summary["errors"] += 1
            logger.error("[renko:retention] old-session pass failed: %s", exc)

    logger.info(
        "[renko:retention] complete: deleted_stale_brick_size=%d deleted_old_sessions=%d "
        "symbols_checked=%d errors=%d",
        summary["deleted_stale_brick_size"], summary["deleted_old_sessions"],
        summary["symbols_checked"], summary["errors"],
    )
    return summary


async def main() -> None:
    """CLI entry: run the retention pass and print the summary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = await run_renko_brick_retention()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
