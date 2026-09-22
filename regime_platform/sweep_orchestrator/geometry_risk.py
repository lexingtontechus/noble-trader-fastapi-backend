"""
Geometry risk — long-lag (expired) share as an ATR/bracket risk input
══════════════════════════════════════════════════════════════════════

worklog/20260918_TIERED_ATR_GEOMETRY_SCOPE.md

Canonical language:
  * Short-horizon resolve — tp_hit/sl_hit within 48h (platform contract)
  * Long-lag / geometry   — expired (open past 48h); not a chosen swing
    but risk from brackets wider than the symbol moves in-window

This module estimates long-lag risk for a *proposed* bracket and formats
metrics for capture. Pure functions + best-effort lookup.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import (
    _bucket_for,
    _vol_tier,
)
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.geometry_risk")

# Conservative prior when a (symbol, vol_tier, bucket) cell is unscored.
# Long-lag share has historically been high on wide buckets — fail toward
# risk, not toward "assume reachable".
LONG_LAG_PRIOR = 0.40
# Cap for diagnostic long_lag_risk flag (worklog Phase C3).
GEOMETRY_RISK_CAP = 0.35
# Weight on resolve-time tail: p90_hours / 48h beyond 1.0 adds risk.
P90_WEIGHT = 0.25
# Vol drift: ATR_now / ATR_at_write — if >1.2, extra risk weight.
VOL_DRIFT_SOFT = 1.2
VOL_DRIFT_WEIGHT = 0.15


@dataclass(frozen=True)
class GeometryMetrics:
    tp_distance_atr: Optional[float]
    sl_distance_atr: Optional[float]
    long_lag_hat: Optional[float]
    geometry_risk_hat: Optional[float]
    bucket: Optional[str]
    vol_tier: str
    p90_hours: Optional[float]
    atr_now: Optional[float]
    atr_at_write: Optional[float]
    long_lag_risk: bool
    reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp_distance_atr": self.tp_distance_atr,
            "sl_distance_atr": self.sl_distance_atr,
            "long_lag_hat": self.long_lag_hat,
            "geometry_risk_hat": self.geometry_risk_hat,
            "bucket": self.bucket,
            "vol_tier": self.vol_tier,
            "p90_hours": self.p90_hours,
            "atr_now": self.atr_now,
            "atr_at_write": self.atr_at_write,
            "long_lag_risk": self.long_lag_risk,
            "reason": self.reason,
        }


def distance_over_atr(distance: Optional[float], atr: Optional[float]) -> Optional[float]:
    if not atr or atr <= 0 or distance is None:
        return None
    return abs(float(distance)) / float(atr)


def vol_drift_factor(
    atr_now: Optional[float],
    atr_at_write: Optional[float],
    *,
    soft: float = VOL_DRIFT_SOFT,
    weight: float = VOL_DRIFT_WEIGHT,
) -> float:
    """1.0 when no drift data or drift is mild; grows if ATR expanded."""
    if not atr_now or not atr_at_write or atr_at_write <= 0:
        return 1.0
    ratio = float(atr_now) / float(atr_at_write)
    if ratio <= soft:
        return 1.0
    return 1.0 + weight * min(ratio - soft, 2.0)


def geometry_risk_hat(
    long_lag: float,
    p90_hours: Optional[float],
    *,
    deadline_hours: float = 48.0,
    p90_weight: float = P90_WEIGHT,
    vol_drift: float = 1.0,
) -> float:
    """Risk score in ~[0, 1.5] — higher = more unmanaged long-lag exposure."""
    tail = 0.0
    if p90_hours is not None and deadline_hours > 0:
        tail = max(0.0, float(p90_hours) / deadline_hours - 1.0)
    return float(long_lag) * (1.0 + p90_weight * tail) * float(vol_drift)


def compute_geometry_metrics(
    *,
    entry_price: Optional[float],
    take_profit: Optional[float],
    stop_loss: Optional[float],
    atr: Optional[float],
    regime_label: Optional[str] = None,
    atr_at_write: Optional[float] = None,
    pct_expired: Optional[float] = None,
    p90_hours: Optional[float] = None,
    long_lag_prior: float = LONG_LAG_PRIOR,
    risk_cap: float = GEOMETRY_RISK_CAP,
) -> GeometryMetrics:
    vol_tier = _vol_tier(regime_label)
    tp_d = (
        distance_over_atr(
            (take_profit - entry_price) if (take_profit is not None and entry_price is not None) else None,
            atr,
        )
    )
    sl_d = (
        distance_over_atr(
            (stop_loss - entry_price) if (stop_loss is not None and entry_price is not None) else None,
            atr,
        )
    )
    bucket = _bucket_for(tp_d) if tp_d is not None else None
    if pct_expired is not None:
        long_lag = max(0.0, min(1.0, float(pct_expired)))
        long_lag_src = "observed"
    elif bucket is not None:
        long_lag = long_lag_prior
        long_lag_src = "prior"
    else:
        long_lag = None
        long_lag_src = "unknown"

    if long_lag is None:
        risk = None
        flagged = False
        reason = "missing_atr_or_distance"
    else:
        drift = vol_drift_factor(atr, atr_at_write)
        risk = round(geometry_risk_hat(long_lag, p90_hours, vol_drift=drift), 4)
        flagged = risk >= risk_cap
        reason = None if long_lag_src == "observed" else f"long_lag_{long_lag_src}"

    return GeometryMetrics(
        tp_distance_atr=round(tp_d, 4) if tp_d is not None else None,
        sl_distance_atr=round(sl_d, 4) if sl_d is not None else None,
        long_lag_hat=round(long_lag, 4) if long_lag is not None else None,
        geometry_risk_hat=risk,
        bucket=bucket,
        vol_tier=vol_tier,
        p90_hours=round(float(p90_hours), 2) if p90_hours is not None else None,
        atr_now=float(atr) if atr else None,
        atr_at_write=float(atr_at_write) if atr_at_write else None,
        long_lag_risk=flagged,
        reason=reason,
    )


async def lookup_long_lag(
    symbol: str,
    *,
    vol_tier: str,
    bucket: str | None,
) -> tuple[Optional[float], Optional[float]]:
    """Return (pct_expired, p90_hours) from nt_bracket_reachability if scored."""
    if not settings.SUPABASE_URL or not bucket:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "pct_expired,p90_hours,n_stored,n",
                    "symbol": f"eq.{symbol}",
                    "vol_tier": f"eq.{vol_tier}",
                    "distance_atr_bucket": f"eq.{bucket}",
                    "signal": "eq.all",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None, None
        row = rows[0]
        n_stored = int(row.get("n_stored") or 0)
        if n_stored < 3:
            return None, None
        return row.get("pct_expired"), row.get("p90_hours")
    except Exception as exc:
        logger.warning("lookup_long_lag %s: %s", symbol, exc)
        return None, None


# ── T1/T2 intraday vol-drift brick scale (2026-09-19) ───────────────
# scale = clip(ATR_now / ATR_at_write, lo, hi); applied to brick_size only.
VOL_SCALE_LO = 0.5
VOL_SCALE_HI = 1.5
VOL_SCALE_DEADBAND = (0.85, 1.15)  # no scale inside this band


def brick_vol_scale(
    atr_now: float | None,
    atr_at_write: float | None,
    *,
    lo: float = VOL_SCALE_LO,
    hi: float = VOL_SCALE_HI,
    deadband: tuple[float, float] = VOL_SCALE_DEADBAND,
) -> float:
    """1.0 when ATR unchanged / unknown; else clipped ATR ratio."""
    if not atr_now or not atr_at_write or float(atr_at_write) <= 0:
        return 1.0
    ratio = float(atr_now) / float(atr_at_write)
    if deadband[0] <= ratio <= deadband[1]:
        return 1.0
    return max(lo, min(hi, ratio))


async def fetch_brick_state(symbol: str) -> Optional[dict[str, Any]]:
    """Latest nt_brick_params_state row (atr_at_write, geometry at write)."""
    if not settings.SUPABASE_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                _url("nt_brick_params_state"),
                headers=_headers(),
                params={
                    "select": "symbol,brick_size,sl_bricks,tp_bricks,atr_at_write,asset_class,written_at",
                    "symbol": f"eq.{symbol}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        return rows[0] if isinstance(rows, list) and rows else None
    except Exception as exc:
        logger.warning("fetch_brick_state %s: %s", symbol, exc)
        return None


async def write_eod_action(
    symbol: str,
    action: str,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Insert into nt_eod_actions (same shape as eod_correction._write_eod_action)."""
    from regime_platform.sweep_orchestrator.supabase_client import _insert_row
    from datetime import datetime, timezone
    if not settings.SUPABASE_URL:
        return False
    row = {
        "symbol": symbol,
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "bias": None,
        "detail": detail or {},
    }
    try:
        return bool(await _insert_row("nt_eod_actions", row))
    except Exception as exc:
        logger.warning("write_eod_action %s %s: %s", symbol, action, exc)
        return False
