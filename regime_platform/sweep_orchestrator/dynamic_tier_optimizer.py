"""
dynamic_tier_optimizer.py — Pre-sweep dynamic tier optimization
═══════════════════════════════════════════════════════════════

Recomputes dynamic tier parameters before each light/heavy sweep so the
sweep uses fresh parameters instead of waiting for the nightly EOD pass.

Design principles (2026-10-07):
  • Same dataset as the nightly process (nt_signal_sim, last 10d, 48h resolution)
  • Same measurements (ATR buckets, vol tiers, regime labels)
  • Same windows (48h matching signal resolution)
  • Only the WHEN changes (pre-sweep vs nightly)

This module does NOT replace the nightly EOD lookback. It supplements it
by refreshing parameters more frequently. The nightly pass remains the
authoritative rebuild; this is a fast refresh between nightly passes.

Tiers optimized:
  1. Dynamic ATR (tiered_atr.walk) — TP/SL in ATR units
  2. Dynamic Deadline (dynamic_deadline) — per-(symbol, vol_tier) deadlines
  3. Regime Edge (regime_edge) — per-(symbol, signal, regime) edge buckets
  4. Geometry Incubation (geometry_incubation) — forward-test status

All tiers maintain shadow approach: parameters are computed and written
to nt_dynamic_tier_state, but the sweep only uses them when the
corresponding feature flag is live.

Usage (standalone):
  python -m regime_platform.sweep_orchestrator.dynamic_tier_optimizer

Called by orchestrator:
  await self._run_dynamic_tier_optimization()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import numpy as np

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import (
    _dedupe_episodes,
)
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    fetch_active_symbols,
)

logger = logging.getLogger("regime.sweep.dynamic_tier_optimizer")

_PAGE_SIZE = 1000
_LOOKBACK_DAYS = 10  # Same as dynamic_atr.lookback_days
_MIN_SIGNALS = 60    # Same as dynamic_atr.min_signals


# ─── Data fetch ──────────────────────────────────────────────────────────────

async def _fetch_resolved_rows(
    symbols: list[str] | None = None,
    *,
    lookback_days: int = _LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Fetch + episode-dedupe resolved nt_signal_sim rows.

    Same population as the nightly build_calibration_bias_by_bucket:
    - outcome IS NOT NULL (resolved)
    - contaminated = false (exclude artifacts)
    - within lookback window
    """
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    params: dict[str, str] = {
        "select": (
            "symbol,ts,signal,entry_price,take_profit,stop_loss,atr,"
            "outcome,resolved_at,regime,p_win,tp_bricks,sl_bricks"
        ),
        "outcome": "not.is.null",
        "contaminated": "eq.false",
        "ts": f"gte.{since}",
        "order": "ts.asc",
        "limit": str(_PAGE_SIZE),
    }
    if symbols:
        params["symbol"] = f"in.({','.join(symbols)})"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    return _dedupe_episodes(rows)


async def _fetch_reachability_rows(
    symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch latest nt_bracket_reachability rows (post-cutover, non-contaminated)."""
    if not settings.SUPABASE_URL:
        return []
    params: dict[str, str] = {
        "select": "symbol,vol_tier,distance_atr_bucket,signal,n,pct_hit_tp,pct_hit_sl,pct_expired,median_hours_to_resolve,p75_hours,p90_hours",
        "signal": "eq.all",
        "order": "symbol.asc",
        "limit": "500",
    }
    if symbols:
        params["symbol"] = f"in.({','.join(symbols)})"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params=params,
            )
        rows = resp.json() or []
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("dynamic_tier_optimizer: reachability fetch failed: %s", exc)
        return []


# ─── Tier 1: Dynamic ATR ────────────────────────────────────────────────────

async def _refresh_dynamic_atr(peers: list[dict[str, Any]]) -> dict[str, Any]:
    """Refresh the dynamic-ATR tier by calling the ONE picker.

    2026-10-09 (worklog 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md): this
    replaces the old ``_compute_dynamic_atr``, which bucketed stored brackets
    and produced a *diagnostic* (a bucket label + expectancy) that no consumer
    could use as geometry. The picker (``tiered_atr.dynamic_atr_pick``) is the
    single computation of ATR TP/SL; it now persists its own result to the
    ``dynamic_atr`` tier, and this function is the 15-min clock that calls it.

    Returns ``{symbol: pick}`` for the summary; the tier write happens inside
    the picker (``write_pick_to_store``). ``use_store=False`` forces a real
    recompute — without it the picker would read back its own previous write.
    """
    from regime_platform.core.feature_flags import (
        get_dynamic_atr_config,
        get_min_rr,
    )
    from regime_platform.sweep_orchestrator.tiered_atr import dynamic_atr_pick
    from regime_platform.core import param_store

    picks: dict[str, Any] = {}
    # Asset-class pooled fallback (2026-10-12, worklog
    # 20261012_POOLED_ATR_AND_SCORECARD_FILTER.md): the per-symbol solve fails
    # for 13/14 symbols (2-3 paths each < min_signals 80), so the dynamic-ATR
    # geometry never ships. Solve the pool ONCE per class (enough paths) and
    # let each symbol in that class inherit it when its own solve returns None.
    # Fail-open chain preserved: per-symbol -> pooled -> brick.
    from regime_platform.sweep_orchestrator.tiered_atr import asset_class_key
    pooled_cache: dict[str, Any] = {}

    async def _pooled_pick(asset_class: str | None):
        ac = asset_class_key(asset_class)
        if ac in pooled_cache:
            return pooled_cache[ac]
        try:
            pk = await dynamic_atr_pick(
                asset_class,
                cfg=get_dynamic_atr_config(asset_class, None),
                min_rr=get_min_rr(),
                symbol=None,          # pooled: walk the whole class
                use_store=False,
            )
        except Exception as exc:
            logger.warning(
                "dynamic_tier_optimizer: pooled atr %s failed: %s", ac, exc,
            )
            pk = None
        pooled_cache[ac] = pk
        if pk is not None:
            logger.info(
                "[dynamic:atr] pooled %s -> %s (inherited by symbols with no "
                "per-symbol pick)", ac, pk.as_log(),
            )
        return pk

    for p in peers:
        symbol = p.get("symbol")
        if not symbol or not p.get("tradingview_symbol"):
            continue
        # Drop legacy per-bucket rows (keys like "4+") from the pre-2026-10-09
        # shape — the tier now holds exactly one row (key "latest"). Runs even
        # when the pick is None, so a symbol with no qualifying bracket does
        # not keep unreachable rows alive as permanent parity drift.
        try:
            await asyncio.to_thread(
                param_store.delete_keys,
                symbol, param_store.TIER_DYNAMIC_ATR, ("latest",),
            )
        except Exception as exc:
            logger.debug("dynamic_tier_optimizer: atr prune %s failed: %s", symbol, exc)
        try:
            pick = await dynamic_atr_pick(
                p.get("asset_class"),
                cfg=get_dynamic_atr_config(p.get("asset_class"), symbol),
                min_rr=get_min_rr(),
                symbol=symbol,
                use_store=False,
            )
        except Exception as exc:
            logger.warning("dynamic_tier_optimizer: atr refresh %s failed: %s", symbol, exc)
            continue
        # Per-symbol solve returned None (too few paths) -> inherit the pooled
        # class pick so the symbol still ships harmonized ATR geometry.
        if pick is None:
            pick = await _pooled_pick(p.get("asset_class"))
            if pick is not None:
                # Persist the pooled pick under THIS symbol so the light path
                # (read_pick_from_store) finds it. Tag the source.
                try:
                    from regime_platform.sweep_orchestrator.tiered_atr import (
                        write_pick_to_store,
                    )
                    await write_pick_to_store(
                        symbol, pick,
                        source=f"pooled:{asset_class_key(p.get('asset_class'))}",
                    )
                except Exception as exc:
                    logger.debug(
                        "dynamic_tier_optimizer: pooled store %s failed: %s",
                        symbol, exc,
                    )
        if pick is not None:
            picks[symbol] = pick
    return picks


# ─── Tier 2: Dynamic Deadline ────────────────────────────────────────────────

def _compute_dynamic_deadline(
    reachability_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute dynamic deadline tiers from reachability percentiles.

    Same algorithm as dynamic_deadline.derive_deadline_tier() but
    operating on the latest reachability rows.
    """
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in reachability_rows:
        sym = r.get("symbol")
        vt = r.get("vol_tier", "unknown")
        if sym:
            by_key[(sym, vt)].append(r)

    tiers: dict[str, Any] = {}
    for (symbol, vol_tier), rows in by_key.items():
        # Use the "all" signal rows for pooled percentiles
        all_rows = [r for r in rows if r.get("signal") == "all"]
        if not all_rows:
            continue

        # Compute weighted percentiles
        total_n = sum(r.get("n", 0) for r in all_rows)
        if total_n < 20:
            continue

        # Weighted median hours
        hours_list = []
        for r in all_rows:
            med = r.get("median_hours_to_resolve")
            n = r.get("n", 0)
            if med is not None and n > 0:
                hours_list.extend([med] * n)
        if not hours_list:
            continue

        hours_arr = np.array(hours_list)
        p50 = float(np.percentile(hours_arr, 50))
        p75 = float(np.percentile(hours_arr, 75))
        p90 = float(np.percentile(hours_arr, 90))

        tiers[f"{symbol}:{vol_tier}"] = {
            "symbol": symbol,
            "vol_tier": vol_tier,
            "soft_deadline_hours": round(p75, 1),
            "hard_deadline_hours": round(p90, 1),
            "median_hours": round(p50, 1),
            "n": total_n,
            "confidence": "high" if total_n >= 50 else "medium" if total_n >= 20 else "low",
            "source": "dynamic_tier_optimizer",
        }

    return tiers


# ─── Tier 3: Regime Edge ────────────────────────────────────────────────────

def _compute_regime_edge(
    rows: list[dict[str, Any]],
    *,
    min_n: int = 20,
) -> dict[str, Any]:
    """Compute per-(symbol, signal, regime) edge from resolved signals.

    Same algorithm as regime_edge.compute_regime_edge() but operating
    on the latest resolved rows.
    """
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        outcome = r.get("outcome")
        if outcome not in ("tp_hit", "sl_hit"):
            continue
        symbol = r.get("symbol")
        signal = r.get("signal")
        regime = r.get("regime")
        if not symbol or not signal or not regime:
            continue
        key = (str(symbol), str(signal), str(regime))
        if key not in buckets:
            buckets[key] = {
                "symbol": str(symbol),
                "signal": str(signal),
                "regime": str(regime),
                "n": 0,
                "wins": 0,
                "losses": 0,
                "rr_sum": 0.0,
            }
        b = buckets[key]
        b["n"] += 1
        if outcome == "tp_hit":
            b["wins"] += 1
            tp_bricks = r.get("tp_bricks") or 0
            sl_bricks = r.get("sl_bricks") or 0
            if sl_bricks:
                b["rr_sum"] += float(tp_bricks) / float(sl_bricks)
        else:
            b["losses"] += 1

    edges: dict[str, Any] = {}
    for key, b in buckets.items():
        if b["n"] < min_n:
            continue
        win_rate = b["wins"] / b["n"]
        avg_rr = b["rr_sum"] / b["wins"] if b["wins"] else 0.0
        expectancy = win_rate * avg_rr - (1 - win_rate) * 1.0
        edges[f"{b['symbol']}:{b['signal']}:{b['regime']}"] = {
            "symbol": b["symbol"],
            "signal": b["signal"],
            "regime": b["regime"],
            "n": b["n"],
            "win_rate": round(win_rate, 4),
            "expectancy_r": round(expectancy, 4),
            "source": "dynamic_tier_optimizer",
        }

    return edges


# ─── Tier 4: Geometry Incubation ────────────────────────────────────────────

async def _refresh_geometry_incubation() -> dict[str, Any]:
    """Stamp the geometry-incubation tier by OBSERVING the real evaluator.

    2026-10-09 (worklog 20261009_DEPENDENCY_MAP_AND_HARMONIZATION_THESIS.md):
    this replaces ``_compute_geometry_incubation``, a second, divergent
    incubation engine that (a) duplicated ``evaluate_promotion``'s logic and
    (b) read ``row.get("n_forward_resolved")`` — a column that does not exist
    (the table column is ``n_resolved``), so it was always 0 and every verdict
    was permanently ``keep_incubating``.

    **This tier does not evaluate.** The single source of truth is
    ``geometry_incubation.reconcile_incubations``, owned by its own scheduler
    job (``geometry-incubation-reconcile``). Here we only READ the table that
    job writes, so the pre-sweep summary reflects the real verdicts. Calling
    reconcile from this 15-min job would run the promotion engine 96×/day and,
    in ``live`` mode, write ``brick_params.yaml`` from a pre-sweep tick — two
    owners for one write, the exact defect this work removes.
    """
    verdicts: dict[str, Any] = {}
    for row in await _fetch_incubation_rows():
        combo = (row.get("brick_size"), row.get("sl_bricks"), row.get("tp_bricks"))
        key = f"{row.get('symbol')}:{combo[0]}:{combo[1]}:{combo[2]}"
        verdicts[key] = {
            "status": row.get("status", "incubating"),
            "reason": row.get("reason"),
            "n_resolved": row.get("n_resolved", 0),
        }
    return verdicts


async def _fetch_incubation_rows() -> list[dict[str, Any]]:
    """Read current incubation rows (as written by the reconcile job)."""
    if not settings.SUPABASE_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_geometry_incubation"),
                headers=_headers(),
                params={
                    "select": "symbol,brick_size,sl_bricks,tp_bricks,status,"
                              "reason,n_resolved,pct_expired,baseline_pct_expired",
                    "status": "eq.incubating",
                    "order": "started_at.desc",
                    "limit": "100",
                },
            )
        rows = resp.json() or []
        return rows if isinstance(rows, list) else []
    except Exception as exc:
        logger.warning("dynamic_tier_optimizer: incubation read failed: %s", exc)
        return []


# ─── Main entry point ────────────────────────────────────────────────────────

async def run_dynamic_tier_optimization() -> dict[str, Any]:
    """Run all dynamic tier optimizations and write results to nt_dynamic_tier_state.

    Returns a summary of what was computed.
    Best-effort: never raises.
    """
    start = time.time()
    summary: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "symbols_checked": 0,
        "atr_picks": 0,
        "deadline_tiers": 0,
        "regime_edges": 0,
        "incubation_verdicts": 0,
        # Set below; pre-seeded so the key is always present (the ATR tier is
        # written by the picker, not this row list — see 2026-10-09 note).
        "state_rows_written": 0,
        "errors": [],
    }

    try:
        peers = [p for p in await fetch_active_symbols() if p.get("symbol")]
    except Exception as exc:
        summary["errors"].append(f"fetch_active_symbols: {exc}")
        logger.warning("dynamic_tier_optimizer: fetch_active_symbols failed: %s", exc)
        return summary

    symbols = [p["symbol"] for p in peers]
    summary["symbols_checked"] = len(symbols)

    # Fetch data (same dataset as nightly process)
    try:
        resolved_rows = await _fetch_resolved_rows(symbols)
    except Exception as exc:
        summary["errors"].append(f"fetch_resolved_rows: {exc}")
        logger.warning("dynamic_tier_optimizer: fetch_resolved_rows failed: %s", exc)
        resolved_rows = []

    try:
        reachability_rows = await _fetch_reachability_rows(symbols)
    except Exception as exc:
        summary["errors"].append(f"fetch_reachability_rows: {exc}")
        logger.warning("dynamic_tier_optimizer: fetch_reachability_rows failed: %s", exc)
        reachability_rows = []

    # Tier 1: Dynamic ATR — the picker is the one computation; this is the
    # clock that refreshes its persisted result (2026-10-09 single source).
    try:
        atr_picks = await _refresh_dynamic_atr(peers)
        summary["atr_picks"] = len(atr_picks)
    except Exception as exc:
        summary["errors"].append(f"refresh_dynamic_atr: {exc}")
        logger.warning("dynamic_tier_optimizer: refresh_dynamic_atr failed: %s", exc)
        atr_picks = {}

    # Tier 2: Dynamic Deadline
    try:
        deadline_tiers = _compute_dynamic_deadline(reachability_rows)
        summary["deadline_tiers"] = len(deadline_tiers)
    except Exception as exc:
        summary["errors"].append(f"compute_dynamic_deadline: {exc}")
        logger.warning("dynamic_tier_optimizer: compute_dynamic_deadline failed: %s", exc)
        deadline_tiers = {}

    # Tier 3: Regime Edge
    try:
        regime_edges = _compute_regime_edge(resolved_rows)
        summary["regime_edges"] = len(regime_edges)
    except Exception as exc:
        summary["errors"].append(f"compute_regime_edge: {exc}")
        logger.warning("dynamic_tier_optimizer: compute_regime_edge failed: %s", exc)
        regime_edges = {}

    # Tier 4: Geometry Incubation
    try:
        incubation_verdicts = await _refresh_geometry_incubation()
        summary["incubation_verdicts"] = len(incubation_verdicts)
    except Exception as exc:
        summary["errors"].append(f"refresh_geometry_incubation: {exc}")
        logger.warning("dynamic_tier_optimizer: refresh_geometry_incubation failed: %s", exc)
        incubation_verdicts = {}

    # Write results to the LOCAL param store (the runtime authority), then
    # mirror to Supabase (durable audit trail, best-effort). 2026-10-08
    # single-source-of-truth fix (worklog 20261008_PARAM_SOURCE_OF_TRUTH.md):
    # an optimization process must NOT retrieve its own parameters back out
    # of a DB to run. Local SQLite is the authority the sweep reads; the
    # nt_dynamic_tier_state table is a mirror, never a read path.
    try:
        state_rows = []
        now_iso = datetime.now(timezone.utc).isoformat()

        # dynamic_atr is NOT assembled here (2026-10-09, worklog
        # 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md): the picker
        # (tiered_atr.dynamic_atr_pick) writes its own usable-geometry row to
        # the tier via write_pick_to_store(), so the optimizer must not write
        # a second, differently-shaped row for the same key.

        for key, tier in deadline_tiers.items():
            state_rows.append({
                "symbol": tier["symbol"],
                "tier_type": "dynamic_deadline",
                "tier_key": tier["vol_tier"],
                # JSON object: the sweep needs soft+hard+median+confidence to
                # build a DeadlineTier without a network round-trip.
                "tier_value": {
                    "soft_deadline_hours": tier["soft_deadline_hours"],
                    "hard_deadline_hours": tier["hard_deadline_hours"],
                    "median_hours": tier["median_hours"],
                    "confidence": tier["confidence"],
                },
                "n": tier["n"],
                "computed_at": now_iso,
                "source": "dynamic_tier_optimizer",
            })

        for key, edge in regime_edges.items():
            state_rows.append({
                "symbol": edge["symbol"],
                "tier_type": "regime_edge",
                "tier_key": f"{edge['signal']}:{edge['regime']}",
                # JSON object so the local store carries BOTH win_rate and
                # expectancy_r — the regime_edge gate needs win_rate, the
                # ranking path needs expectancy_r. One row, both fields.
                "tier_value": {
                    "win_rate": edge.get("win_rate"),
                    "expectancy_r": edge.get("expectancy_r"),
                },
                "n": edge["n"],
                "computed_at": now_iso,
                "source": "dynamic_tier_optimizer",
            })

        for key, verdict in incubation_verdicts.items():
            state_rows.append({
                "symbol": key.split(":")[0],
                "tier_type": "geometry_incubation",
                "tier_key": key,
                "tier_value": verdict.get("status", "unknown"),
                "n": verdict.get("n_forward_resolved", 0),
                "computed_at": now_iso,
                "source": "dynamic_tier_optimizer",
            })

        if state_rows:
            from regime_platform.core import param_store

            written = await param_store.write_params(state_rows)
            summary["state_rows_written"] = written
            # Mirror to Supabase (audit only — never blocks the sweep).
            mirror = await param_store.sync_to_supabase()
            summary["mirrored_to_supabase"] = mirror.get("mirrored", 0)

    except Exception as exc:
        summary["errors"].append(f"param_store write: {exc}")
        logger.warning("dynamic_tier_optimizer: param_store write failed: %s", exc)

    summary["duration_ms"] = round((time.time() - start) * 1000)
    logger.info(
        "dynamic_tier_optimizer: done — atr=%d deadline=%d edge=%d incubation=%d errors=%d",
        summary["atr_picks"], summary["deadline_tiers"],
        summary["regime_edges"], summary["incubation_verdicts"],
        len(summary["errors"]),
    )
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = asyncio.run(run_dynamic_tier_optimization())
    print(result)
