"""
first_passage.py — P(TP before SL) as a first-class, geometry-free read.
═══════════════════════════════════════════════════════════════════════════════

Phase 1b of `worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md`
(option E: first-passage replaces the HMM).

The thesis (scope §2.4): ``p_markov_hold_n`` asked *P(reach TP within N bars |
current state)* via ``T^N[current][target]`` — a matrix power over a discretised
3-state abstraction whose source model measured **below** the naive base rate
(0.525 vs 0.5655; see the RM-1 evaluation). ``tiered_atr.walk()`` answers the
same question **on the actual price path**, with no states, no EM, no
covariance assumption and no refit instability — and it is already validated in
production as the geometry selector.

This module promotes that arithmetic to a probability read.

What is genuinely new here (scope §2.1) — do not conflate with the geometry work:

======================  ==============================  =========================
                        grid selection (tiered_atr)      first-passage source (here)
======================  ==============================  =========================
unit                    a whole symbol/class, 10–45d      one signal, at entry
output                  the best (tp, sl) pair            P(TP before SL) for the
                                                          signal's LIVE bracket
conditioning            pooled                            per-signal (τ, type)
consumer                geometry                          EV blend
======================  ==============================  =========================

``walk()`` is asked "which bracket is best?"; this module is asked "what is the
probability, for this bracket, right now?" Different unit, different question —
which is why this needs its own ΔD evaluation and cannot inherit the geometry
work's credibility (scope risk R2).

LIVE WIRING: **none.** Phase 1 is offline-only (scope risk R1 — ``walk()`` needs
a full 48-bar window, which a live signal does not have). Any future live stamp
must use trailing paths only, the same no-lookahead rule ``build_paths()``
already enforces. Nothing in this module is imported by the sweep path.

Pure functions only — no I/O, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from regime_platform.sweep_orchestrator.kairos_rank import (
    freshness_score,
    type_score,
)
from regime_platform.sweep_orchestrator.tiered_atr import EXPIRY_BARS

# ─── Frozen weights ────────────────────────────────────────────────────────
# Declared BEFORE any scoring, same discipline as kairos_rank.rank_score
# ("weights frozen before scoring"). Phase 2 measures this composition; it is
# not re-fit per day (overfit risk, scope risk R5).

# How strongly the first-passage LEVEL is carried into the blend. A signal
# entering fresh ∧ dir_flip carries its level at full strength; deep ∧
# vol_contract carries almost none. Floor is deliberately > 0: the level is
# still evidence even when the timing prior is weak — the prior modulates
# confidence, it does not zero the read.
FP_OVERLAY_W: dict[str, float] = {
    "floor": 0.25,      # minimum carry when freshness/type are both weakest
    "ceiling": 1.00,    # maximum carry at fresh ∧ dir_flip
}


@dataclass(frozen=True)
class FirstPassageRead:
    """P(TP before SL) for one bracket, with the evidence behind it."""

    p_tp_first: float        # P(TP before SL), expiry excluded from the numerator
    expiry_share: float      # share of paths that touched neither by bar 48
    n_paths: int
    tp_atr: float
    sl_atr: float

    def as_log(self) -> str:
        return (
            f"p_tp_first={self.p_tp_first:.4f} expiry={self.expiry_share:.1%} "
            f"n={self.n_paths} tp={self.tp_atr:g}ATR sl={self.sl_atr:g}ATR"
        )


# ─── Core ──────────────────────────────────────────────────────────────────


def first_passage(
    paths: np.ndarray,
    tp_atr: float,
    sl_atr: float,
    *,
    min_paths: int = 20,
) -> FirstPassageRead | None:
    """P(TP before SL) over direction-signed, ATR-scaled close paths.

    ``paths`` is (n, EXPIRY_BARS) where each row is
    ``(close - entry) * dir / atr`` — exactly the shape ``tiered_atr.build_paths``
    produces. Reuses ``walk()``'s first-touch rule (TP first on a same-bar tie,
    48-bar expiry) so a probability computed here and a grid picked there can
    never disagree about what "hit" means.

    Returns ``None`` when there is not enough evidence (``min_paths``) or the
    bracket is degenerate — the caller's blend should renormalise around a
    missing source rather than substitute a neutral 0.5 (the same contract
    ``EVEngine.compute_p_markov_hold_n`` used).

    Denominator is **decided** paths only (TP-first + SL-first). Expired paths
    are excluded from ``p_tp_first`` and reported separately as
    ``expiry_share`` — the same "expired is neither a win nor a loss" rule used
    throughout this platform (``_realized_direction``, the EOD resolver's Brier,
    the Kairos scorecards).
    """
    if paths is None or len(paths) == 0:
        return None
    try:
        tp = float(tp_atr)
        sl = float(sl_atr)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(tp) and math.isfinite(sl)) or tp <= 0 or sl <= 0:
        return None

    arr = np.asarray(paths, dtype="float64")
    if arr.ndim != 2 or arr.shape[0] < min_paths:
        return None

    hit_tp = arr >= tp
    hit_sl = arr <= -sl
    ti = np.where(hit_tp.any(1), hit_tp.argmax(1), EXPIRY_BARS)
    si = np.where(hit_sl.any(1), hit_sl.argmax(1), EXPIRY_BARS)
    tp_first = (ti < EXPIRY_BARS) & (ti <= si)
    sl_first = (~tp_first) & (si < EXPIRY_BARS)

    n_tp = int(tp_first.sum())
    n_sl = int(sl_first.sum())
    decided = n_tp + n_sl
    if decided <= 0:
        # Every path expired — the bracket is unreachable on this evidence.
        # Report the read with p undefined-but-honest rather than None: the
        # expiry share is itself the finding (a signal whose bracket never
        # resolves is a geometry problem, not a missing-source problem).
        return FirstPassageRead(
            p_tp_first=0.0,
            expiry_share=1.0,
            n_paths=int(arr.shape[0]),
            tp_atr=tp,
            sl_atr=sl,
        )

    return FirstPassageRead(
        p_tp_first=n_tp / decided,
        expiry_share=1.0 - decided / int(arr.shape[0]),
        n_paths=int(arr.shape[0]),
        tp_atr=tp,
        sl_atr=sl,
    )


# ─── D5: the regime-transition reinforcement ───────────────────────────────


def transition_carry(
    window: str | None,
    ttype: str | None,
) -> float:
    """How much of the first-passage LEVEL to carry, given the transition.

    D5 (`worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md` §3):
    first-passage gives the *level*, the transition gives the *timing weight*.
    Both inputs are the Kairos-proven findings, reused verbatim rather than
    re-derived:

      * **freshness** — ``fresh − deep`` y_acc = +0.117 [+0.105, +0.129]
        (T1 supported; n=24,768 / 5,188).
      * **type** — ``dir_flip`` is the only non-negative transition type
        (+0.007); ``vol_expand`` / ``vol_contract`` are worse than average.
        Surprise was REJECTED (Spearman 0.004) and is deliberately not used.

    Returns ``[floor, ceiling]`` — never 0, because the level remains evidence
    even under a weak timing prior.
    """
    fresh = freshness_score(window)   # 0.00 deep … 1.00 fresh
    typ = type_score(ttype)           # 0.25 vol-only … 1.00 dir_flip

    # Geometric mean, not arithmetic: a strong freshness with a weak type (or
    # vice versa) should NOT carry as much as both being strong. Multiplicative
    # combination matches how the two were found to interact (fresh ∧ dir_flip
    # is the supported cell; either alone is weaker).
    combined = math.sqrt(max(0.0, fresh) * max(0.0, typ))

    floor = FP_OVERLAY_W["floor"]
    ceiling = FP_OVERLAY_W["ceiling"]
    return floor + (ceiling - floor) * combined


def first_passage_score(
    read: FirstPassageRead | None,
    window: str | None,
    ttype: str | None,
) -> float | None:
    """Shrink the first-passage level toward 0.5 by ``transition_carry``.

    The reinforced read: a signal entering ``fresh ∧ dir_flip`` keeps its level
    almost intact; one entering ``deep ∧ vol_contract`` is pulled most of the
    way back toward no-information.

    Returns ``None`` when there is no read (missing-source contract — the blend
    renormalises around it rather than treating a shrunk 0.5 as data).

    This is the Phase-1 *candidate*, not a live input. Phase 2 measures whether
    it beats the base-rate control and whether the overlay adds anything over
    ``read.p_tp_first`` alone (scope bar B5 — a source can be individually
    accurate and still add nothing to a correlated blend).
    """
    if read is None:
        return None
    carry = transition_carry(window, ttype)
    level = max(0.0, min(1.0, float(read.p_tp_first)))
    return 0.5 + carry * (level - 0.5)


def evaluate_rows(
    rows: Sequence[dict[str, Any]],
    *,
    paths_by_id: dict[str, np.ndarray] | None = None,
    min_paths: int = 20,
) -> list[dict[str, Any]]:
    """Offline Phase-2 helper: score decided rows, no I/O.

    Each row needs ``tp_distance_atr`` / ``sl_distance_atr`` (the live bracket)
    and its own path array (keyed by ``signal_id`` in ``paths_by_id``). Rows
    without paths or a bracket are skipped, never imputed — the same
    "no invented priors" rule the Kairos tracker follows.

    Returns one dict per scored row with both the bare level and the reinforced
    score, so Phase 2 can measure the overlay's increment separately (B5).
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        sid = row.get("signal_id")
        paths = (paths_by_id or {}).get(sid) if sid else None
        if paths is None:
            continue
        try:
            tp = float(row["tp_distance_atr"])
            sl = float(row["sl_distance_atr"])
        except (KeyError, TypeError, ValueError):
            continue
        read = first_passage(paths, tp, sl, min_paths=min_paths)
        if read is None:
            continue
        out.append({
            "signal_id": sid,
            "symbol": row.get("symbol"),
            "window": row.get("window"),
            "transition_type": row.get("transition_type"),
            "p_tp_first": read.p_tp_first,
            "expiry_share": read.expiry_share,
            "n_paths": read.n_paths,
            "carry": transition_carry(row.get("window"), row.get("transition_type")),
            "fp_score": first_passage_score(read, row.get("window"), row.get("transition_type")),
        })
    return out
