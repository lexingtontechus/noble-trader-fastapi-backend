"""Tests for the context-alignment gate (regime_platform/sweep_orchestrator/
context_alignment.py), worklog/20260910_LONGITUDINAL_CONTEXT_AND_BRICK_GEOMETRY_REVIEW.md
Part 10.

``compute_context_alignment`` is a pure function (no I/O) — tested
here directly. It never reads or writes p_win/ev_result, so there is
no Supabase-backed component to this module at all (unlike
macro_filter.py/trend_filter.py, which each have a refresh/get pair);
this file is the complete test suite.
"""

from pytest import approx as pytest_approx

from regime_platform.sweep_orchestrator.context_alignment import (
    compute_context_alignment,
)


def test_neutral_signal_is_always_aligned():
    assert compute_context_alignment("neutral", "bear", "trending_down") is True
    assert compute_context_alignment("unknown", "bear", "trending_down") is True


def test_both_oppose_buy_fails():
    # Buy implies bull; bear macro + a bearish trend label both oppose it.
    assert compute_context_alignment("buy", "bear", "trending_down") is False
    assert compute_context_alignment("buy", "bear", "reversal_down") is False


def test_both_oppose_sell_fails():
    assert compute_context_alignment("sell", "bull", "trending_up") is False
    assert compute_context_alignment("sell", "bull", "reversal_up") is False


def test_only_macro_opposes_still_passes():
    # A single noisy source disagreeing is handled proportionally by the
    # existing Kelly multipliers, not an outright reject here.
    assert compute_context_alignment("buy", "bear", "trending_up") is True
    assert compute_context_alignment("buy", "bear", None) is True
    assert compute_context_alignment("buy", "bear", "consolidating") is True


def test_only_trend_opposes_still_passes():
    assert compute_context_alignment("buy", "bull", "trending_down") is True
    assert compute_context_alignment("buy", None, "trending_down") is True
    assert compute_context_alignment("buy", "neutral", "trending_down") is True


def test_both_aligned_passes():
    assert compute_context_alignment("buy", "bull", "trending_up") is True
    assert compute_context_alignment("sell", "bear", "reversal_down") is True


def test_neutral_or_missing_macro_never_opposes():
    assert compute_context_alignment("buy", "neutral", "trending_down") is True
    assert compute_context_alignment("buy", None, "trending_down") is True
    assert compute_context_alignment("sell", "neutral", "trending_up") is True


def test_consolidating_or_missing_trend_never_opposes():
    assert compute_context_alignment("buy", "bear", "consolidating") is True
    assert compute_context_alignment("buy", "bear", None) is True
    assert compute_context_alignment("sell", "bull", "consolidating") is True


def test_both_neutral_or_missing_passes():
    assert compute_context_alignment("buy", None, None) is True
    assert compute_context_alignment("buy", "neutral", "consolidating") is True


def test_return_type_is_plain_bool_not_numpy_or_none():
    # Downstream code (gate_results dict, nt_signal_sim capture) expects
    # a plain bool, not a numpy bool_ or an Optional that could sneak a
    # None into a boolean column.
    result = compute_context_alignment("buy", "bear", "trending_down")
    assert result is False
    assert isinstance(result, bool)
    result2 = compute_context_alignment("buy", "bull", "trending_up")
    assert isinstance(result2, bool)
