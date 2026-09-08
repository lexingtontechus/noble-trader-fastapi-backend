"""
HMM Shadow Accuracy — outcome-linked evidence for the Tier 1/2 live decision
════════════════════════════════════════════════════════════════════════════

worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md. ``nt_markov_source_shadow``
(Tier 1) / ``nt_trend_probs_gate_shadow`` (Tier 2) measure DISAGREEMENT
between the legacy and trend_hmm sources — they don't measure which one is
actually right, because neither links a comparison to what the trade did.
This module closes that gap using the 4 columns migration 131 added to
``nt_signal_sim`` (``legacy_markov_state``/``trend_hmm_markov_state``/
``raw_p_regime``/``gated_p_regime``, captured at signal time by both sweep
paths — see ``orchestrator.py``/``heavy_sweep.py``'s Tier 1/2 blocks).

READ-ONLY DIAGNOSTIC, same discipline as ``bracket_reachability.py``/
``calibration_bias_bucket.py``/``macro_edge.py``: reads win/loss-resolved
``nt_signal_sim`` rows, computes two independent metrics, upserts
``nt_hmm_shadow_accuracy``. Does NOT change ``markov_source_mode``/
``trend_probs_gate_mode`` — informs the 2026-09-21 decision review, does
not make it automatically.

  * **Tier 1** (``compute_tier1_accuracy``): per source (legacy /
    trend_hmm), ``coverage`` = % of rows where that source called
    non-FLAT, ``accuracy`` = hit rate against realized direction computed
    ONLY on that source's own non-FLAT calls — a FLAT call is "no
    answer," not a wrong answer, excluded from the accuracy tally (same
    rule used throughout this session for expired/FLAT populations).
  * **Tier 2** (``compute_tier2_brier``): per source (raw / gated),
    Brier score — ``(p_regime - realized)^2`` with ``realized ∈ {0,1}``
    (tp_hit=1, sl_hit=0) — reusing ``eod_resolver.py``'s existing
    ``calibration_brier`` convention, not a new metric. ``p_regime`` is
    already direction-adjusted by ``EVEngine.compute_p_regime`` (expresses
    "probability THIS signal's own direction wins"), so no separate
    buy/sell adjustment is needed here.

Usage (standalone):
  cd noble-trader-fastapi-backend
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.hmm_shadow_accuracy
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    upsert_hmm_shadow_accuracy,
)

logger = logging.getLogger("regime.sweep.hmm_shadow_accuracy")

_PAGE_SIZE = 1000


async def _fetch_all(table: str, select: str, filters: dict[str, str]) -> list[dict[str, Any]]:
    """Mirrors macro_edge.py's own local copy — each module keeps its own
    pagination helper rather than sharing one (existing convention in
    this codebase)."""
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    params = {"select": select, "order": "symbol.asc", "limit": str(_PAGE_SIZE), **filters}
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


def _realized_direction(signal: str, outcome: str) -> str | None:
    """"UP"/"DOWN" — which way price actually went, in the same UP/DOWN
    vocabulary the markov current_state columns use. ``None`` for an
    unrecognized signal/outcome pair (defensive; callers skip the row)."""
    if signal not in ("buy", "sell") or outcome not in ("tp_hit", "sl_hit"):
        return None
    hit_favorable = outcome == "tp_hit"
    if signal == "buy":
        return "UP" if hit_favorable else "DOWN"
    return "DOWN" if hit_favorable else "UP"


def compute_tier1_accuracy(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (symbol, source) — source in {legacy, trend_hmm} — coverage +
    accuracy against realized direction. Pure function, no I/O."""
    by_symbol_source: dict[tuple[str, str], dict[str, int]] = {}
    total_rows: dict[str, int] = {}

    for row in rows:
        symbol = row.get("symbol")
        realized = _realized_direction(row.get("signal"), row.get("outcome"))
        if symbol is None or realized is None:
            continue
        total_rows[symbol] = total_rows.get(symbol, 0) + 1

        for source_key, column in (("legacy", "legacy_markov_state"), ("trend_hmm", "trend_hmm_markov_state")):
            state = row.get(column)
            if not state:
                continue
            d = by_symbol_source.setdefault((symbol, source_key), {"n_calls": 0, "n_correct": 0})
            if state == "FLAT":
                continue
            d["n_calls"] += 1
            if state == realized:
                d["n_correct"] += 1

    output_rows: list[dict[str, Any]] = []
    for (symbol, source_key), d in by_symbol_source.items():
        n_calls = d["n_calls"]
        n_total = total_rows.get(symbol, 0)
        output_rows.append({
            "symbol": symbol,
            "metric": source_key,
            "n": n_calls,
            "coverage": round(n_calls / n_total, 4) if n_total else None,
            "accuracy": round(d["n_correct"] / n_calls, 4) if n_calls else None,
            "brier_score": None,
        })
    return output_rows


def compute_tier2_brier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (symbol, source) — source in {raw, gated} — Brier score
    against realized outcome. Pure function, no I/O."""
    accum: dict[tuple[str, str], dict[str, float]] = {}

    for row in rows:
        symbol = row.get("symbol")
        outcome = row.get("outcome")
        if symbol is None or outcome not in ("tp_hit", "sl_hit"):
            continue
        realized = 1.0 if outcome == "tp_hit" else 0.0

        for source_key, column in (("raw", "raw_p_regime"), ("gated", "gated_p_regime")):
            p_regime = row.get(column)
            if p_regime is None:
                continue
            d = accum.setdefault((symbol, source_key), {"n": 0, "sq_error_sum": 0.0})
            d["n"] += 1
            d["sq_error_sum"] += (float(p_regime) - realized) ** 2

    output_rows: list[dict[str, Any]] = []
    for (symbol, source_key), d in accum.items():
        n = d["n"]
        output_rows.append({
            "symbol": symbol,
            "metric": source_key,
            "n": n,
            "coverage": None,
            "accuracy": None,
            "brier_score": round(d["sq_error_sum"] / n, 6) if n else None,
        })
    return output_rows


async def build_hmm_shadow_accuracy(symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Compute + upsert the Tier 1/2 outcome-linked accuracy table.

    Population: win/loss-resolved nt_signal_sim rows with at least one of
    the 4 capture columns populated — same "expired excluded" rule as
    everywhere else this session.
    """
    filters: dict[str, str] = {"outcome": "in.(tp_hit,sl_hit)"}
    if symbols:
        filters["symbol"] = f"in.({','.join(symbols)})"

    try:
        rows = await _fetch_all(
            "nt_signal_sim",
            "symbol,signal,outcome,legacy_markov_state,trend_hmm_markov_state,"
            "raw_p_regime,gated_p_regime",
            filters,
        )
    except Exception as exc:
        logger.error("hmm_shadow_accuracy: fetch failed: %s", exc)
        return []
    if not rows:
        logger.info("hmm_shadow_accuracy: no win/loss-resolved rows found")
        return []

    output_rows = compute_tier1_accuracy(rows) + compute_tier2_brier(rows)

    # 2026-09-08 fix: compute_tier1_accuracy/compute_tier2_brier are pure
    # (no wall-clock dependency, by design, for testability) and never set
    # computed_at themselves -- it has to be stamped here, at the
    # orchestration point, right before the upsert. Without it the upsert
    # payload omits the column entirely, and an upsert-on-conflict never
    # touches a column absent from the payload -- identical bug class to
    # the 2026-09-02 nt_macro_regime.computed_at incident (confirmed live
    # 2026-09-08: the job re-ran and computed fresh rows per the log, but
    # every row's computed_at stayed frozen at its first-ever insert time).
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in output_rows:
        row["computed_at"] = now_iso

    try:
        await upsert_hmm_shadow_accuracy(output_rows)
    except Exception as exc:
        logger.error("hmm_shadow_accuracy: upsert failed: %s", exc)

    logger.info(
        "hmm_shadow_accuracy: computed %d row(s) across %d symbol(s)",
        len(output_rows), len({r["symbol"] for r in output_rows}),
    )
    return output_rows


def _print_report(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No hmm-shadow-accuracy rows computed.")
        return
    print(f"{'Symbol':<10} {'Metric':<10} {'n':>6} {'Coverage':>9} {'Accuracy':>9} {'Brier':>8}")
    print("-" * 60)
    for row in sorted(rows, key=lambda r: (r["symbol"], r["metric"])):
        cov = row["coverage"]
        acc = row["accuracy"]
        brier = row["brier_score"]
        print(
            f"{row['symbol']:<10} {row['metric']:<10} {row['n']:>6} "
            f"{f'{cov * 100:.1f}%' if cov is not None else '-':>9} "
            f"{f'{acc * 100:.1f}%' if acc is not None else '-':>9} "
            f"{f'{brier:.4f}' if brier is not None else '-':>8}"
        )


async def main() -> None:
    """CLI entry: build the hmm-shadow-accuracy table and print a text report."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = await build_hmm_shadow_accuracy()
    _print_report(rows)


if __name__ == "__main__":
    asyncio.run(main())
