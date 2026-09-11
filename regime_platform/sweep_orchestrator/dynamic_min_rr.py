"""
Dynamic/regime-conditional MIN_RR (worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 11).

Confirmed via code trace before building: ``min_rr`` is consulted only
in ``compute_ev()``/``recompute_ev_fields()``'s post-blend allow/deny
gate (``ev_engine.py``), never the Steps 1-3 logit-pool blend — this
module never touches ``p_win``/``ev_result`` beyond that existing gate
mechanism, and does not require reworking ``ev_engine.py``'s blend
logic in any way.

Real gap this closes: the light sweep has no independent RR check of
its own — ``ev_allowed`` was, before this, a pass-through of whatever
RR the already-fixed brick geometry (chosen once/day by the heavy
sweep) produced, with zero regime awareness in either sweep path.
"""

from __future__ import annotations


def compute_effective_min_rr(
    base_min_rr: float,
    consolidation_min_rr: float,
    macro_label: str | None,
    trend_label: str | None,
) -> float:
    """Raise the RR floor when there is no directional edge to ride from
    either longitudinal filter; never lower it below ``base_min_rr``.

    Deliberately one-directional — a confirmed trend does not relax
    risk controls, it only avoids tightening them unnecessarily. Only
    raises the floor when BOTH filters read no-edge (neutral macro AND
    consolidating/missing trend), same "require both, don't act on one
    noisy source alone" philosophy as ``context_alignment.py``.
    """
    no_macro_edge = macro_label is None or macro_label == "neutral"
    no_trend_edge = trend_label is None or trend_label == "consolidating"
    if no_macro_edge and no_trend_edge:
        return max(base_min_rr, consolidation_min_rr)
    return base_min_rr
