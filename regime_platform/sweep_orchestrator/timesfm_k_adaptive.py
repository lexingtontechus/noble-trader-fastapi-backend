"""
TimesFM k-adaptive dampening (worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 12).

Confirmed via code trace before building: the sigmoid mapping that
turns TimesFM's raw forecast into ``p_timesfm`` runs entirely upstream
of ``compute_ev()`` (``orchestrator.py::_fetch_timesfm_cached``) —
``compute_ev()`` only ever receives the final probability as an opaque
float. This module never touches ``ev_engine.py``.
"""

from __future__ import annotations

import math


def compute_adaptive_timesfm_k(
    base_k: float,
    trend_strength: float | None,
    floor_k: float,
    ceiling_k: float,
) -> float:
    """Stronger trend (higher ``trend_strength``) -> HIGHER k -> a
    sharper, more confident probability mapping (trust the forecast
    more). No trend context (``None``) or a consolidating/zero-strength
    read -> k stays at ``base_k`` (today's behavior, unchanged) ->
    conservative default when there is no evidence either way. Clamped
    to ``[floor_k, ceiling_k]`` as a defensive bound.

    CORRECTED 2026-09-11: the worklog's original Part 12.3 formula was
    ``base_k / (trend_strength + epsilon)`` — DECREASING in strength.
    Caught while implementing this, not before: in a sigmoid, a
    SMALLER k flattens the curve (pushes output toward 0.5 for the same
    input), so dividing by strength made strong trends LESS confident —
    the opposite of the doc's own stated intent ("strong trend -> more
    aggressive mapping"). Verified directly:
    ``sigmoid(10*0.02)=0.5498`` vs ``sigmoid(40*0.02)=0.6900`` — higher
    k is unambiguously more aggressive (further from 0.5), not less.
    This function now scales k UP multiplicatively with strength
    instead.
    """
    strength = trend_strength if trend_strength is not None else 0.0
    strength = max(0.0, min(1.0, strength))
    k = base_k * (1.0 + strength)  # up to 2x base_k at full trend strength
    return max(floor_k, min(ceiling_k, k))


def map_forecast_to_probability(
    rel_change: float,
    k: float,
    clamp_min: float,
    clamp_max: float,
) -> float:
    """``sigmoid(k * rel_change)``, clamped — extracted from
    ``_fetch_timesfm_cached``'s previously-inline computation so both
    the real (base-k) and shadow-mode (adaptive-k) probabilities can be
    computed from the SAME already-fetched forecast, with no second
    network call.
    """
    z = k * rel_change
    if z >= 0:
        p = 1.0 / (1.0 + math.exp(-z))
    else:
        ez = math.exp(z)
        p = ez / (1.0 + ez)
    return max(clamp_min, min(clamp_max, p))
