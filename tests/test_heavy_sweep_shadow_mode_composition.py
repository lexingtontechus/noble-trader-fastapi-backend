"""Regression test for the ATR-anchor / deadline-aware shadow-leak bug.

Found 2026-09-14 (worklog 20260914_DEADLINE_AWARE_ATR_ANCHOR_SHADOW_LEAK.md):
confirmed live in production that XAUUSD's heavy sweep picked an
ATR-anchored (widened) combo despite ``heavy_sweep_atr_anchored_bricks_mode
= "shadow"``, because ``select_best_combo_deadline_aware`` re-picked from
the FULL ``grid_results`` (which already includes ATR-anchored candidates
whenever that mode is not "off") instead of the ATR-anchoring stage's own
restricted candidate pool. The fix in ``heavy_sweep.py``'s call site
restricts the pool fed into deadline-aware scoring to
``original_brick_sizes`` unless ``atr_anchored_mode == "live"``.

These tests exercise the two selection helpers directly plus the exact
composition logic added at the call site (mirrored here as a small
helper, since the real composition lives inline in
``run_heavy_sweep_for_symbol`` and isn't itself an importable function).
"""
from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.heavy_sweep import (
    ComboResult,
    select_best_combo_deadline_aware,
    select_best_combo_with_atr_anchoring,
)


def _combo(brick_size, sl, tp, sharpe, deadline_sharpe=None, total_trades=50, win_rate=0.5):
    return ComboResult(
        brick_size=brick_size,
        sl_bricks=sl,
        tp_bricks=tp,
        sharpe=sharpe,
        total_trades=total_trades,
        win_rate=win_rate,
        deadline_aware_sharpe=deadline_sharpe if deadline_sharpe is not None else sharpe,
    )


def _deadline_candidates(grid_results, original_brick_sizes, atr_anchored_mode):
    """Mirrors the exact restriction added at the heavy_sweep.py call site."""
    if atr_anchored_mode == "live":
        return grid_results
    return [c for c in grid_results if c.brick_size in original_brick_sizes]


# ─── The bug, reproduced ───────────────────────────────────────────────


def test_shadow_leak_reproduced_without_the_fix():
    """Sanity check: feeding the FULL grid straight into deadline-aware
    scoring (the pre-fix behavior) lets a shadow-only ATR-anchored combo
    win — this is the exact bug that shipped to XAUUSD in production."""
    original_brick_sizes = {2.0}
    baseline = _combo(2.0, 3, 5, sharpe=1.2, deadline_sharpe=1.2)  # narrow grid winner
    widened = _combo(0.8, 3, 5, sharpe=1.7, deadline_sharpe=1.7)  # ATR-anchored, wins on sharpe
    grid_results = [baseline, widened]

    atr_selection = select_best_combo_with_atr_anchoring(
        grid_results, original_brick_sizes, mode="shadow",
    )
    assert atr_selection.chosen.brick_size == 2.0  # shadow correctly restricts

    # Pre-fix: deadline-aware stage re-picked from the full grid, ignoring
    # the ATR-anchoring stage's restriction entirely.
    buggy_selection = select_best_combo_deadline_aware(grid_results, mode="live")
    assert buggy_selection.chosen.brick_size == 0.8  # the leak: shadow combo won anyway


def test_fix_keeps_atr_anchored_combo_out_of_production_in_shadow_mode():
    """With the fix, when atr_anchored_mode="shadow", deadline-aware
    scoring only ever sees original_brick_sizes candidates — the
    widened/shadow combo cannot become `best` no matter how much better
    its (deadline-aware or raw) sharpe is."""
    original_brick_sizes = {2.0}
    baseline = _combo(2.0, 3, 5, sharpe=1.2, deadline_sharpe=1.2)
    widened = _combo(0.8, 3, 5, sharpe=1.7, deadline_sharpe=1.7)
    grid_results = [baseline, widened]

    candidates = _deadline_candidates(grid_results, original_brick_sizes, atr_anchored_mode="shadow")
    assert candidates == [baseline]

    selection = select_best_combo_deadline_aware(candidates, mode="live")
    assert selection.chosen.brick_size == 2.0


def test_fix_allows_atr_anchored_combo_when_atr_anchored_mode_is_live():
    """Once ATR-anchoring itself is promoted to live, deadline-aware
    scoring should see the full grid again — the restriction is only for
    shadow isolation, not a permanent narrowing."""
    original_brick_sizes = {2.0}
    baseline = _combo(2.0, 3, 5, sharpe=1.2, deadline_sharpe=1.2)
    widened = _combo(0.8, 3, 5, sharpe=1.7, deadline_sharpe=1.7)
    grid_results = [baseline, widened]

    candidates = _deadline_candidates(grid_results, original_brick_sizes, atr_anchored_mode="live")
    assert candidates == grid_results

    selection = select_best_combo_deadline_aware(candidates, mode="live")
    assert selection.chosen.brick_size == 0.8


def test_fix_is_noop_when_atr_anchored_mode_is_off():
    """atr_anchored_mode="off" never generates widened candidates in the
    first place (heavy_sweep.py's all_brick_sizes == brick_sizes), so
    original_brick_sizes == every brick_size present — restriction is a
    no-op, matching pre-Phase-A behavior exactly."""
    original_brick_sizes = {2.0, 0.8}  # both are "original" when mode=off
    baseline = _combo(2.0, 3, 5, sharpe=1.2, deadline_sharpe=1.2)
    other = _combo(0.8, 3, 5, sharpe=1.7, deadline_sharpe=1.7)
    grid_results = [baseline, other]

    candidates = _deadline_candidates(grid_results, original_brick_sizes, atr_anchored_mode="off")
    assert candidates == grid_results

    selection = select_best_combo_deadline_aware(candidates, mode="live")
    assert selection.chosen.brick_size == 0.8  # highest sharpe wins, as always


def test_both_modes_off_is_byte_identical_to_pre_phase_a():
    """With both flags off, deadline-aware scoring's own docstring claims
    byte-identical behavior to pre-fix — confirm the composition doesn't
    disturb that when deadline_mode="off" too (chosen = raw c.sharpe pick,
    no filtering at all)."""
    original_brick_sizes = {2.0, 0.8}
    baseline = _combo(2.0, 3, 5, sharpe=1.2, deadline_sharpe=1.2)
    other = _combo(0.8, 3, 5, sharpe=1.7, deadline_sharpe=1.7)
    grid_results = [baseline, other]

    candidates = _deadline_candidates(grid_results, original_brick_sizes, atr_anchored_mode="off")
    selection = select_best_combo_deadline_aware(candidates, mode="off")
    assert selection.chosen.brick_size == 0.8
    assert selection.uncapped is None and selection.corrected is None  # off mode: no shadow comparison


def test_atr_anchoring_stage_shadow_mode_unaffected_by_this_fix():
    """This fix only changes the deadline-aware stage's input pool — the
    ATR-anchoring stage's own shadow/live semantics (tested implicitly
    above) are untouched: shadow always restricts `chosen` to
    original_brick_sizes regardless of what deadline-aware scoring later
    does with that restricted set."""
    original_brick_sizes = {2.0}
    baseline = _combo(2.0, 3, 5, sharpe=1.2)
    widened = _combo(0.8, 3, 5, sharpe=1.7)
    grid_results = [baseline, widened]

    selection = select_best_combo_with_atr_anchoring(grid_results, original_brick_sizes, mode="shadow")
    assert selection.chosen.brick_size == 2.0
    assert selection.widened.brick_size == 0.8  # still computed for comparison/logging
    assert selection.changed is True
