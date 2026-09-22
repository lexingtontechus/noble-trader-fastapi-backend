"""
Tests for regime_platform/sweep_orchestrator/bracket_reachability.py's
pure helper functions — ``_bucket_for`` and ``_vol_tier`` (2026-09-13,
worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md).

Only the pure, I/O-free functions are unit-tested here (``_fetch_resolved_rows``,
``_current_atr_by_symbol``, ``build_reachability_table`` all hit Supabase/
the price fetcher and are exercised via the module's own best-effort/
CLI verification, matching this file's existing convention across the
codebase — see heavy_sweep_reachability.py's own module docstring on
why pure-function-only unit tests are the norm here).
"""

from regime_platform.sweep_orchestrator.bracket_reachability import (
    _bucket_for,
    _signal_side,
    _vol_tier,
)


# ─── _bucket_for ────────────────────────────────────────────────────────


def test_bucket_for_boundaries():
    assert _bucket_for(0.0) == "<0.5"
    assert _bucket_for(0.49) == "<0.5"
    assert _bucket_for(0.5) == "0.5-1"
    assert _bucket_for(0.99) == "0.5-1"
    assert _bucket_for(1.0) == "1-2"
    assert _bucket_for(1.99) == "1-2"
    assert _bucket_for(2.0) == "2-4"
    assert _bucket_for(3.99) == "2-4"
    assert _bucket_for(4.0) == "4+"
    assert _bucket_for(100.0) == "4+"


# ─── _vol_tier ──────────────────────────────────────────────────────────


def test_vol_tier_all_four_tiers():
    assert _vol_tier("low_vol_bull") == "low_vol"
    assert _vol_tier("med_low_vol_strong_bear") == "med_low_vol"
    assert _vol_tier("med_high_vol_strong_bull") == "med_high_vol"
    assert _vol_tier("high_vol_bear") == "high_vol"


def test_vol_tier_all_trend_labels_for_one_tier():
    # trend_state doesn't affect the extraction — only the vol_state prefix does.
    for trend in ("strong_bear", "bear", "bull", "strong_bull"):
        assert _vol_tier(f"low_vol_{trend}") == "low_vol"


def test_vol_tier_missing_or_unparseable_resolves_to_unknown():
    assert _vol_tier(None) == "unknown"
    assert _vol_tier("") == "unknown"
    assert _vol_tier("unknown") == "unknown"  # the pre-bootstrap fallback label itself
    # No literal "_vol_" marker at all -> falls all the way through to "unknown".
    assert _vol_tier("garbage") == "unknown"


def test_vol_tier_is_a_pure_string_split_not_case_sensitive_magic():
    # Confirms the split point is literally "_vol_" — verifies no hidden
    # normalization/whitespace handling is silently relied upon.
    assert _vol_tier("med_low_vol_bull") == "med_low_vol"
    assert "_vol_" in "med_low_vol_bull"


# ─── _signal_side (migration 159 BUY vs SELL) ────────────────────────────


def test_signal_side_buy_sell_passthrough():
    assert _signal_side("buy") == "buy"
    assert _signal_side("sell") == "sell"
    assert _signal_side("BUY") == "buy"
    assert _signal_side(" Sell ") == "sell"


def test_signal_side_non_side_collapses_to_other():
    # neutral / missing / garbage do not become a fake side; the builder
    # still folds them into the pooled signal='all' rollup only.
    assert _signal_side("neutral") == "other"
    assert _signal_side(None) == "other"
    assert _signal_side("") == "other"
    assert _signal_side("hold") == "other"
