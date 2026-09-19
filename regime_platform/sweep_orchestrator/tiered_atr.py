"""
Tiered / dynamic ATR — asset-class priors + long-lag risk-capped pick
═════════════════════════════════════════════════════════════════════

worklog/20260918_TIERED_ATR_GEOMETRY_SCOPE.md

Language (canonical):
  * Short-horizon resolve — tp/sl within 48h (platform contract)
  * Long-lag / geometry   — expired past 48h; risk from wide brackets

This module does **not** flip production geometry by itself. It:
  1. Supplies **tier priors** (asset-class × vol_tier → preferred TP ATR bands)
  2. Ranks quality-filtered combos by **geometry risk** (long-lag / p90)
  3. Returns a **risk-capped pick** for shadow comparison / later live use

Live flips of heavy_sweep_atr_anchored_bricks_mode / reachability_mode
remain roadmap-gated (nt_roadmap review_at 2026-10-05). Shadow comparison
is allowed now; dynamic_atr_mode=live is opt-in after evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from regime_platform.sweep_orchestrator.geometry_risk import (
    GEOMETRY_RISK_CAP,
    geometry_risk_hat,
)

logger = logging.getLogger("regime.sweep.tiered_atr")

# Preferred TP distance / ATR bands by (asset_class, vol_tier family).
# Priors only — risk cap still wins if the bucket is known-bad.
# vol families collapse the HMM tier string for lookup.
TIER_TP_ATR_BAND: dict[str, tuple[float, float]] = {
    "crypto": (0.5, 2.0),
    "equity": (0.5, 2.0),
    "etf": (0.5, 2.0),
    "forex": (0.3, 1.5),
    "metal": (0.5, 2.0),
    "commodity": (0.5, 2.0),
    "index": (0.5, 2.0),
    "default": (0.5, 2.0),
}

# Long-lag / geometry risk cap for accepting a pick when dynamic_atr_mode=live.
DEFAULT_RISK_CAP = GEOMETRY_RISK_CAP


def asset_class_key(asset_class: str | None) -> str:
    if not asset_class:
        return "default"
    a = str(asset_class).strip().lower()
    if "crypto" in a or a in ("btc", "coin"):
        return "crypto"
    if "forex" in a or "fx" in a or a in ("currency",):
        return "forex"
    if "metal" in a or a in ("gold", "silver", "xau", "xag"):
        return "metal"
    if "commod" in a:
        return "commodity"
    if "etf" in a:
        return "etf"
    if "index" in a or a in ("us30", "us500", "ndx"):
        return "index"
    if "equity" in a or "stock" in a or "share" in a:
        return "equity"
    return "default"


def preferred_tp_atr_band(asset_class: str | None) -> tuple[float, float]:
    return TIER_TP_ATR_BAND.get(asset_class_key(asset_class), TIER_TP_ATR_BAND["default"])


def vol_family(vol_tier: str | None) -> str:
    """Collapse HMM vol tier to a coarse family for logging/priors."""
    if not vol_tier:
        return "unknown"
    t = str(vol_tier).lower()
    if "high" in t:
        return "high"
    if "low" in t:
        return "low"
    if "med" in t or "mid" in t:
        return "med"
    return t


def in_preferred_band(tp_distance_atr: float | None, asset_class: str | None) -> bool:
    if tp_distance_atr is None:
        return False
    lo, hi = preferred_tp_atr_band(asset_class)
    return lo <= float(tp_distance_atr) <= hi


@dataclass(frozen=True)
class RiskPickResult:
    baseline_key: tuple | None
    baseline_tp_atr: float | None
    baseline_risk: float | None
    capped_key: tuple | None
    capped_tp_atr: float | None
    capped_risk: float | None
    pick_changed: bool
    nothing_survived: bool
    asset_class: str
    risk_cap: float
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_key": self.baseline_key,
            "baseline_tp_atr": self.baseline_tp_atr,
            "baseline_risk": self.baseline_risk,
            "capped_key": self.capped_key,
            "capped_tp_atr": self.capped_tp_atr,
            "capped_risk": self.capped_risk,
            "pick_changed": self.pick_changed,
            "nothing_survived": self.nothing_survived,
            "asset_class": self.asset_class,
            "risk_cap": self.risk_cap,
            "mode": self.mode,
        }


def _combo_key(c: Any) -> tuple:
    return (c.brick_size, c.sl_bricks, c.tp_bricks)


def combo_tp_distance_atr(c: Any, atr: float | None) -> Optional[float]:
    if not atr or atr <= 0:
        return None
    return abs(float(c.tp_bricks) * float(c.brick_size)) / float(atr)


def combo_geometry_risk(
    c: Any,
    atr: float | None,
    *,
    pct_expired_by_bucket: dict[str, float | None] | None = None,
    p90_by_bucket: dict[str, float | None] | None = None,
    long_lag_prior: float = 0.40,
) -> float:
    """Long-lag risk for one combo; missing bucket data uses prior."""
    from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for

    tp_atr = combo_tp_distance_atr(c, atr)
    bucket = _bucket_for(tp_atr) if tp_atr is not None else None
    lag = long_lag_prior
    p90 = None
    if bucket and pct_expired_by_bucket:
        v = pct_expired_by_bucket.get(bucket)
        if v is not None:
            lag = float(v)
    if bucket and p90_by_bucket:
        p90 = p90_by_bucket.get(bucket)
    return geometry_risk_hat(lag, p90)


def quality_filter(
    combos: Iterable[Any],
    *,
    min_rr: float,
    max_sharpe: float,
    min_trades: int,
) -> list[Any]:
    out = []
    for c in combos:
        try:
            rr = float(c.tp_bricks) / max(float(c.sl_bricks), 1.0)
            if rr < min_rr:
                continue
            if float(c.sharpe) > max_sharpe:
                continue
            if int(c.total_trades) < min_trades:
                continue
            out.append(c)
        except Exception:
            continue
    return out


def risk_capped_pick(
    combos: Iterable[Any],
    *,
    atr: float | None,
    asset_class: str | None = None,
    min_rr: float = 1.5,
    max_sharpe: float = 10.0,
    min_trades: int = 5,
    risk_cap: float = DEFAULT_RISK_CAP,
    pct_expired_by_bucket: dict[str, float | None] | None = None,
    p90_by_bucket: dict[str, float | None] | None = None,
    mode: str = "shadow",
) -> RiskPickResult:
    """Baseline = max Sharpe among quality-filtered.

    Capped pick = max Sharpe among quality-filtered **and** risk ≤ cap,
    preferring combos whose TP ATR sits in the asset-class preferred band
    (tier prior). Unscored geometry uses the prior risk (not fail-open 0).
    """
    ac = asset_class_key(asset_class)
    lo, hi = preferred_tp_atr_band(ac)
    pool = quality_filter(
        combos, min_rr=min_rr, max_sharpe=max_sharpe, min_trades=min_trades,
    )
    if not pool:
        return RiskPickResult(
            None, None, None, None, None, None, False, True,
            ac, risk_cap, mode,
        )

    baseline = max(pool, key=lambda c: c.sharpe)
    b_tp_atr = combo_tp_distance_atr(baseline, atr)
    b_risk = combo_geometry_risk(
        baseline, atr,
        pct_expired_by_bucket=pct_expired_by_bucket,
        p90_by_bucket=p90_by_bucket,
    )

    def _rank(c: Any) -> tuple:
        tp_atr = combo_tp_distance_atr(c, atr)
        risk = combo_geometry_risk(
            c, atr,
            pct_expired_by_bucket=pct_expired_by_bucket,
            p90_by_bucket=p90_by_bucket,
        )
        in_band = 0 if (tp_atr is not None and lo <= tp_atr <= hi) else 1
        return (in_band, risk, -float(c.sharpe))

    survivors = [
        c for c in pool
        if combo_geometry_risk(
            c, atr,
            pct_expired_by_bucket=pct_expired_by_bucket,
            p90_by_bucket=p90_by_bucket,
        ) <= risk_cap
    ]
    if survivors:
        capped = min(survivors, key=_rank)
        nothing = False
    else:
        # No combo under cap — return best-risk combo for shadow visibility
        capped = min(pool, key=_rank)
        nothing = True

    c_tp_atr = combo_tp_distance_atr(capped, atr)
    c_risk = combo_geometry_risk(
        capped, atr,
        pct_expired_by_bucket=pct_expired_by_bucket,
        p90_by_bucket=p90_by_bucket,
    )
    return RiskPickResult(
        baseline_key=_combo_key(baseline),
        baseline_tp_atr=round(b_tp_atr, 4) if b_tp_atr is not None else None,
        baseline_risk=round(b_risk, 4),
        capped_key=_combo_key(capped),
        capped_tp_atr=round(c_tp_atr, 4) if c_tp_atr is not None else None,
        capped_risk=round(c_risk, 4),
        pick_changed=_combo_key(capped) != _combo_key(baseline),
        nothing_survived=nothing,
        asset_class=ac,
        risk_cap=risk_cap,
        mode=mode,
    )


def should_promote_atr_candidates(
    *,
    selection_changed: bool,
    baseline_bucket: str | None,
    widened_bucket: str | None,
    pct_expired_by_bucket: dict[str, float | None] | None,
    risk_cap: float = DEFAULT_RISK_CAP,
) -> bool:
    """P0 evidence rule: allow ATR-anchored candidates into the live pick pool.

    Promote only when shadow comparison says the pick **changed** and the
    widened bucket is strictly better on long-lag (lower pct_expired) than
    baseline — or baseline is 4+ and widened is a tighter band under cap.
    Missing metrics → no promote (fail-closed for evidence).
    """
    if not selection_changed:
        return False
    exp = pct_expired_by_bucket or {}
    b_lag = exp.get(baseline_bucket) if baseline_bucket else None
    w_lag = exp.get(widened_bucket) if widened_bucket else None
    tighter = {"<0.5", "0.5-1", "1-2", "2-4"}
    if baseline_bucket == "4+" and widened_bucket in tighter:
        if w_lag is not None and float(w_lag) <= risk_cap:
            return True
        if widened_bucket in ("0.5-1", "1-2", "<0.5") and w_lag is None:
            # Tighter ATR band with unknown lag — still prefer vs known-bad 4+
            return b_lag is None or float(b_lag) > risk_cap
    if b_lag is not None and w_lag is not None:
        return float(w_lag) < float(b_lag) and float(w_lag) <= risk_cap
    return False


def compose_deadline_candidate_pool(
    grid_results: list,
    original_brick_sizes: set,
    atr_anchored_mode: str,
    *,
    promote: bool,
) -> list:
    """P0.3: pool fed into deadline-aware scoring.

    * atr_anchored_mode == live → full grid
    * shadow + evidence promote → full grid (ATR candidates eligible)
    * shadow without promote → original brick sizes only (leak-safe)
    * off → original sizes only
    """
    if atr_anchored_mode == "live" or (atr_anchored_mode != "off" and promote):
        return list(grid_results)
    return [c for c in grid_results if getattr(c, "brick_size", None) in original_brick_sizes]


async def fetch_bucket_long_lag(symbol: str) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Load (pct_expired_by_bucket, p90_by_bucket) from reachability for risk scores."""
    import httpx

    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

    exp: dict[str, float | None] = {}
    p90: dict[str, float | None] = {}
    if not settings.SUPABASE_URL:
        return exp, p90
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "distance_atr_bucket,pct_expired,p90_hours,n_stored",
                    "symbol": f"eq.{symbol}",
                    "limit": "20",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        for r in rows if isinstance(rows, list) else []:
            if int(r.get("n_stored") or 0) < 3:
                continue
            b = r.get("distance_atr_bucket")
            if not b:
                continue
            exp[b] = r.get("pct_expired")
            p90[b] = r.get("p90_hours")
    except Exception as exc:
        logger.warning("tiered_atr fetch_bucket_long_lag %s: %s", symbol, exc)
    return exp, p90
