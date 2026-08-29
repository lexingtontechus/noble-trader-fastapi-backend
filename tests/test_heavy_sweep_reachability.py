"""
Tests for Phase 3a of the calibration reframe
(regime_platform/sweep_orchestrator/heavy_sweep_reachability.py),
worklog/20260825_CALIBRATION_REFRAME_SCOPE.md "Phase 3 scope" (2026-08-28).

``reachability_aware_pick`` is a pure function — tested here with
duck-typed combo stubs (only .brick_size/.sl_bricks/.tp_bricks/.sharpe/
.total_trades are read, matching the module's own docstring on why it
doesn't import heavy_sweep.ComboResult). All combos use sl_bricks=3 and
brick_size=1, atr=10 unless noted, so distance = tp_bricks and
ratio = tp_bricks / 10 lands cleanly in ATR_BUCKETS:
  tp=6  -> ratio 0.6 -> "0.5-1"
  tp=15 -> ratio 1.5 -> "1-2"
"""

from types import SimpleNamespace

from regime_platform.sweep_orchestrator.heavy_sweep_reachability import (
    reachability_aware_pick,
)


def _combo(tp_bricks, sharpe, sl_bricks=3, brick_size=1.0, total_trades=20):
    return SimpleNamespace(
        brick_size=brick_size, sl_bricks=sl_bricks, tp_bricks=tp_bricks,
        sharpe=sharpe, total_trades=total_trades,
    )


def test_good_bucket_pick_unchanged():
    combo = _combo(tp_bricks=6, sharpe=2.0)  # bucket "0.5-1"
    result = reachability_aware_pick([combo], atr=10, bias_by_bucket={"0.5-1": 0.5})

    assert result["baseline_bucket"] == "0.5-1"
    assert result["filtered_bucket"] == "0.5-1"
    assert result["filtered_combo"] is combo
    assert result["nothing_survived"] is False
    assert result["pick_changed"] is False


def test_bad_bucket_pick_changes_to_reachable_alternative():
    baseline_combo = _combo(tp_bricks=6, sharpe=3.0)   # bucket "0.5-1", highest sharpe
    better_combo = _combo(tp_bricks=15, sharpe=1.0)    # bucket "1-2", lower sharpe
    bias_by_bucket = {"0.5-1": -0.3, "1-2": 0.4}

    result = reachability_aware_pick(
        [baseline_combo, better_combo], atr=10, bias_by_bucket=bias_by_bucket,
    )

    assert result["baseline_bucket"] == "0.5-1"
    assert result["baseline_sharpe"] == 3.0
    assert result["filtered_combo"] is better_combo
    assert result["filtered_bucket"] == "1-2"
    assert result["nothing_survived"] is False
    assert result["pick_changed"] is True


def test_nothing_survives_when_every_bucket_is_negative():
    combo_a = _combo(tp_bricks=6, sharpe=3.0)   # "0.5-1"
    combo_b = _combo(tp_bricks=15, sharpe=1.0)  # "1-2"
    bias_by_bucket = {"0.5-1": -0.3, "1-2": -0.4}

    result = reachability_aware_pick([combo_a, combo_b], atr=10, bias_by_bucket=bias_by_bucket)

    assert result["baseline_bucket"] == "0.5-1"  # baseline still computed
    assert result["filtered_combo"] is None
    assert result["filtered_bucket"] is None
    assert result["nothing_survived"] is True
    assert result["pick_changed"] is False


def test_unscored_bucket_is_kept_not_excluded():
    combo = _combo(tp_bricks=6, sharpe=2.0)  # "0.5-1", not in bias_by_bucket at all
    result = reachability_aware_pick([combo], atr=10, bias_by_bucket={})

    assert result["filtered_combo"] is combo
    assert result["nothing_survived"] is False


def test_missing_atr_disables_reachability_leg_entirely():
    combo = _combo(tp_bricks=6, sharpe=2.0)
    # Even a hugely negative bucket score must not exclude anything when
    # atr is falsy — bucket becomes unknown for every combo, fail-open.
    result = reachability_aware_pick([combo], atr=0, bias_by_bucket={"0.5-1": -100.0})

    assert result["baseline_bucket"] is None
    assert result["filtered_bucket"] is None
    assert result["filtered_combo"] is combo
    assert result["nothing_survived"] is False


def test_mirrors_pick_best_combos_own_quality_filter():
    # Fails min_trades (10) despite a great sharpe and a great bucket —
    # must never appear as baseline OR filtered.
    low_trades_combo = _combo(tp_bricks=6, sharpe=5.0, total_trades=5)
    valid_combo = _combo(tp_bricks=6, sharpe=1.0, total_trades=20)
    bias_by_bucket = {"0.5-1": -0.5}  # negative -> valid_combo won't be "reachable" either

    result = reachability_aware_pick(
        [low_trades_combo, valid_combo], atr=10, bias_by_bucket=bias_by_bucket,
    )

    assert result["baseline_sharpe"] == 1.0  # NOT 5.0 — low_trades_combo excluded
    assert result["nothing_survived"] is True  # valid_combo's bucket is negative


def test_empty_combo_list_is_a_safe_noop():
    result = reachability_aware_pick([], atr=10, bias_by_bucket={})

    assert result["baseline_bucket"] is None
    assert result["filtered_combo"] is None
    assert result["nothing_survived"] is False
    assert result["pick_changed"] is False
