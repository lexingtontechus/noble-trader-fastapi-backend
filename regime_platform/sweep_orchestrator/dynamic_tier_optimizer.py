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
    _bucket_for,
    _dedupe_episodes,
    _hours_between,
    _signal_side,
    _vol_tier,
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

def _compute_dynamic_atr(
    rows: list[dict[str, Any]],
    *,
    lookback_days: int = _LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Compute dynamic ATR pick per symbol from resolved signals.

    Same algorithm as tiered_atr.walk() but operating on the full
    symbol population (not per-class). Returns per-symbol picks.
    """
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sym = r.get("symbol")
        if sym:
            by_symbol[sym].append(r)

    picks: dict[str, Any] = {}
    for symbol, sym_rows in by_symbol.items():
        if len(sym_rows) < _MIN_SIGNALS:
            continue

        # Compute ATR for each row (use stored ATR, skip if missing)
        atr_rows = [r for r in sym_rows if r.get("atr")]
        if len(atr_rows) < _MIN_SIGNALS:
            continue

        # Bucket by distance/ATR
        bucket_data: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"n": 0, "tp": 0, "sl": 0, "exp": 0, "rr_sum": 0.0, "hours": []}
        )
        for r in atr_rows:
            entry = r.get("entry_price")
            tp = r.get("take_profit")
            outcome = r.get("outcome")
            if entry is None or tp is None or outcome not in ("tp_hit", "sl_hit", "expired"):
                continue
            atr = float(r["atr"])
            ratio = abs(float(tp) - float(entry)) / atr
            bucket = _bucket_for(ratio)
            if bucket is None:
                continue
            d = bucket_data[bucket]
            d["n"] += 1
            if outcome == "tp_hit":
                d["tp"] += 1
                tp_bricks = r.get("tp_bricks") or 0
                sl_bricks = r.get("sl_bricks") or 0
                if sl_bricks:
                    d["rr_sum"] += float(tp_bricks) / float(sl_bricks)
            elif outcome == "sl_hit":
                d["sl"] += 1
            else:
                d["exp"] += 1
            if outcome in ("tp_hit", "sl_hit"):
                hrs = _hours_between(r.get("ts"), r.get("resolved_at"))
                if hrs is not None:
                    d["hours"].append(hrs)

        # Find best bucket by expectancy
        best_bucket = None
        best_exp = -999.0
        for bucket, d in bucket_data.items():
            n = d["n"]
            if n < _MIN_SIGNALS:
                continue
            win_rate = d["tp"] / n
            avg_rr = d["rr_sum"] / d["tp"] if d["tp"] else 0.0
            exp = win_rate * avg_rr - (1 - win_rate) * 1.0
            if exp > best_exp:
                best_exp = exp
                best_bucket = bucket

        if best_bucket:
            picks[symbol] = {
                "bucket": best_bucket,
                "expectancy_r": round(best_exp, 4),
                "n": bucket_data[best_bucket]["n"],
                "source": "dynamic_tier_optimizer",
            }

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

async def _compute_geometry_incubation() -> dict[str, Any]:
    """Check geometry incubation status from nt_geometry_incubation.

    Returns promote/reject/keep-incubating verdicts for each candidate.
    """
    if not settings.SUPABASE_URL:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_geometry_incubation"),
                headers=_headers(),
                params={
                    "select": "*",
                    "status": "eq.incubating",
                    "order": "started_at.desc",
                    "limit": "100",
                },
            )
        rows = resp.json() or []
        if not isinstance(rows, list):
            return {}
    except Exception as exc:
        logger.warning("dynamic_tier_optimizer: incubation fetch failed: %s", exc)
        return {}

    verdicts: dict[str, Any] = {}
    for row in rows:
        symbol = row.get("symbol")
        combo = row.get("brick_size"), row.get("sl_bricks"), row.get("tp_bricks")
        key = f"{symbol}:{combo[0]}:{combo[1]}:{combo[2]}"

        # Check if enough forward evidence has accumulated
        started_at = row.get("started_at")
        if started_at:
            try:
                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - start_dt).total_seconds() / 3600
            except (TypeError, ValueError):
                age_hours = 0
        else:
            age_hours = 0

        # Minimum 7 days incubation (same as geometry_incubation_min_days)
        if age_hours < 168:
            verdicts[key] = {"status": "keep_incubating", "age_hours": round(age_hours, 1)}
            continue

        # Check forward resolved count
        n_resolved = row.get("n_forward_resolved", 0)
        if n_resolved < 20:
            verdicts[key] = {"status": "keep_incubating", "n_forward_resolved": n_resolved}
            continue

        # Check expiry regression
        pct_expired = row.get("pct_expired")
        baseline_pct = row.get("baseline_pct_expired")
        if pct_expired is not None and baseline_pct is not None:
            regression = pct_expired - baseline_pct
            if regression > 0.10:
                verdicts[key] = {"status": "reject", "reason": "expiry_regression", "regression": round(regression, 4)}
                continue

        verdicts[key] = {"status": "promote", "n_forward_resolved": n_resolved}

    return verdicts


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

    # Tier 1: Dynamic ATR
    try:
        atr_picks = _compute_dynamic_atr(resolved_rows)
        summary["atr_picks"] = len(atr_picks)
    except Exception as exc:
        summary["errors"].append(f"compute_dynamic_atr: {exc}")
        logger.warning("dynamic_tier_optimizer: compute_dynamic_atr failed: %s", exc)
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
        incubation_verdicts = await _compute_geometry_incubation()
        summary["incubation_verdicts"] = len(incubation_verdicts)
    except Exception as exc:
        summary["errors"].append(f"compute_geometry_incubation: {exc}")
        logger.warning("dynamic_tier_optimizer: compute_geometry_incubation failed: %s", exc)
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

        for symbol, pick in atr_picks.items():
            state_rows.append({
                "symbol": symbol,
                "tier_type": "dynamic_atr",
                "tier_key": pick["bucket"],
                "tier_value": pick["expectancy_r"],
                "n": pick["n"],
                "computed_at": now_iso,
                "source": "dynamic_tier_optimizer",
            })

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
