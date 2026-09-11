"""Tests for TimesFM k-adaptive dampening
(regime_platform/sweep_orchestrator/timesfm_k_adaptive.py), worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 12.
"""

from pytest import approx as pytest_approx

from regime_platform.sweep_orchestrator.timesfm_k_adaptive import (
    compute_adaptive_timesfm_k,
    map_forecast_to_probability,
)


# ─── compute_adaptive_timesfm_k ──────────────────────────────────────────


def test_strong_trend_raises_k_above_base():
    k = compute_adaptive_timesfm_k(base_k=20.0, trend_strength=1.0, floor_k=5.0, ceiling_k=60.0)
    assert k == pytest_approx(40.0)
    assert k > 20.0


def test_no_trend_context_stays_at_base_k():
    # No evidence either way -> today's unchanged default, not an extreme.
    k = compute_adaptive_timesfm_k(base_k=20.0, trend_strength=None, floor_k=5.0, ceiling_k=60.0)
    assert k == pytest_approx(20.0)


def test_zero_trend_strength_stays_at_base_k():
    k = compute_adaptive_timesfm_k(base_k=20.0, trend_strength=0.0, floor_k=5.0, ceiling_k=60.0)
    assert k == pytest_approx(20.0)


def test_partial_trend_strength_scales_linearly():
    k = compute_adaptive_timesfm_k(base_k=20.0, trend_strength=0.5, floor_k=5.0, ceiling_k=60.0)
    assert k == pytest_approx(30.0)


def test_result_never_above_ceiling():
    k = compute_adaptive_timesfm_k(base_k=40.0, trend_strength=1.0, floor_k=5.0, ceiling_k=60.0)
    assert k == 60.0


def test_out_of_range_strength_is_clamped_before_scaling():
    # Defends the formula against an out-of-[0,1] input, same clamp
    # discipline as kelly_trend_multiplier's strength handling.
    k = compute_adaptive_timesfm_k(base_k=20.0, trend_strength=5.0, floor_k=5.0, ceiling_k=100.0)
    assert k == pytest_approx(40.0)  # clamped to strength=1.0 -> 2x base_k, not 6x


# ─── map_forecast_to_probability ─────────────────────────────────────────


def test_zero_rel_change_is_half():
    assert map_forecast_to_probability(0.0, k=20.0, clamp_min=0.001, clamp_max=0.999) == pytest_approx(0.5)


def test_positive_rel_change_above_half():
    p = map_forecast_to_probability(0.05, k=20.0, clamp_min=0.001, clamp_max=0.999)
    assert p > 0.5


def test_negative_rel_change_below_half():
    p = map_forecast_to_probability(-0.05, k=20.0, clamp_min=0.001, clamp_max=0.999)
    assert p < 0.5


def test_symmetric_around_half():
    p_up = map_forecast_to_probability(0.05, k=20.0, clamp_min=0.001, clamp_max=0.999)
    p_down = map_forecast_to_probability(-0.05, k=20.0, clamp_min=0.001, clamp_max=0.999)
    assert p_up == pytest_approx(1.0 - p_down)


def test_clamp_bounds_respected():
    p_high = map_forecast_to_probability(10.0, k=20.0, clamp_min=0.05, clamp_max=0.95)
    assert p_high == pytest_approx(0.95)
    p_low = map_forecast_to_probability(-10.0, k=20.0, clamp_min=0.05, clamp_max=0.95)
    assert p_low == pytest_approx(0.05)


def test_lower_k_produces_less_extreme_probability_for_same_rel_change():
    # This is the whole point of the adaptation: a lower k (strong-trend
    # case) should map the SAME rel_change to a value closer to 0.5 than
    # a higher k would -- i.e. LESS aggressive, not more. Confirm the
    # direction explicitly rather than assuming.
    p_low_k = map_forecast_to_probability(0.02, k=10.0, clamp_min=0.001, clamp_max=0.999)
    p_high_k = map_forecast_to_probability(0.02, k=40.0, clamp_min=0.001, clamp_max=0.999)
    assert abs(p_low_k - 0.5) < abs(p_high_k - 0.5)
