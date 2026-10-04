"""
geometry_band_urgency.py — Dynamic re-computation of geometry band
calibration statistics when drift is detected.

Replicates the dynamic_atr methodology:
1. Monitor per-bucket calibration bias (already computed by 4h EOD pass)
2. When bias drifts beyond threshold, re-compute from recent resolved signals
3. Update nt_bracket_reachability with fresh statistics
4. Log to nt_eod_actions (geometry_band_urgency)

This is NOT a static threshold lookup. It dynamically re-computes bucket
statistics from recent data when drift is detected — the same way
dynamic_atr re-optimizes TP/SL from recent signals when vol spikes.

Cadence: 4h check, 4h per-bucket cooldown (same as ATR urgency).
State: nt_roadmap(geometry_band_urgency) — survives restarts.

Usage:
  PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m regime_platform.sweep_orchestrator.geometry_band_urgency
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import (
    ATR_BUCKETS,
    _bucket_for,
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

_URGENCY_STATE_MECH = "geometry_band_urgency"
_PAGE_SIZE = 1000


async def _load_urgency_state() -> dict[str, Any]:
    """Durable last-run stamps from nt_roadmap (survives restart)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_roadmap"),
                headers=_headers(),
                params={"select": "current_state,notes", "mechanism": f"eq.{_URGENCY_STATE_MECH}"},
            )
        rows = resp.json() or []
        if not rows:
            return {}
        raw = rows[0].get("current_state") or ""
        if "·" in raw:
            raw = raw.split("·", 1)[1].strip()
        import json
        return json.loads(raw) if raw.startswith("{") else {}
    except Exception as exc:
        logger.warning("[geo:urg] state load failed: %s", exc)
        return {}


async def _save_urgency_state(state: dict[str, Any]) -> None:
    try:
        from regime_platform.sweep_orchestrator.kairos import write_job_heartbeat
        await write_job_heartbeat(_URGENCY_STATE_MECH, state)
    except Exception as exc:
        logger.warning("[geo:urg] state save failed: %s", exc)


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


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _fetch_resolved_rows(
    symbol: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """Fetch resolved nt_signal_sim rows for a symbol since a date."""
    rows: list[dict[str, Any]] = []
    offset = 0
    params: dict[str, str] = {
        "select": "symbol,ts,entry_price,take_profit,atr,outcome,resolved_at,regime,signal",
        "symbol": f"eq.{symbol}",
        "outcome": "not.is.null",
        "and": f"(ts.gte.{since.isoformat()})",
        "order": "ts.asc",
        "limit": str(_PAGE_SIZE),
    }
    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(
                _url("nt_signal_sim"),
                headers=_headers(),
                params={**params, "offset": str(offset)},
            )
            resp.raise_for_status()
            batch = resp.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch)
            if len(batch) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    return rows


def _compute_bucket_stats(rows: list[dict[str, Any]]) -> dict[tuple, dict]:
    """Compute per-bucket reachability stats from resolved signals."""
    cells = defaultdict(lambda: {
        "n": 0, "tp": 0, "sl": 0, "exp": 0, "p_win_sum": 0.0,
    })
    for r in rows:
        entry = r.get("entry_price")
        tp = r.get("take_profit")
        atr = r.get("atr")
        if not entry or not tp or not atr:
            continue
        ratio = abs(float(tp) - float(entry)) / float(atr)
        bucket = _bucket_for(ratio)
        if bucket is None:
            continue
        key = (r["symbol"], _vol_tier(r.get("regime")), bucket)
        cells[key]["n"] += 1
        cells[key]["p_win_sum"] += float(r.get("p_win") or 0.0)
        outcome = r.get("outcome")
        if outcome == "tp_hit":
            cells[key]["tp"] += 1
        elif outcome == "sl_hit":
            cells[key]["sl"] += 1
        else:
            cells[key]["exp"] += 1

    out = {}
    for key, c in cells.items():
        n = c["n"]
        if n == 0:
            continue
        p_resolve = (c["tp"] + c["sl"]) / n if n else 0.0
        win_rate = c["tp"] / n if n else 0.0
        avg_p_win = c["p_win_sum"] / n
        bias = avg_p_win - win_rate
        shrunk_wr = beta_shrunk_win_rate(c["tp"], c["sl"])
        bias_shrunk = avg_p_win - shrunk_wr if shrunk_wr is not None else bias
        out[key] = {
            "n": n, "tp": c["tp"], "sl": c["sl"], "exp": c["exp"],
            "p_resolve": round(p_resolve, 4),
            "win_rate": round(win_rate, 4),
            "avg_p_win": round(avg_p_win, 4),
            "bias": round(bias, 4),
            "bias_shrunk": round(bias_shrunk, 4),
        }
    return out


async def _fetch_bucket_bias(symbol: str) -> dict[tuple, dict]:
    """Fetch current per-bucket bias from nt_calibration_bias_by_cell."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_calibration_bias_by_cell"),
                headers=_headers(),
                params={
                    "select": "symbol,side,vol_tier,distance_atr_bucket,n,bias,bias_shrunk,expectancy_r",
                    "symbol": f"eq.{symbol}",
                    "n": "gte.10",
                },
            )
        rows = resp.json() or []
    except Exception as exc:
        logger.warning("[geo:urg] bias fetch failed for %s: %s", symbol, exc)
        return {}

    out = {}
    for r in rows:
        key = (r["symbol"], r["vol_tier"], r["distance_atr_bucket"])
        out[key] = {
            "n": int(r.get("n") or 0),
            "bias": float(r.get("bias") or 0.0),
            "bias_shrunk": float(r.get("bias_shrunk") or 0.0),
            "expectancy_r": float(r.get("expectancy_r") or 0.0),
        }
    return out


async def check_geometry_band_urgency() -> dict[str, Any]:
    """4h urgency check for geometry band calibration drift.

    Replicates dynamic_atr methodology:
    1. For each symbol, fetch current per-bucket bias
    2. If |bias_shrunk| > threshold and n >= min_n, re-compute from recent signals
    3. Upsert fresh stats into nt_bracket_reachability
    4. Log to nt_eod_actions (geometry_band_urgency)

    Returns: {enabled, checked, triggered, recomputed, errors}
    """
    from regime_platform.core.feature_flags import get_geometry_band_urgency_config

    cfg = get_geometry_band_urgency_config()
    if not cfg.get("enabled", True):
        return {"enabled": False, "checked": 0, "triggered": 0, "recomputed": 0, "errors": []}

    threshold = float(cfg.get("urgency_threshold", 0.15))
    min_n = int(cfg.get("min_n", 20))
    cooldown_h = float(cfg.get("cooldown_hours", 4))
    min_age_h = float(cfg.get("min_age_hours", 4))
    lookback_days = int(cfg.get("lookback_days", 7))

    state = await _load_urgency_state()
    last_recomputed = state.get("last_recomputed_at") or {}
    errors: list[str] = []
    checked = 0
    triggered = 0
    recomputed = 0

    try:
        peers = [p for p in await fetch_active_symbols() if p.get("symbol")]
    except Exception as exc:
        logger.warning("[geo:urg] peers fetch failed: %s", exc)
        return {"enabled": True, "checked": 0, "triggered": 0, "recomputed": 0, "errors": [str(exc)]}

    now = time.time()
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    for p in peers:
        symbol = p["symbol"]
        sym_cfg = get_geometry_band_urgency_config(symbol)
        sym_threshold = float(sym_cfg.get("urgency_threshold", threshold))
        sym_min_n = int(sym_cfg.get("min_n", min_n))
        sym_cooldown = float(sym_cfg.get("cooldown_hours", cooldown_h))

        # Fetch current bias
        try:
            bias_data = await _fetch_bucket_bias(symbol)
        except Exception as exc:
            errors.append(f"{symbol}: bias fetch failed: {exc}")
            continue

        if not bias_data:
            continue
        checked += 1

        # Check each bucket
        for key, bias in bias_data.items():
            if bias["n"] < sym_min_n:
                continue
            if abs(bias["bias_shrunk"]) < sym_threshold:
                continue

            # Cooldown check
            last_ts = _ts_or_none(last_recomputed.get(f"{key[0]}:{key[1]}:{key[2]}"))
            if last_ts is not None and (now - last_ts) < sym_cooldown * 3600.0:
                continue

            # Re-compute from recent signals
            try:
                rows = await _fetch_resolved_rows(symbol, since)
            except Exception as exc:
                errors.append(f"{symbol}: fetch failed: {exc}")
                continue

            if not rows:
                continue

            recent_stats = _compute_bucket_stats(rows)
            recent_bucket = recent_stats.get(key)
            if recent_bucket is None or recent_bucket["n"] < sym_min_n:
                continue

            # Upsert fresh stats
            try:
                await upsert_bracket_reachability([{
                    "symbol": key[0],
                    "vol_tier": key[1],
                    "distance_atr_bucket": key[2],
                    "n_stored": recent_bucket["n"],
                    "n_proxy": 0,
                    "p_resolve": recent_bucket["p_resolve"],
                    "p_win_resolve": recent_bucket["win_rate"],
                    "expectancy_r": recent_bucket.get("expectancy_r", 0.0),
                }])
            except Exception as exc:
                errors.append(f"{symbol}: upsert failed: {exc}")
                continue

            # Log to nt_eod_actions
            try:
                from regime_platform.sweep_orchestrator.supabase_client import _headers, _url
                import httpx
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.post(
                        _url("nt_eod_actions"),
                        headers=_headers(),
                        json={
                            "symbol": symbol,
                            "action": "geometry_band_urgency",
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "detail": {
                                "vol_tier": key[1],
                                "bucket": key[2],
                                "old_bias": bias["bias_shrunk"],
                                "new_bias": recent_bucket["bias_shrunk"],
                                "n": recent_bucket["n"],
                            },
                        },
                    )
            except Exception as exc:
                errors.append(f"{symbol}: eod_actions log failed: {exc}")

            last_recomputed[f"{key[0]}:{key[1]}:{key[2]}"] = _iso(now)
            triggered += 1
            recomputed += 1

    # Save state
    state["last_recomputed_at"] = last_recomputed
    state["last_run_at"] = _iso(now)
    await _save_urgency_state(state)

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
