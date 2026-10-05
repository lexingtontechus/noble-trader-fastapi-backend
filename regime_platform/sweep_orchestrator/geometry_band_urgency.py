"""
geometry_band_urgency.py — Dynamic re-computation of geometry-band
(reachability) statistics when calibration drift is detected.

Replicates the dynamic_atr methodology on the geometry side:
  1. Monitor per-cell calibration bias (nt_calibration_bias_by_cell, already
     refreshed by the 4h EOD pass).
  2. When |bias_shrunk| drifts past threshold, RE-COMPUTE that bucket's
     reachability stats from recent resolved signals — not a static
     threshold lookup.
  3. Upsert the fresh stats into nt_bracket_reachability.
  4. Log a ``geometry_band_urgency`` row to nt_eod_actions.
  5. Per-cell cooldown read back from nt_eod_actions (durable, no state row).
Cadence: called at the END of every light sweep (orchestrator._sweep_group
-> _run_geometry_band_urgency) so bands stay current BETWEEN qualified-signal
generations. A per-cell cooldown (geometry_band_urgency.cooldown_hours,
default 4h) stops it firing every 15 min; the cooldown reads the most recent
geometry_band_urgency row per cell back out of nt_eod_actions (durable across
restarts, no hand-created state row).

Correctness notes (2026-10-04):
  * Resolved rows are episode-deduped (``_dedupe_episodes``) before any
    statistic is computed — one economic signal captured on N consecutive
    sweeps is ONE trial, not N. The episode key includes ``regime``, so a
    capture that spans a regime shift stays a distinct signal.
  * The upsert payload matches nt_bracket_reachability's REAL columns
    (pct_hit_tp / pct_hit_sl / pct_expired / median_hours_to_resolve /
    p75_hours / p90_hours / atr_source), keyed on
    (symbol, vol_tier, distance_atr_bucket, signal).

Usage (standalone):
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.geometry_band_urgency
"""

from __future__ import annotations

import asyncio
import logging
import time
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
from regime_platform.sweep_orchestrator.p_win_bracket import beta_shrunk_win_rate
from regime_platform.sweep_orchestrator.supabase_client import (
    _headers,
    _url,
    fetch_active_symbols,
    upsert_bracket_reachability,
)

logger = logging.getLogger("regime.sweep.geometry_band_urgency")

_PAGE_SIZE = 1000


# ─── Durable state (nt_eod_actions) ─────────────────────────────────────────
#
# Cooldown state comes from the action ledger itself, NOT a hand-created
# nt_roadmap row. write_job_heartbeat() only PATCHes an EXISTING row and no
# `geometry_band_urgency` row exists, so the old roadmap path silently
# persisted nothing and the per-cell cooldown never held — it re-computed
# all 59 drifted cells on EVERY 15-min sweep instead of every 4h
# (observed 2026-10-05 00:47). Reading the most recent action per cell is
# durable across restarts and needs no manual setup or new table.

async def _load_last_fired() -> dict[str, float]:
    """Most recent geometry_band_urgency fire per cell, from nt_eod_actions.

    Returns {symbol:side:vol_tier:bucket -> epoch seconds}. Bounded to the
    last 7 days of actions (a cell idle longer than that is past any
    cooldown anyway, so treating it as never-fired is correct).
    """
    if not settings.SUPABASE_URL:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_eod_actions"),
                headers=_headers(),
                params={
                    "select": "symbol,ts,detail",
                    "action": "eq.geometry_band_urgency",
                    "ts": f"gte.{(datetime.now(timezone.utc) - timedelta(days=7)).isoformat()}",
                    "order": "ts.desc",
                    "limit": "500",
                },
            )
        rows = resp.json() or []
    except Exception as exc:
        logger.warning("[geo:urg] last-fired fetch failed: %s", exc)
        return {}

    out: dict[str, float] = {}
    for r in rows:
        detail = r.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        key = ":".join(str(detail.get(k)) for k in (
            "side", "vol_tier", "distance_atr_bucket",
        ))
        state_key = f"{r.get('symbol')}:{key}"
        ts = _ts_or_none(r.get("ts"))
        if ts is None:
            continue
        # Rows arrive newest-first; keep the first (most recent) per cell.
        if state_key not in out:
            out[state_key] = ts
    return out


def _ts_or_none(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except (TypeError, ValueError):
        return None


# ─── Data fetch ──────────────────────────────────────────────────────────────

async def _fetch_resolved_rows(symbol: str, since: datetime) -> list[dict[str, Any]]:
    """Fetch + episode-dedupe resolved nt_signal_sim rows for one symbol."""
    if not settings.SUPABASE_URL:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    params: dict[str, str] = {
        "select": (
            "symbol,ts,signal,entry_price,take_profit,stop_loss,atr,"
            "outcome,resolved_at,regime,p_win"
        ),
        "symbol": f"eq.{symbol}",
        "outcome": "not.is.null",
        "ts": f"gte.{since.isoformat()}",
        "order": "ts.asc",
        "limit": str(_PAGE_SIZE),
    }
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


async def _fetch_cell_bias(symbol: str, min_n: int) -> dict[tuple, dict]:
    """Current per-cell calibration bias for one symbol (the drift signal).

    Key: (side, vol_tier, distance_atr_bucket) -> {n, bias, bias_shrunk}.
    """
    if not settings.SUPABASE_URL:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_calibration_bias_by_cell"),
                headers=_headers(),
                params={
                    "select": "side,vol_tier,distance_atr_bucket,n,bias,bias_shrunk",
                    "symbol": f"eq.{symbol}",
                    "n": f"gte.{min_n}",
                },
            )
        rows = resp.json() or []
    except Exception as exc:
        logger.warning("[geo:urg] cell bias fetch failed for %s: %s", symbol, exc)
        return {}

    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("side"), r.get("vol_tier"), r.get("distance_atr_bucket"))
        out[key] = {
            "n": int(r.get("n") or 0),
            "bias": float(r.get("bias") or 0.0),
            "bias_shrunk": float(r.get("bias_shrunk") or 0.0),
        }
    return out


# ─── Re-computation ──────────────────────────────────────────────────────────

def _recompute_cell(
    rows: list[dict[str, Any]],
    *,
    side: str,
    vol_tier: str,
    bucket: str,
) -> dict[str, Any] | None:
    """Re-compute one cell's stats from recent (deduped) resolved rows.

    Returns None when the cell has no rows in the window. Mirrors
    ``bracket_reachability.build_reachability_table``'s per-row bucketing so
    the fresh values are directly comparable to the stored ones.
    """
    n = tp = sl = exp = n_stored = n_proxy = 0
    p_win_sum = 0.0
    hours: list[float] = []

    for row in rows:
        entry = row.get("entry_price")
        tp_price = row.get("take_profit")
        outcome = row.get("outcome")
        if entry is None or tp_price is None:
            continue
        if outcome not in ("tp_hit", "sl_hit", "expired"):
            continue

        atr = row.get("atr")
        if not atr:
            # No per-symbol ATR proxy here: a row missing its stored ATR is
            # skipped rather than mixing a look-ahead proxy into an urgency
            # refresh (the weekly full build does use the proxy).
            continue

        ratio = abs(float(tp_price) - float(entry)) / float(atr)
        if _bucket_for(ratio) != bucket:
            continue
        if _vol_tier(row.get("regime")) != vol_tier:
            continue
        if side != "all" and _signal_side(row.get("signal")) != side:
            continue

        n += 1
        n_stored += 1
        p_win_sum += float(row.get("p_win") or 0.0)
        if outcome == "tp_hit":
            tp += 1
        elif outcome == "sl_hit":
            sl += 1
        else:
            exp += 1
        if outcome in ("tp_hit", "sl_hit"):
            h = _hours_between(row.get("ts"), row.get("resolved_at"))
            if h is not None:
                hours.append(h)

    if n == 0:
        return None

    win_rate = tp / n
    avg_p_win = p_win_sum / n
    shrunk = beta_shrunk_win_rate(tp, sl)
    bias = avg_p_win - win_rate
    bias_shrunk = avg_p_win - shrunk if shrunk is not None else bias

    return {
        "n": n,
        "n_stored": n_stored,
        "n_proxy": n_proxy,
        "pct_hit_tp": round(tp / n, 4),
        "pct_hit_sl": round(sl / n, 4),
        "pct_expired": round(exp / n, 4),
        "median_hours_to_resolve": (
            round(float(np.percentile(hours, 50)), 2) if hours else None
        ),
        "p75_hours": round(float(np.percentile(hours, 75)), 2) if hours else None,
        "p90_hours": round(float(np.percentile(hours, 90)), 2) if hours else None,
        "atr_source": "stored",
        "win_rate": round(win_rate, 4),
        "avg_p_win": round(avg_p_win, 4),
        "bias": round(bias, 4),
        "bias_shrunk": round(bias_shrunk, 4),
    }


async def _log_eod_action(symbol: str, detail: dict[str, Any], bias: float) -> None:
    """Append a geometry_band_urgency row to nt_eod_actions (best-effort)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                _url("nt_eod_actions"),
                headers={**_headers(), "Prefer": "return=minimal"},
                json={
                    "symbol": symbol,
                    "action": "geometry_band_urgency",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "bias": bias,
                    "detail": detail,
                },
            )
    except Exception as exc:
        logger.warning("[geo:urg] eod_actions log failed for %s: %s", symbol, exc)


# ─── Entry point ─────────────────────────────────────────────────────────────

async def check_geometry_band_urgency() -> dict[str, Any]:
    """Sweep-triggered geometry-band drift check + re-computation.

    Returns {enabled, checked, triggered, recomputed, errors}. Best-effort:
    never raises into the sweep tick.
    """
    from regime_platform.core.feature_flags import get_geometry_band_urgency_config

    cfg = get_geometry_band_urgency_config()
    if not cfg.get("enabled", True):
        return {"enabled": False, "checked": 0, "triggered": 0,
                "recomputed": 0, "errors": []}

    threshold = float(cfg.get("urgency_threshold", 0.15))
    min_n = int(cfg.get("min_n", 20))
    cooldown_h = float(cfg.get("cooldown_hours", 4))
    lookback_days = int(cfg.get("lookback_days", 7))

    last_recomputed: dict[str, float] = await _load_last_fired()
    errors: list[str] = []
    checked = triggered = recomputed = 0

    try:
        peers = [p for p in await fetch_active_symbols() if p.get("symbol")]
    except Exception as exc:
        logger.warning("[geo:urg] peers fetch failed: %s", exc)
        return {"enabled": True, "checked": 0, "triggered": 0,
                "recomputed": 0, "errors": [str(exc)]}

    now = time.time()
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    for p in peers:
        symbol = p["symbol"]
        sym_cfg = get_geometry_band_urgency_config(symbol)
        sym_threshold = float(sym_cfg.get("urgency_threshold", threshold))
        sym_min_n = int(sym_cfg.get("min_n", min_n))
        sym_cooldown = float(sym_cfg.get("cooldown_hours", cooldown_h))

        cells = await _fetch_cell_bias(symbol, sym_min_n)
        if not cells:
            continue
        checked += 1

        drifted = [
            (key, c) for key, c in cells.items()
            if c["n"] >= sym_min_n and abs(c["bias_shrunk"]) >= sym_threshold
        ]
        if not drifted:
            continue

        try:
            rows = await _fetch_resolved_rows(symbol, since)
        except Exception as exc:
            errors.append(f"{symbol}: fetch failed: {exc}")
            continue
        if not rows:
            continue

        for (side, vol_tier, bucket), cur in drifted:
            state_key = f"{symbol}:{side}:{vol_tier}:{bucket}"
            last_ts = _ts_or_none(last_recomputed.get(state_key))
            if last_ts is not None and (now - last_ts) < sym_cooldown * 3600.0:
                continue

            fresh = _recompute_cell(
                rows, side=side, vol_tier=vol_tier, bucket=bucket,
            )
            if fresh is None or fresh["n"] < sym_min_n:
                continue

            triggered += 1

            # Upsert fresh reachability stats (real nt_bracket_reachability
            # column names; keyed on symbol, vol_tier, bucket, signal).
            try:
                await upsert_bracket_reachability([{
                    "symbol": symbol,
                    "vol_tier": vol_tier,
                    "distance_atr_bucket": bucket,
                    "signal": side,
                    "n": fresh["n"],
                    "n_stored": fresh["n_stored"],
                    "n_proxy": fresh["n_proxy"],
                    "pct_hit_tp": fresh["pct_hit_tp"],
                    "pct_hit_sl": fresh["pct_hit_sl"],
                    "pct_expired": fresh["pct_expired"],
                    "median_hours_to_resolve": fresh["median_hours_to_resolve"],
                    "p75_hours": fresh["p75_hours"],
                    "p90_hours": fresh["p90_hours"],
                    "atr_source": fresh["atr_source"],
                }])
            except Exception as exc:
                errors.append(f"{symbol}: upsert failed: {exc}")
                continue

            await _log_eod_action(
                symbol,
                {
                    "side": side,
                    "vol_tier": vol_tier,
                    "distance_atr_bucket": bucket,
                    "old_bias_shrunk": cur["bias_shrunk"],
                    "new_bias_shrunk": fresh["bias_shrunk"],
                    "old_n": cur["n"],
                    "new_n": fresh["n"],
                    "lookback_days": lookback_days,
                    "source": "sweep",
                },
                bias=fresh["bias_shrunk"],
            )

            # In-memory stamp so a second drifted cell for the same symbol in
            # this run is cooldown-checked too. Durability comes from the
            # nt_eod_actions row just written (read back by _load_last_fired).
            last_recomputed[state_key] = now
            recomputed += 1

    logger.info(
        "[geo:urg] checked=%d triggered=%d recomputed=%d errors=%d",
        checked, triggered, recomputed, len(errors),
    )
    return {
        "enabled": True,
        "checked": checked,
        "triggered": triggered,
        "recomputed": recomputed,
        "errors": errors,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(check_geometry_band_urgency())
