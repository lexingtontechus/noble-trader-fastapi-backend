"""
p_win_bracket — deadline-conditioned bracket-outcome probability
═════════════════════════════════════════════════════════════════

Batch C, worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md.

Closes the category error: the EV engine's ``p_win`` is directional
confidence; realized W/L is first-passage of TP vs SL within the 48h
deadline. This module estimates the *same event* the settlement contract
labels as a win:

    p_resolve     = P(resolved | symbol, vol_tier, distance/ATR bucket)
    p_win_resolve = Beta(α,β) shrunk P(tp_hit | resolved)
    p_win_bracket = p_resolve * p_win_resolve

Unscored buckets / missing ATR → ``None`` (fail-open: never invent a
verdict; shadow gate does not reject on missing data).

Pure math is unit-tested offline; fetch is best-effort Supabase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.bracket_reachability import (
    _bucket_for,
    _vol_tier,
)
from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

logger = logging.getLogger("regime.sweep.p_win_bracket")

# Weakly informative Beta prior on P(win | resolve). α=β=2 pulls thin
# buckets toward 0.5 without dominating n≥10 cells (worklog Phase 3.1).
BETA_ALPHA = 2.0
BETA_BETA = 2.0

# Reject when the *bracket* probability of a full win is below this.
# Distinct from MIN_P_WIN (directional). Applied only when mode=live.
MIN_P_WIN_BRACKET = 0.45


@dataclass(frozen=True)
class BracketProb:
    p_resolve: float | None
    p_win_resolve: float | None
    p_win_bracket: float | None
    bucket: str | None
    vol_tier: str
    n: int
    n_stored: int
    atr_source: str  # "stored" | "legacy_stored" | "none"
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "p_resolve": self.p_resolve,
            "p_win_resolve": self.p_win_resolve,
            "p_win_bracket": self.p_win_bracket,
            "bucket": self.bucket,
            "vol_tier": self.vol_tier,
            "n": self.n,
            "n_stored": self.n_stored,
            "atr_source": self.atr_source,
            "reason": self.reason,
        }


def beta_shrunk_win_rate(wins: float, losses: float, alpha: float = BETA_ALPHA, beta: float = BETA_BETA) -> float:
    """Posterior mean of Beta(α + wins, β + losses)."""
    return (wins + alpha) / (wins + losses + alpha + beta)


def compute_bracket_probs(
    *,
    entry_price: float | None,
    take_profit: float | None,
    atr: float | None,
    regime_label: str | None,
    n: int,
    n_stored: int,
    pct_hit_tp: float | None,
    pct_hit_sl: float | None,
    pct_expired: float | None,
    min_n_stored: int = 3,
    alpha: float = BETA_ALPHA,
    beta: float = BETA_BETA,
    allow_legacy_stored: bool = True,
    row_atr_source: str | None = None,
) -> BracketProb:
    """Pure computation from one reachability bucket row.

    Bootstrap fallback (2026-09-16): when ``n_stored < min_n_stored`` but
    ``n ≥ min_n_stored`` and the row is tagged ``atr_source='stored'``
    (legacy rebuild predating the counters), use ``n`` as the sample size
    and stamp ``atr_source='legacy_stored'``. Never applies to
    ``proxy_current`` rows.
    """
    vol_tier = _vol_tier(regime_label)
    if not atr or entry_price is None or take_profit is None:
        return BracketProb(None, None, None, None, vol_tier, n, n_stored, "none", "missing_atr_or_prices")
    bucket = _bucket_for(abs(float(take_profit) - float(entry_price)) / float(atr))
    if bucket is None:
        return BracketProb(None, None, None, None, vol_tier, n, n_stored, "stored", "no_bucket")
    if pct_hit_tp is None or pct_hit_sl is None:
        return BracketProb(None, None, None, bucket, vol_tier, n, n_stored, "stored", "missing_pct")

    sample_n = n_stored
    source = "stored"
    if n_stored < min_n_stored:
        if (
            allow_legacy_stored
            and (row_atr_source or "stored") == "stored"
            and n >= min_n_stored
        ):
            sample_n = n
            source = "legacy_stored"
        else:
            return BracketProb(
                None, None, None, bucket, vol_tier, n, n_stored, "stored",
                "insufficient_stored_n",
            )

    # Prefer proportions that sum with expired ≈ 1; fall back if expired missing
    p_resolve = float(pct_hit_tp) + float(pct_hit_sl)
    if pct_expired is not None:
        p_resolve = max(0.0, min(1.0, 1.0 - float(pct_expired)))
    wins = float(sample_n) * float(pct_hit_tp)
    losses = float(sample_n) * float(pct_hit_sl)
    p_win_resolve = beta_shrunk_win_rate(wins, losses, alpha, beta)
    p_bracket = p_resolve * p_win_resolve
    return BracketProb(
        p_resolve=round(p_resolve, 4),
        p_win_resolve=round(p_win_resolve, 4),
        p_win_bracket=round(p_bracket, 4),
        bucket=bucket,
        vol_tier=vol_tier,
        n=n,
        n_stored=n_stored,
        atr_source=source,
        reason=None,
    )


def _empty(reason: str, vol_tier: str = "unknown") -> BracketProb:
    return BracketProb(None, None, None, None, vol_tier, 0, 0, "none", reason)


async def lookup_bracket_probs(
    symbol: str,
    *,
    entry_price: float | None,
    take_profit: float | None,
    atr: float | None,
    regime_label: str | None,
) -> BracketProb:
    """Fetch the matching (symbol, vol_tier, bucket) reachability row and
    compute p_win_bracket. Best-effort — never raises into the sweep tick.
    """
    vol_tier = _vol_tier(regime_label)
    if not atr or entry_price is None or take_profit is None:
        return _empty("missing_atr_or_prices", vol_tier)
    bucket = _bucket_for(abs(float(take_profit) - float(entry_price)) / float(atr))
    if bucket is None:
        return _empty("no_bucket", vol_tier)
    if not settings.SUPABASE_URL:
        return _empty("no_supabase", vol_tier)

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "n,n_stored,n_proxy,pct_hit_tp,pct_hit_sl,pct_expired,atr_source",
                    "symbol": f"eq.{symbol}",
                    "vol_tier": f"eq.{vol_tier}",
                    "distance_atr_bucket": f"eq.{bucket}",
                    "signal": "eq.all",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        logger.warning("lookup_bracket_probs %s: fetch failed: %s", symbol, exc)
        return _empty("fetch_failed", vol_tier)

    if not isinstance(rows, list) or not rows:
        return _empty("bucket_unscored", vol_tier)
    row = rows[0]
    return compute_bracket_probs(
        entry_price=entry_price,
        take_profit=take_profit,
        atr=atr,
        regime_label=regime_label,
        n=int(row.get("n") or 0),
        n_stored=int(row.get("n_stored") or 0),
        pct_hit_tp=row.get("pct_hit_tp"),
        pct_hit_sl=row.get("pct_hit_sl"),
        pct_expired=row.get("pct_expired"),
        row_atr_source=row.get("atr_source"),
    )


def would_bracket_gate_reject(
    p_win_bracket: float | None,
    *,
    min_p_win_bracket: float = MIN_P_WIN_BRACKET,
) -> bool | None:
    """True = live bracket gate would reject. None = unscored (fail-open keep)."""
    if p_win_bracket is None:
        return None
    return p_win_bracket < min_p_win_bracket
