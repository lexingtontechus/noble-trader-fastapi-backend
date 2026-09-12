"""
Phase 2 detector orchestration (worklog
20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS2/SS11,
self_learning_phase_2_detectors, RM-37).

Runs all three Phase 2 detectors against real Supabase data and writes
any findings to ``nt_self_learning_findings``. Diagnostic-only:
nothing here reads its own output to make a live decision, and nothing
downstream reads ``nt_self_learning_findings`` yet either — this is
for human review (or a future Phase 3 LLM-assisted triage, not built).

Detector 1 (regime-vs-realized-price mismatch) runs per-symbol against
``nt_sweep_result``. Detector 2 (dead-mechanism) runs against a
registry of this codebase's mode-gated mechanisms and their per-symbol
tables. Detector 3 (redundant-computation) runs against the existing
shadow-compare tables' own accumulated agreement statistics.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from regime_platform.core.feature_flags import (
    get_context_alignment_gate_mode,
    get_heavy_sweep_atr_anchored_bricks_mode,
    get_heavy_sweep_deadline_aware_scoring_mode,
    get_heavy_sweep_reachability_mode,
    get_macro_regime_mode,
    get_regime_label_debounce_mode,
    get_signal_stream_meta_model_mode,
    get_trend_regime_mode,
)
from regime_platform.services.self_learning.dead_mechanism_detector import (
    detect_dead_mechanism,
)
from regime_platform.services.self_learning.redundant_computation_detector import (
    detect_redundant_computation,
)
from regime_platform.services.self_learning.regime_price_mismatch import (
    detect_regime_price_mismatch,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_active_symbols,
    fetch_all_rows,
    insert_self_learning_finding,
)

logger = logging.getLogger("regime.self_learning.run_detectors")

# ── Detector 2 registry: mode-gated mechanism -> its per-symbol table ──
# Deliberately excludes context_alignment_gate_mode/dynamic_min_rr_mode/
# timesfm_k_adaptive_mode -- those are log-only (no dedicated table),
# a known, documented limitation (see worklog SS11), not an oversight.
_DEAD_MECHANISM_REGISTRY: list[dict[str, Any]] = [
    {"mechanism": "macro_regime_mode", "mode_getter": get_macro_regime_mode,
     "table": "nt_macro_regime", "ts_column": "computed_at", "max_staleness": timedelta(hours=26)},
    {"mechanism": "trend_regime_mode", "mode_getter": get_trend_regime_mode,
     "table": "nt_trend_regime", "ts_column": "computed_at", "max_staleness": timedelta(hours=26)},
    {"mechanism": "regime_label_debounce_mode", "mode_getter": get_regime_label_debounce_mode,
     "table": "nt_regime_debounce_shadow", "ts_column": "updated_at", "max_staleness": timedelta(hours=6)},
    {"mechanism": "signal_stream_meta_model_mode", "mode_getter": get_signal_stream_meta_model_mode,
     "table": "nt_signal_stream_meta_shadow", "ts_column": "updated_at", "max_staleness": timedelta(hours=6)},
    {"mechanism": "heavy_sweep_reachability_mode", "mode_getter": get_heavy_sweep_reachability_mode,
     "table": "nt_heavy_sweep_reachability_shadow", "ts_column": "updated_at", "max_staleness": timedelta(hours=30)},
    {"mechanism": "heavy_sweep_atr_anchored_bricks_mode", "mode_getter": get_heavy_sweep_atr_anchored_bricks_mode,
     "table": "nt_heavy_sweep_atr_anchored_shadow", "ts_column": "updated_at", "max_staleness": timedelta(hours=30)},
    {"mechanism": "heavy_sweep_deadline_aware_scoring_mode", "mode_getter": get_heavy_sweep_deadline_aware_scoring_mode,
     "table": "nt_heavy_sweep_deadline_aware_shadow", "ts_column": "updated_at", "max_staleness": timedelta(hours=30)},
]

# ── Detector 3 registry: shadow-compare tables with an n_compared +
# a disagreement-count column already accumulated by the live sweep. ──
_REDUNDANCY_REGISTRY: list[dict[str, Any]] = [
    {"pair_name": "markov_source (legacy vs trend_hmm)", "table": "nt_markov_source_shadow",
     "disagree_column": "n_state_disagree"},
    {"pair_name": "regime_label_debounce (raw vs debounced)", "table": "nt_regime_debounce_shadow",
     "disagree_column": "n_differed"},
    {"pair_name": "heavy_sweep_atr_anchored (baseline vs anchored)", "table": "nt_heavy_sweep_atr_anchored_shadow",
     "disagree_column": "n_changed"},
    {"pair_name": "heavy_sweep_deadline_aware (uncapped vs corrected)", "table": "nt_heavy_sweep_deadline_aware_shadow",
     "disagree_column": "n_changed"},
    {"pair_name": "heavy_sweep_reachability (baseline vs filtered)", "table": "nt_heavy_sweep_reachability_shadow",
     "disagree_column": "n_pick_changed"},
]

_REGIME_MISMATCH_LOOKBACK_DAYS = 21


async def _run_regime_price_mismatch_detector() -> list[dict[str, Any]]:
    """Detector 1 against every active symbol's real nt_sweep_result
    history, aggregated to daily rows."""
    findings: list[dict[str, Any]] = []
    symbols = await fetch_active_symbols()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_REGIME_MISMATCH_LOOKBACK_DAYS)).isoformat()

    raw_rows = await fetch_all_rows(
        "nt_sweep_result",
        "symbol,sweep_timestamp,entry_price,regime,regime_conf",
        filters={"entry_price": "not.is.null", "sweep_timestamp": f"gt.{cutoff}"},
        order="sweep_timestamp.asc",
        max_rows=100_000,
    )

    by_symbol_day: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        day = str(r["sweep_timestamp"])[:10]
        by_symbol_day[(r["symbol"], day)].append(r)

    daily_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (symbol, day), day_rows in sorted(by_symbol_day.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        day_rows_sorted = sorted(day_rows, key=lambda r: r["sweep_timestamp"])
        regime_counts: dict[str, int] = defaultdict(int)
        conf_sum = 0.0
        for r in day_rows_sorted:
            regime_counts[r["regime"]] += 1
            conf_sum += r["regime_conf"] or 0.0
        dominant_regime = max(regime_counts.items(), key=lambda kv: kv[1])[0]
        daily_by_symbol[symbol].append({
            "day": day,
            "regime_label": dominant_regime,
            "regime_conf": conf_sum / len(day_rows_sorted),
            "day_open": day_rows_sorted[0]["entry_price"],
            "day_close": day_rows_sorted[-1]["entry_price"],
        })

    active_symbols = {s["symbol"] for s in symbols}
    for symbol, daily_rows in daily_by_symbol.items():
        if symbol not in active_symbols:
            continue
        result = detect_regime_price_mismatch(daily_rows)
        if result is None:
            continue
        start_day = daily_rows[result["start_index"]]["day"]
        end_day = daily_rows[result["end_index"]]["day"]
        worst_day = daily_rows[result["worst_day_index"]]["day"]
        findings.append({
            "detector": "regime_price_mismatch",
            "symbol": symbol,
            "summary": (
                f"{symbol}: regime read {result['regime_direction']} for "
                f"{result['run_length_days']}d ({start_day}..{end_day}, conf="
                f"{result['avg_regime_conf']:.2f}) while price moved "
                f"{result['worst_price_move_pct']:+.2%} against it (worst day {worst_day})"
            ),
            "details": {**result, "start_day": start_day, "end_day": end_day, "worst_day": worst_day},
        })
    return findings


async def _run_dead_mechanism_detector() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for entry in _DEAD_MECHANISM_REGISTRY:
        mode = entry["mode_getter"]()
        rows = await fetch_all_rows(entry["table"], f"symbol,{entry['ts_column']}")
        by_symbol = {r["symbol"]: r.get(entry["ts_column"]) for r in rows}

        # Mechanism-wide: never written ANY row at all while shadow/live.
        if not rows:
            result = detect_dead_mechanism(entry["mechanism"], mode, None, now, entry["max_staleness"])
            if result:
                findings.append({
                    "detector": "dead_mechanism", "symbol": None,
                    "summary": f"{entry['mechanism']}: {mode} but {entry['table']} has zero rows",
                    "details": result,
                })
            continue

        # Per-symbol staleness within an otherwise-populated table.
        most_recent = max(
            (v for v in by_symbol.values() if v), default=None,
        )
        last_write_at = (
            datetime.fromisoformat(most_recent.replace("Z", "+00:00"))
            if most_recent else None
        )
        result = detect_dead_mechanism(entry["mechanism"], mode, last_write_at, now, entry["max_staleness"])
        if result:
            findings.append({
                "detector": "dead_mechanism", "symbol": None,
                "summary": f"{entry['mechanism']}: {mode}, {entry['table']} {result['reason']} (last write {result['last_write_at']})",
                "details": result,
            })
    return findings


async def _run_redundant_computation_detector() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for entry in _REDUNDANCY_REGISTRY:
        rows = await fetch_all_rows(entry["table"], f"n_compared,{entry['disagree_column']}")
        n_compared = sum(int(r.get("n_compared") or 0) for r in rows)
        n_disagree = sum(int(r.get(entry["disagree_column"]) or 0) for r in rows)
        result = detect_redundant_computation(entry["pair_name"], n_compared, n_disagree)
        if result:
            findings.append({
                "detector": "redundant_computation", "symbol": None,
                "summary": (
                    f"{entry['pair_name']}: {result['agreement_rate']:.1%} agreement "
                    f"over {n_compared} comparisons — consider consolidating"
                ),
                "details": result,
            })
    return findings


async def run_all_detectors() -> dict[str, Any]:
    """Run all three Phase 2 detectors, write any findings to
    ``nt_self_learning_findings``, return a summary. Best-effort per
    detector — one detector's failure does not block the others.
    """
    summary: dict[str, Any] = {"regime_price_mismatch": 0, "dead_mechanism": 0, "redundant_computation": 0, "errors": 0}

    for name, runner in (
        ("regime_price_mismatch", _run_regime_price_mismatch_detector),
        ("dead_mechanism", _run_dead_mechanism_detector),
        ("redundant_computation", _run_redundant_computation_detector),
    ):
        try:
            findings = await runner()
        except Exception as exc:
            logger.error("[self-learning] detector %s failed: %s: %r", name, type(exc).__name__, exc)
            summary["errors"] += 1
            continue
        for f in findings:
            await insert_self_learning_finding(
                detector=f["detector"], summary=f["summary"],
                symbol=f.get("symbol"), details=f.get("details"),
            )
        summary[name] = len(findings)

    return summary
