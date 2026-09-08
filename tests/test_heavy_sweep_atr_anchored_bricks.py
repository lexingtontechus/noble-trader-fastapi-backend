"""
Tests for Phase A of worklog
20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md
(regime_platform/sweep_orchestrator/heavy_sweep.py:
``generate_atr_anchored_brick_sizes`` / ``select_best_combo_with_atr_anchoring``).

Both are pure functions — tested here with real ``ComboResult`` instances,
explicit min_rr/max_sharpe/min_trades (not the module's live
config/feature_flags.yaml-derived defaults) so these tests never drift if
that config changes.
"""

from regime_platform.sweep_orchestrator.heavy_sweep import (
    ComboResult,
    generate_atr_anchored_brick_sizes,
    select_best_combo_with_atr_anchoring,
)

_FILTERS = dict(min_rr=1.0, max_sharpe=10.0, min_trades=1)


def _combo(brick_size, sharpe, sl_bricks=3, tp_bricks=6, total_trades=20):
    return ComboResult(
        brick_size=brick_size, sl_bricks=sl_bricks, tp_bricks=tp_bricks,
        sharpe=sharpe, total_trades=total_trades, win_rate=0.5,
    )


# ─── generate_atr_anchored_brick_sizes ────────────────────────────────────


def test_atr_anchored_sizes_zero_atr_returns_empty():
    assert generate_atr_anchored_brick_sizes(0.0) == []
    assert generate_atr_anchored_brick_sizes(-1.0) == []


def test_atr_anchored_sizes_returns_point15_point3_point6_x_atr():
    # 2026-09-07 correction: these multipliers (not the original
    # 0.5/1.0/2.0) are chosen so tp_bricks=5 (the grid's own minimum
    # viable value under MIN_RR) lands inside "0.5-1"/"1-2"/"2-4" —
    # confirmed necessary by a real-data backfill, see heavy_sweep.py's
    # generate_atr_anchored_brick_sizes docstring.
    assert generate_atr_anchored_brick_sizes(10.0) == [1.5, 3.0, 6.0]


# ─── select_best_combo_with_atr_anchoring ─────────────────────────────────


def test_off_mode_picks_over_full_grid_no_comparison_fields():
    # off mode: caller is expected to not have added ATR-anchored combos
    # at all, so `grid_results` here IS the original-only set.
    original = _combo(brick_size=1.0, sharpe=1.0)
    result = select_best_combo_with_atr_anchoring(
        [original], original_brick_sizes={1.0}, mode="off", **_FILTERS,
    )
    assert result.chosen is original
    assert result.baseline is None
    assert result.widened is None
    assert result.changed is False


def test_shadow_mode_never_overrides_baseline_even_when_widened_is_better():
    baseline_combo = _combo(brick_size=1.0, sharpe=1.0)          # original
    atr_anchored_combo = _combo(brick_size=0.5, sharpe=9.0)       # ATR-anchored, much better
    result = select_best_combo_with_atr_anchoring(
        [baseline_combo, atr_anchored_combo],
        original_brick_sizes={1.0}, mode="shadow", **_FILTERS,
    )
    assert result.chosen is baseline_combo         # zero live-behavior change
    assert result.baseline is baseline_combo
    assert result.widened is atr_anchored_combo    # logged, not applied
    assert result.changed is True


def test_shadow_mode_unchanged_when_widened_pick_is_the_same_combo():
    only_combo = _combo(brick_size=1.0, sharpe=1.0)
    result = select_best_combo_with_atr_anchoring(
        [only_combo], original_brick_sizes={1.0}, mode="shadow", **_FILTERS,
    )
    assert result.chosen is only_combo
    assert result.baseline is only_combo
    assert result.widened is only_combo
    assert result.changed is False


def test_live_mode_applies_widened_pick():
    baseline_combo = _combo(brick_size=1.0, sharpe=1.0)
    atr_anchored_combo = _combo(brick_size=0.5, sharpe=9.0)
    result = select_best_combo_with_atr_anchoring(
        [baseline_combo, atr_anchored_combo],
        original_brick_sizes={1.0}, mode="live", **_FILTERS,
    )
    assert result.chosen is atr_anchored_combo
    assert result.baseline is baseline_combo
    assert result.widened is atr_anchored_combo
    assert result.changed is True


def test_live_mode_falls_back_to_baseline_when_widened_finds_nothing():
    # Nothing anywhere passes the RR filter -> both baseline and widened
    # picks are None regardless of mode.
    unusable = _combo(brick_size=1.0, sharpe=1.0, sl_bricks=10, tp_bricks=1)
    result = select_best_combo_with_atr_anchoring(
        [unusable], original_brick_sizes={1.0}, mode="live", **_FILTERS,
    )
    assert result.chosen is None
    assert result.baseline is None
    assert result.widened is None
    assert result.changed is False


def test_live_mode_rescues_symbol_when_only_atr_anchored_candidate_passes():
    # Baseline (original-only) grid has nothing that passes the filters;
    # the ATR-anchored candidate does. Live mode should rescue the sweep
    # instead of failing it (matches today's no_combo_passed_filters path
    # for symbols like the ones flagged in
    # nt_heavy_sweep_reachability_shadow).
    baseline_unusable = _combo(brick_size=1.0, sharpe=1.0, sl_bricks=10, tp_bricks=1)
    atr_anchored_ok = _combo(brick_size=0.5, sharpe=2.0, sl_bricks=3, tp_bricks=6)
    result = select_best_combo_with_atr_anchoring(
        [baseline_unusable, atr_anchored_ok],
        original_brick_sizes={1.0}, mode="live", **_FILTERS,
    )
    assert result.chosen is atr_anchored_ok
    assert result.baseline is None
    assert result.widened is atr_anchored_ok
    assert result.changed is True
