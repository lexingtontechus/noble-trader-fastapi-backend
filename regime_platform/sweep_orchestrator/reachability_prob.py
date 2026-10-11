"""Reachability-conditioned probability — the calibration reframe (R-build).

══════════════════════════════════════════════════════════════════════════
2026-10-11. worklog/20261011_REACHABILITY_CONDITIONED_CALIBRATION.md.
Roadmap: calibration_reachability_reframe.

THE REFRAME
───────────
Calibration and reachability are ONE problem, measured twice. A direction-only
``p_win`` — P(this direction resolves TP) — priced against a book where the
bracket is usually geometrically unreachable WILL read as overconfident,
because the realized win rate is capped by GEOMETRY and TIME, not direction
skill. Era-clean: WR 12.5%, bias +0.10..+0.33 on every symbol; tight brackets
(<2 ATR) stop out 91-100%; only the 4+ bucket reaches 19% TP (30% of which
expire). The overconfidence is largely an ARTIFACT of pricing direction
independently of reachability.

THE FIX
───────
Compute ONE probability that already conditions on whether the bracket is
reachable:

    P_reach = P(reach TP before SL before expiry | symbol, vol_tier,
                distance_atr_bucket, expiry_hours)

  * The cell's era-clean TP-hit share IS P(TP before SL before expiry) for
    that (symbol, vol_tier, bucket) — it already nets out expiry, so it is NOT
    re-discounted (that would double-count the leash).
  * Expiry enters as a SEPARATE monotonic factor via the cell's own p90: a
    signal held past the cell's p90 resolution time gets a haircut (the tail
    that would have resolved later is cut off); one held within it does not.
    Never inflates.
  * Direction is carried as a light logit adjustment on top: within a
    reachable cell, does the direction still have an edge? The era-clean
    XAUUSD buy trending_down 38.2% vs consolidating 8.1% gap is a within-cell
    direction effect the base rate alone would wash out.

This is the number that should enter EV/Kelly, replacing the direction-only
p_win. It reprices the geometry honestly — which is what actually moves a
negative-expectancy book, versus a gate that only trims.

SHADOW / FAIL-OPEN. This module computes and returns the probability; wiring
it into EV/Kelly as the live p_win is a separate, gated flip (not done here).
Every entry point returns None on any miss (cold cell, bad inputs) so a caller
falls back to the existing blend — it never invents a probability and never
raises into the sweep tick.
══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("regime.sweep.reachability_prob")

# Minimum cell sample before the cell's rate is trusted. Below this the cell
# is "cold" and we fail open (None) rather than price on noise. Mirrors the
# platform's existing calibration floors (nt_calibration_bias_by_bucket min_n).
MIN_CELL_N = 20


@dataclass(frozen=True)
class ReachProbability:
    """The reachability-conditioned probability for one signal."""

    p_reach: float            # P(reach TP before SL before expiry), 0..1
    cell_n: int               # sample size of the matched cell
    cell_tp_share: float      # the cell's raw TP-hit share (pre-direction)
    expiry_factor: float      # the p90-based expiry haircut (<=1.0)
    direction_adj: float      # the logit-space direction adjustment applied
    matched: bool             # True if a real cell matched (vs fallback)
    reason: str

    def as_log(self) -> str:
        return (
            f"p_reach={self.p_reach:.4f} n={self.cell_n} "
            f"tp_share={self.cell_tp_share:.3f} exp={self.expiry_factor:.3f} "
            f"dir_adj={self.direction_adj:+.3f} ({self.reason})"
        )


def _logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _expiry_factor(expiry_hours: float | None, p90_hours: float | None) -> float:
    """Monotonic expiry haircut in (0, 1].

    1.0 when the leash covers the cell's p90 resolution time; a shorter leash
    scales down proportionally (the tail that would have resolved later is cut
    off by expiry). Never inflates above 1.0. Unknown p90 or expiry -> 1.0
    (no haircut — fail-open to the raw cell rate).
    """
    if not expiry_hours or not p90_hours or p90_hours <= 0:
        return 1.0
    return min(1.0, float(expiry_hours) / float(p90_hours))


def _direction_adjustment(
    cell_tp_share: float, direction_edge: float | None, *, weight: float = 0.5
) -> float:
    """Logit-space nudge for the direction's within-cell edge.

    ``direction_edge`` is the signal direction's realized edge within the
    SAME regime context, expressed as (WR_direction - WR_opposite) in [-1, 1]
    (None when unknown -> 0 adjustment). Positive edge pushes P up, negative
    pushes down, scaled by ``weight`` so a direction read modulates but never
    dominates the reachability base rate. This is what preserves the
    trending_down-vs-consolidating gap WITHOUT letting a direction-only signal
    re-introduce the overconfidence the reframe removes.
    """
    if direction_edge is None:
        return 0.0
    # Centre the edge on the cell base rate: only the EXCESS over the cell's
    # own hit rate is a direction effect.
    excess = float(direction_edge)
    return weight * excess


def reachability_conditioned_probability(
    reach_rows: list[dict[str, Any]],
    *,
    symbol: str,
    vol_tier: str,
    distance_bucket: str,
    expiry_hours: float | None = None,
    direction_edge: float | None = None,
    signal: str = "all",
    min_n: int = MIN_CELL_N,
) -> Optional[ReachProbability]:
    """P(reach TP before SL before expiry | symbol, vol_tier, bucket, expiry).

    Looks up the era-clean reachability cell, takes its TP-hit share as the
    base rate, applies the monotonic expiry haircut, then a light logit-space
    direction adjustment. Returns None (fail-open) when no cell matches or the
    cell is too thin — the caller keeps its existing p_win, never a fabricated
    number.

    ``signal`` selects the side-specific row (buy/sell) when present, else the
    pooled 'all' row (migration 159 split). ``direction_edge`` is optional
    within-cell direction skill; None -> base rate only.
    """
    if not reach_rows or not symbol:
        return None

    sym = str(symbol)
    # Prefer the side-specific row, fall back to the pooled 'all' row.
    def _match(want_signal: str) -> dict[str, Any] | None:
        for r in reach_rows:
            if (r.get("symbol") == sym
                    and r.get("vol_tier") == vol_tier
                    and r.get("distance_atr_bucket") == distance_bucket
                    and r.get("signal", "all") == want_signal):
                return r
        return None

    row = _match(signal) or _match("all")
    if row is None:
        return None

    n = int(row.get("n") or 0)
    tp_share = row.get("pct_hit_tp")
    if tp_share is None:
        # Derive from counts if the pct column is absent.
        closed = n  # reachability n counts resolved-in-bucket episodes
        tp_share = 0.0 if not closed else float(row.get("n_tp") or 0) / closed
    tp_share = float(tp_share)
    if n < min_n:
        return ReachProbability(
            p_reach=tp_share, cell_n=n, cell_tp_share=tp_share,
            expiry_factor=1.0, direction_adj=0.0, matched=False,
            reason=f"cold cell n={n}<{min_n}",
        )

    p90 = row.get("p90_hours")
    exp_f = _expiry_factor(expiry_hours, p90)
    base = tp_share * exp_f

    dir_adj = _direction_adjustment(base, direction_edge)
    # Apply the direction adjustment in logit space around the base rate.
    p_reach = _sigmoid(_logit(base) + dir_adj) if 0.0 < base < 1.0 else base
    p_reach = min(max(p_reach, 0.0), 1.0)

    return ReachProbability(
        p_reach=round(p_reach, 4),
        cell_n=n,
        cell_tp_share=round(tp_share, 4),
        expiry_factor=round(exp_f, 4),
        direction_adj=round(dir_adj, 4),
        matched=True,
        reason=f"cell n={n} tp={tp_share:.3f}",
    )


def compare_to_blend(
    p_reach: float | None, p_win_blend: float
) -> dict[str, Any]:
    """Diagnostic: how far the reachability-conditioned probability sits from
    the current direction-only blend. Positive delta = the blend OVERPRICES
    vs reachability (the overconfidence the reframe removes).

    Never raises; p_reach None -> {'delta': None} (no reachability read).
    """
    if p_reach is None:
        return {"delta": None, "direction": "no_reach_read"}
    delta = round(float(p_win_blend) - float(p_reach), 4)
    return {
        "delta": delta,
        "direction": "overpriced" if delta > 0 else "underpriced",
        "p_reach": p_reach,
        "p_win_blend": p_win_blend,
    }


async def lookup_reach_prob(
    symbol: str,
    *,
    vol_tier: str,
    distance_bucket: str,
    expiry_hours: float | None = None,
    direction_edge: float | None = None,
    signal: str = "all",
) -> Optional[ReachProbability]:
    """Fetch the matching reachability cell and compute P_reach. Best-effort —
    never raises into the sweep tick (same contract as lookup_bracket_probs).

    Shadow-stamp helper: the sweep calls this to stamp p_reach on
    nt_signal_sim for measurement. Fails open (None) on any miss.
    """
    try:
        import httpx
        from regime_platform.core.settings import settings
        from regime_platform.sweep_orchestrator.supabase_client import _headers, _url

        if not settings.SUPABASE_URL or not symbol or not distance_bucket:
            return None
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                _url("nt_bracket_reachability"),
                headers=_headers(),
                params={
                    "select": "symbol,vol_tier,distance_atr_bucket,signal,n,"
                              "pct_hit_tp,pct_hit_sl,pct_expired,p90_hours",
                    "symbol": f"eq.{symbol}",
                    "vol_tier": f"eq.{vol_tier}",
                    "distance_atr_bucket": f"eq.{distance_bucket}",
                    "signal": f"eq.{signal}",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None
        return reachability_conditioned_probability(
            rows, symbol=symbol, vol_tier=vol_tier,
            distance_bucket=distance_bucket, expiry_hours=expiry_hours,
            direction_edge=direction_edge, signal=signal,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reach:prob] lookup %s failed: %s", symbol, exc)
        return None
