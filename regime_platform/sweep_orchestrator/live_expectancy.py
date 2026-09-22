"""
Live expectancy daily builder — P2 flip evidence for p_win_bracket (migration 163).

Cohorts on the QUALIFIED live book (outcome in tp_hit/sl_hit/expired):
  all_qualified   — product book as published
  bracket_accept  — p_win_bracket >= min_p_win_bracket (0.20)
  bracket_reject  — scored but below the gate (bracket_gate_shadow)

Flip rule (worklog 20260922_R2_ADDENDUM): hold modes at shadow until
bracket_accept shows expired%↓ AND expectancy_r↑/flat vs all_qualified
over ≥7d. This module snapshots that comparison daily so eod_retention
cannot erase the evidence.

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.live_expectancy
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    upsert_live_expectancy_daily,
)

logger = logging.getLogger("regime.sweep.live_expectancy")

_PAGE_SIZE = 1000
COHORT_ALL = "all_qualified"
COHORT_ACCEPT = "bracket_accept"
COHORT_REJECT = "bracket_reject"


async def _fetch_all(table: str, select: str, filters: dict[str, str]) -> list[dict[str, Any]]:
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    params = {
        "select": select,
        "order": "ts.asc",
        "limit": str(_PAGE_SIZE),
        **filters,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url(table), headers=_headers(), params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    return rows


def _day_key(ts: Any) -> str | None:
    if not ts:
        return None
    s = str(ts)
    return s[:10]


def _empty() -> dict[str, Any]:
    return {
        "n": 0, "n_tp": 0, "n_sl": 0, "n_expired": 0,
        "p_dir_sum": 0.0, "p_dir_n": 0,
        "p_br_sum": 0.0, "p_br_n": 0,
        "rr_sum": 0.0, "wins": 0,
    }


def _bump(d: dict[str, Any], row: dict[str, Any], outcome: str) -> None:
    d["n"] += 1
    if outcome == "tp_hit":
        d["n_tp"] += 1
        d["wins"] += 1
        tp = row.get("tp_bricks") or 0
        sl = row.get("sl_bricks") or 0
        if sl:
            d["rr_sum"] += float(tp) / float(sl)
    elif outcome == "sl_hit":
        d["n_sl"] += 1
    else:
        d["n_expired"] += 1
    p_dir = row.get("p_win_dir")
    if p_dir is None:
        p_dir = row.get("predicted_p_win_raw")
    if p_dir is None:
        p_dir = row.get("p_win")
    if p_dir is not None:
        d["p_dir_sum"] += float(p_dir)
        d["p_dir_n"] += 1
    p_br = row.get("p_win_bracket")
    if p_br is not None:
        d["p_br_sum"] += float(p_br)
        d["p_br_n"] += 1


def _finalize(d: dict[str, Any]) -> dict[str, Any]:
    n = d["n"]
    closed = d["n_tp"] + d["n_sl"]
    win_rate = (d["n_tp"] / closed) if closed else None
    avg_rr = (d["rr_sum"] / d["wins"]) if d["wins"] else None
    expectancy_r = (
        round(win_rate * (avg_rr or 0.0) - (1.0 - win_rate) * 1.0, 4)
        if win_rate is not None
        else None
    )
    avg_p_dir = (d["p_dir_sum"] / d["p_dir_n"]) if d["p_dir_n"] else None
    bias_dir = (
        round(avg_p_dir - win_rate, 4)
        if avg_p_dir is not None and win_rate is not None
        else None
    )
    return {
        "n": n,
        "n_tp": d["n_tp"],
        "n_sl": d["n_sl"],
        "n_expired": d["n_expired"],
        "win_rate_closed": round(win_rate, 4) if win_rate is not None else None,
        "pct_expired": round(d["n_expired"] / n, 4) if n else None,
        "expectancy_r": expectancy_r,
        "avg_p_dir": round(avg_p_dir, 4) if avg_p_dir is not None else None,
        "avg_p_win_bracket": (
            round(d["p_br_sum"] / d["p_br_n"], 4) if d["p_br_n"] else None
        ),
        "bias_dir": bias_dir,
    }


async def build_live_expectancy_daily(
    min_p_win_bracket: float = 0.20,
    days: int = 35,
) -> list[dict[str, Any]]:
    """Compute + upsert daily cohort metrics for the flip-evidence table."""
    since = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # PostgREST: use a wide window; days is advisory for callers that want a tail.
    filters = {
        "qualified": "eq.true",
        "outcome": "in.(tp_hit,sl_hit,expired)",
    }
    try:
        rows = await _fetch_all(
            "nt_signal_sim",
            "symbol,ts,outcome,p_win,p_win_dir,p_win_bracket,predicted_p_win_raw,"
            "tp_bricks,sl_bricks,bracket_gate_shadow",
            filters,
        )
    except Exception as exc:
        logger.error("live_expectancy: fetch failed: %s", exc)
        return []
    if not rows:
        logger.info("live_expectancy: no qualified resolved rows found")
        return []

    buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(_empty)
    for row in rows:
        day = _day_key(row.get("ts"))
        if not day:
            continue
        outcome = row.get("outcome")
        if outcome not in ("tp_hit", "sl_hit", "expired"):
            continue
        _bump(buckets[(day, COHORT_ALL)], row, outcome)
        p_br = row.get("p_win_bracket")
        if p_br is not None:
            cohort = COHORT_ACCEPT if float(p_br) >= float(min_p_win_bracket) else COHORT_REJECT
            _bump(buckets[(day, cohort)], row, outcome)

    output: list[dict[str, Any]] = []
    for (day, cohort), d in buckets.items():
        if d["n"] <= 0:
            continue
        metrics = _finalize(d)
        if metrics["n"] <= 0:
            continue
        output.append({
            "day": day,
            "cohort": cohort,
            **metrics,
        })

    if output:
        try:
            await upsert_live_expectancy_daily(output)
        except Exception as exc:
            logger.error("live_expectancy: upsert failed: %s", exc)

    by_cohort = {c: sum(r["n"] for r in output if r["cohort"] == c) for c in
                 (COHORT_ALL, COHORT_ACCEPT, COHORT_REJECT)}
    logger.info(
        "live_expectancy: wrote %d day×cohort row(s) (n all=%s accept=%s reject=%s) "
        "min_p_win_bracket=%.2f",
        len(output), by_cohort[COHORT_ALL], by_cohort[COHORT_ACCEPT],
        by_cohort[COHORT_REJECT], min_p_win_bracket,
    )
    return output


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(build_live_expectancy_daily())
