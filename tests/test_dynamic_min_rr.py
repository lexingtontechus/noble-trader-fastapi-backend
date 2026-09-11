"""Tests for dynamic/regime-conditional MIN_RR
(regime_platform/sweep_orchestrator/dynamic_min_rr.py), worklog
20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md Part 11.
"""

from pytest import approx as pytest_approx

from regime_platform.sweep_orchestrator.dynamic_min_rr import (
    compute_effective_min_rr,
)


def test_both_no_edge_raises_floor():
    assert compute_effective_min_rr(1.5, 2.0, "neutral", "consolidating") == 2.0
    assert compute_effective_min_rr(1.5, 2.0, None, None) == 2.0


def test_macro_edge_present_keeps_base():
    assert compute_effective_min_rr(1.5, 2.0, "bull", "consolidating") == 1.5
    assert compute_effective_min_rr(1.5, 2.0, "bear", None) == 1.5


def test_trend_edge_present_keeps_base():
    assert compute_effective_min_rr(1.5, 2.0, "neutral", "trending_up") == 1.5
    assert compute_effective_min_rr(1.5, 2.0, None, "reversal_down") == 1.5


def test_both_edges_present_keeps_base():
    assert compute_effective_min_rr(1.5, 2.0, "bull", "trending_up") == 1.5


def test_never_returns_below_base_even_if_consolidation_value_is_lower():
    # One-directional by design: this should never happen with sane
    # config, but the function must not silently loosen risk controls.
    assert compute_effective_min_rr(2.5, 2.0, "neutral", "consolidating") == 2.5


def test_returns_plain_float():
    result = compute_effective_min_rr(1.5, 2.0, None, None)
    assert isinstance(result, float)
    assert result == pytest_approx(2.0)
