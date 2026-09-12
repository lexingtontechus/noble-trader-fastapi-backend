"""
Redundant-computation detector — Phase 2, detector 3 of 3
(worklog/20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS2,
self_learning_phase_2_detectors, RM-37).

This codebase already has several "shadow compare" tables recording
whether two parallel mechanisms (a legacy computation vs. a new one)
AGREE with each other on real production data
(``nt_markov_source_shadow``, ``nt_regime_debounce_shadow``,
``nt_heavy_sweep_atr_anchored_shadow``,
``nt_heavy_sweep_deadline_aware_shadow``,
``nt_heavy_sweep_reachability_shadow``). Those tables exist to answer
"do these differ enough to matter before flipping shadow->live" — this
detector reuses the SAME data for the opposite question: if two
computations agree almost every time on real data, one of them may be
redundant and worth consolidating (the exact shape of the
2026-09-09 p_historical/apply_calibration_adjustment consolidation,
done by hand — this detector would have flagged that pair
automatically had it existed then).
"""

from __future__ import annotations

from typing import Any


def detect_redundant_computation(
    pair_name: str,
    n_compared: int,
    n_disagree: int,
    min_redundancy_threshold: float = 0.95,
    min_sample_size: int = 20,
) -> dict[str, Any] | None:
    """Flags a comparison pair as possibly redundant when its
    agreement rate (``1 - n_disagree/n_compared``) is at or above
    ``min_redundancy_threshold`` — i.e., the two computations almost
    always produce the same result on real data, so keeping both may
    not be adding information.

    Requires ``n_compared >= min_sample_size`` before drawing any
    conclusion — a tiny sample agreeing 100% of the time is not
    evidence of redundancy, just insufficient data. Returns ``None``
    when there's too little data, or when the two computations
    genuinely disagree often enough to justify keeping both.
    """
    if n_compared < min_sample_size:
        return None

    agreement_rate = 1.0 - (n_disagree / n_compared)
    if agreement_rate < min_redundancy_threshold:
        return None

    return {
        "pair_name": pair_name,
        "n_compared": n_compared,
        "n_disagree": n_disagree,
        "agreement_rate": round(agreement_rate, 4),
    }
