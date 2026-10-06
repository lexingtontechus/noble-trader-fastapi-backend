"""
Dynamic Deadline Tiers — per-(symbol, vol_tier) resolution deadlines.
═══════════════════════════════════════════════════════════════════════

Replaces the uniform 48h deadline with regime-conditional deadline tiers
derived from the reachability table's own percentiles.

Each (symbol, vol_tier) gets a DeadlineTier with:
  - soft_deadline_hours (p75) — full Sharpe credit
  - hard_deadline_hours (p90) — zero credit
  - median_hours (p50) — expected resolution
  - confidence (high/medium/low based on sample size)

The deadline is a DISTRIBUTION, not a constant. A high_vol regime has
wider natural brackets AND faster resolution. A low_vol regime has
tighter brackets AND slower resolution.

Fail-open: missing data → conservative defaults (24h soft, 48h hard).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.dynamic_deadline")


# ─── Data model ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeadlineTier:
    """Per-(symbol, vol_tier) deadline configuration."""

    symbol: str
    vol_tier: str
    soft_deadline_hours: float   # p75 — full Sharpe credit
    hard_deadline_hours: float   # p90 — zero credit
    median_hours: float          # p50 — expected resolution
    n: int                       # sample size
    confidence: str              # high (n>=50), medium (n>=20), low (n<20)

    def decay(self, hours: float) -> float:
        """Sharpe decay factor for a trade resolving in `hours`.

        1.0 at or below soft_deadline, 0.0 at or above hard_deadline,
        linear interpolation between.
        """
        if hours <= self.soft_deadline_hours:
            return 1.0
        if hours >= self.hard_deadline_hours:
            return 0.0
        span = self.hard_deadline_hours - self.soft_deadline_hours
        if span <= 0:
            return 0.0
        return 1.0 - (hours - self.soft_deadline_hours) / span

    def to_payload(self) -> dict[str, Any]:
        """Convert to a signal-payload dict (for nt_signal_sim + broadcast)."""
        return {
            "expected_resolution_hours": round(self.median_hours, 1),
            "expected_resolution_days": round(self.median_hours / 24.0, 1),
            "resolution_confidence": self.confidence,
            "soft_deadline_hours": round(self.soft_deadline_hours, 1),
            "hard_deadline_hours": round(self.hard_deadline_hours, 1),
        }


# ─── Derivation ────────────────────────────────────────────────────────


def derive_deadline_tier(
    symbol: str,
    vol_tier: str,
    reachability_rows: list[dict[str, Any]],
    *,
    min_soft: float = 12.0,
    max_hard: float = 120.0,
    min_n_high: int = 50,
    min_n_medium: int = 20,
) -> DeadlineTier:
    """Derive a DeadlineTier from nt_bracket_reachability rows.

    Uses the row with the most episodes for this (symbol, vol_tier).
    Falls back to vol_tier-global, then to conservative defaults.
    """
    # Filter rows for this (symbol, vol_tier)
    rows = [
        r for r in reachability_rows
        if r.get("symbol") == symbol and r.get("vol_tier") == vol_tier
    ]

    if not rows:
        # Fallback to global percentiles for this vol_tier
        rows = [r for r in reachability_rows if r.get("vol_tier") == vol_tier]

    if not rows:
        # Ultimate fallback: conservative defaults
        return DeadlineTier(
            symbol=symbol, vol_tier=vol_tier,
            soft_deadline_hours=24.0, hard_deadline_hours=48.0,
            median_hours=24.0, n=0, confidence="low",
        )

    # Use the row with the most episodes (most reliable)
    best = max(rows, key=lambda r: r.get("n", 0))
    n = int(best.get("n", 0))

    soft = best.get("p75_hours") or best.get("median_hours") or 24.0
    hard = best.get("p90_hours") or soft * 2.0
    median = best.get("median_hours") or soft

    # Clamp
    soft = max(min_soft, min(float(soft), 72.0))
    hard = max(soft * 1.5, min(float(hard), max_hard))

    confidence = "high" if n >= min_n_high else "medium" if n >= min_n_medium else "low"

    return DeadlineTier(
        symbol=symbol, vol_tier=vol_tier,
        soft_deadline_hours=soft, hard_deadline_hours=hard,
        median_hours=float(median), n=n, confidence=confidence,
    )


def derive_all_deadline_tiers(
    symbols: list[str],
    vol_tiers: list[str],
    reachability_rows: list[dict[str, Any]],
) -> dict[tuple[str, str], DeadlineTier]:
    """Derive DeadlineTier for every (symbol, vol_tier) pair."""
    tiers: dict[tuple[str, str], DeadlineTier] = {}
    for sym in symbols:
        for vt in vol_tiers:
            tiers[(sym, vt)] = derive_deadline_tier(sym, vt, reachability_rows)
    return tiers


# ─── Urgency reopt (4h cycle, same pattern as atr_reopt.py) ──────────────

_URGENCY_STATE_MECH = "dynamic_deadline_reopt_urgency"


async def _load_urgency_state() -> dict[str, Any]:
    """Durable last-run/last-apply stamps from nt_roadmap (survives restart)."""
    try:
        import httpx
        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_roadmap"),
                headers=_headers(),
                params={
                    "select": "current_state,notes",
                    "mechanism": f"eq.{_URGENCY_STATE_MECH}",
                },
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
        logger.warning("[deadline:urg] state load failed: %s", exc)
        return {}


async def _save_urgency_state(state: dict[str, Any]) -> None:
    try:
        from regime_platform.sweep_orchestrator.kairos import write_job_heartbeat
        await write_job_heartbeat(_URGENCY_STATE_MECH, state)
    except Exception as exc:
        logger.warning("[deadline:urg] state save failed: %s", exc)


async def _write_deadline_tiers_to_db(
    symbols: list[str],
    vol_tiers: list[str],
    reach_rows: list[dict[str, Any]],
) -> None:
    """Write derived deadline tiers to nt_deadline_tiers (migration 200).

    Best-effort: failures are logged and swallowed. Uses upsert on
    (symbol, vol_tier) so each reopt cycle replaces the prior row.
    """
    try:
        import httpx
        from regime_platform.core.settings import settings
        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

        if not settings.SUPABASE_URL:
            return

        rows = []
        for sym in symbols:
            for vt in vol_tiers:
                tier = derive_deadline_tier(sym, vt, reach_rows)
                rows.append({
                    "symbol": sym,
                    "vol_tier": vt,
                    "soft_deadline_hours": tier.soft_deadline_hours,
                    "hard_deadline_hours": tier.hard_deadline_hours,
                    "median_hours": tier.median_hours,
                    "n": tier.n,
                    "confidence": tier.confidence,
                })

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _url("nt_deadline_tiers"),
                headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
                json=rows,
            )
            if not resp.is_success:
                logger.warning(
                    "[deadline:urg] nt_deadline_tiers upsert HTTP %d — %s",
                    resp.status_code, resp.text[:200],
                )
    except Exception as exc:
        logger.warning("[deadline:urg] nt_deadline_tiers write failed: %s", exc)


def _ts_or_none(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except (TypeError, ValueError):
        return None


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def check_deadline_reopt_urgency() -> dict[str, Any]:
    """4h cycle: re-derive deadline tiers from fresh reachability data.

    Same pattern as atr_reopt.py:
    1. Every 4h: read fresh nt_bracket_reachability data
    2. Re-derive DeadlineTier per (symbol, vol_tier)
    3. If tier drifted > 15% from current, flag for review
    4. State lives in nt_roadmap(dynamic_deadline_reopt_urgency)

    Returns {enabled, checked, drifted, tiers_updated}. Never raises.
    """
    import time as _time

    from regime_platform.core.feature_flags import get_dynamic_deadline_config

    cfg = get_dynamic_deadline_config()
    if not cfg.get("enabled", False):
        return {"enabled": False, "checked": 0, "drifted": []}

    cycle_hours = float(cfg.get("cycle_hours") or 4.0)
    drift_threshold = float(cfg.get("drift_threshold") or 0.15)
    min_n = int(cfg.get("min_n_for_update") or 20)

    state = await _load_urgency_state()
    last_run = _ts_or_none(state.get("last_run_at"))
    now = _time.time()

    if last_run is not None and (now - last_run) < cycle_hours * 3600.0:
        return {
            "enabled": True,
            "checked": 0,
            "drifted": [],
            "skipped": f"cycle floor ({cycle_hours}h) — last_run {_iso(last_run)}",
        }

    try:
        import httpx
        from regime_platform.sweep_orchestrator.supabase_client import (
            _headers,
            _url,
            fetch_active_symbols,
        )

        peers = [
            p for p in await fetch_active_symbols()
            if p.get("symbol") and p.get("tradingview_symbol")
        ]
        symbols = [p["symbol"] for p in peers]

        # Fetch reachability data
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "symbol,vol_tier,distance_atr_bucket,n,p75_hours,p90_hours,median_hours_to_resolve",
                    "order": "symbol.asc",
                    "limit": "1000",
                },
            )
        reach_rows = resp.json() or []

        # Derive tiers for all symbols
        vol_tiers = list(set(r.get("vol_tier", "unknown") for r in reach_rows))
        if not vol_tiers:
            vol_tiers = ["low_vol", "med_low_vol", "med_high_vol", "high_vol", "unknown"]

        drifted = []
        for sym in symbols:
            for vt in vol_tiers:
                new_tier = derive_deadline_tier(sym, vt, reach_rows)
                if new_tier.n < min_n:
                    continue

                # Check drift from stored tier
                stored = state.get("tiers", {}).get(f"{sym}:{vt}")
                if stored:
                    old_soft = stored.get("soft_deadline_hours")
                    old_hard = stored.get("hard_deadline_hours")
                    if old_soft and old_hard:
                        soft_drift = abs(new_tier.soft_deadline_hours - old_soft) / old_soft
                        hard_drift = abs(new_tier.hard_deadline_hours - old_hard) / old_hard
                        if soft_drift > drift_threshold or hard_drift > drift_threshold:
                            drifted.append({
                                "symbol": sym,
                                "vol_tier": vt,
                                "old_soft": old_soft,
                                "new_soft": new_tier.soft_deadline_hours,
                                "old_hard": old_hard,
                                "new_hard": new_tier.hard_deadline_hours,
                                "n": new_tier.n,
                            })

        # Update state
        state["last_run_at"] = _iso(now)
        state["tiers"] = {}
        for sym in symbols:
            for vt in vol_tiers:
                tier = derive_deadline_tier(sym, vt, reach_rows)
                key = f"{sym}:{vt}"
                state["tiers"][key] = {
                    "soft_deadline_hours": tier.soft_deadline_hours,
                    "hard_deadline_hours": tier.hard_deadline_hours,
                    "n": tier.n,
                }
        await _save_urgency_state(state)
        await _write_deadline_tiers_to_db(symbols, vol_tiers, reach_rows)

        logger.warning(
            "[deadline:urg] checked=%d drifted=%d cycle=%sh",
            len(symbols) * len(vol_tiers), len(drifted), cycle_hours,
        )

        return {
            "enabled": True,
            "checked": len(symbols) * len(vol_tiers),
            "drifted": drifted,
        }

    except Exception as exc:
        logger.error("[deadline:urg] failed: %s", exc)
        return {"enabled": True, "checked": 0, "drifted": [], "error": str(exc)}
