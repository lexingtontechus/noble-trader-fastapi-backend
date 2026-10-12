"""E2E: geometry harmony — ATR pick is the single source of truth (2026-10-12,
worklog 20261012_GEOMETRY_HARMONY.md).

The replay proved 40/40 signals shipped brick geometry while reopt optimized a
different ATR bracket that never reached the signal — two TP/SL, two buckets,
two reachability reads, diverging every tick. The fix: the ATR pick is the one
geometry; brick is the documented fail-open fallback. The gates (consistency,
walk-forward, clamp bands) must REJECT artifact brackets (the 12:1 RR the
solver produced on intrabar envelopes) so a bad pick fails open to brick rather
than shipping an untradeable bracket.

These tests pin the contract using the REAL functions:
  - the clamp bands reject the 12:1 artifact,
  - a sane pick survives,
  - the consistency gate rejects a pick contradicting its measured bucket,
  - the modes default to live (harmony, not shadow),
  - the bridge applies ATR geometry when a pick exists (to_price_levels).
"""
from __future__ import annotations

import numpy as np
import pytest

from regime_platform.sweep_orchestrator.tiered_atr import (
    DynamicAtrPick,
    check_pick_reachability_consistency,
)
from regime_platform.core.feature_flags import (
    get_dynamic_atr_light_mode,
    get_dynamic_atr_consistency_mode,
    get_dynamic_atr_walkforward_mode,
)


# ── modes default to live (harmony, not shadow) ─────────────────────────────

def test_geometry_modes_default_live():
    """All three gates + the bridge default live — the ATR pick is the single
    source of truth, not a logged-and-discarded shadow."""
    assert get_dynamic_atr_light_mode() == "live"
    assert get_dynamic_atr_consistency_mode() == "live"
    assert get_dynamic_atr_walkforward_mode() == "live"


# ── the bridge applies ATR geometry (one TP/SL source) ──────────────────────

def test_to_price_levels_is_the_shipped_geometry():
    """The ATR pick's to_price_levels IS what the signal ships — so bucket,
    reachability, and EV all read the SAME geometry the trade uses. No more
    40/40 bucket-flip divergence."""
    pick = DynamicAtrPick(tp_atr=2.5, sl_atr=1.5, exp_atr=0.1, exp_h1=0.1,
                          exp_h2=0.1, expiry=0.1, n=100)
    entry, atr = 4200.0, 4.0
    # buy: TP above, SL below, distances = atr multiples
    tp, sl = pick.to_price_levels(entry, atr, "buy")
    assert tp == pytest.approx(entry + 2.5 * atr)   # +10.0
    assert sl == pytest.approx(entry - 1.5 * atr)   # -6.0
    # the bucket this geometry implies (tp_distance/atr = 2.5)
    from regime_platform.sweep_orchestrator.bracket_reachability import _bucket_for
    assert _bucket_for(abs(tp - entry) / atr) == "2-4"


def test_brick_is_failopen_fallback_not_parallel_system():
    """When no pick exists, brick geometry is the fallback — documented, not a
    silent second system. read_pick_from_store returns None → caller keeps
    brick, logged as fallback."""
    # absent store -> None -> brick fallback (the fail-open contract)
    from regime_platform.sweep_orchestrator.tiered_atr import read_pick_from_store
    # a symbol with no persisted pick returns None (fail-open)
    assert read_pick_from_store("__no_such_symbol__", max_age_hours=1.0) is None


# ── the consistency gate rejects artifacts ──────────────────────────────────

def test_consistency_gate_rejects_pick_contradicting_measurement():
    """A pick whose walked TP rate (0.47) contradicts its measured bucket
    (0.24) by > tol (0.20) is REJECTED — this is the XAGUSD case the log
    flagged every cycle while doing nothing. Now it blocks."""
    ok, reason = check_pick_reachability_consistency(
        walk_tp_rate=0.47, measured_pct_hit_tp=0.24, measured_n=54,
        tolerance=0.20,
    )
    assert ok is False, "divergence 0.23 > 0.20 must reject"
    assert "contradicts" in reason or "diff" in reason.lower() or "Δ" in reason


def test_consistency_gate_passes_agreeing_pick():
    """A pick whose walked TP agrees with measurement passes."""
    ok, reason = check_pick_reachability_consistency(
        walk_tp_rate=0.26, measured_pct_hit_tp=0.24, measured_n=54,
        tolerance=0.20,
    )
    assert ok is True


def test_consistency_gate_fails_open_on_cold_bucket():
    """No measured bucket (cold) → fail-open (pass) — never block on missing
    evidence."""
    ok, _ = check_pick_reachability_consistency(
        walk_tp_rate=0.47, measured_pct_hit_tp=None, measured_n=0,
        tolerance=0.20,
    )
    assert ok is True


# ── the clamp bands reject the 12:1 artifact ────────────────────────────────

def test_clamp_bands_reject_artifact_bracket():
    """select_tp_sl's clamp bands (tp_atr_max) reject the 12:1 RR artifact
    (tp=9.5) the solver produced on intrabar envelopes. A pick outside the
    band returns None → fail-open to brick, never ships the artifact."""
    from regime_platform.sweep_orchestrator.tiered_atr import select_tp_sl
    # synthetic envelopes: enough paths, and a wide-TP cell would win EV
    rng = np.random.default_rng(0)
    n = 100
    fav = rng.uniform(0, 12, size=(n, 48)).cumsum(axis=1) * 0.1
    adv = -rng.uniform(0, 3, size=(n, 48)).cumsum(axis=1) * 0.1
    env = np.stack([fav, adv], axis=2)
    # with tp_atr_max=6, the 9.5 artifact is outside the band → rejected
    pick = select_tp_sl(
        envelopes=env,
        tp_grid=[0.5, 1.0, 2.0, 4.0, 6.0, 9.5],
        sl_grid=[0.5, 0.75, 1.0, 1.5],
        min_rr=1.0, max_expiry=0.6, cost_atr=0.05, min_signals=20,
        tp_atr_max=6.0,   # 9.5 excluded
    )
    if pick is not None:
        assert pick.tp_atr <= 6.0, "clamp band must exclude the 9.5 artifact"
        assert pick.tp_atr / pick.sl_atr < 12.0, "no 12:1 artifact ships"


def test_select_tp_sl_fails_open_below_min_signals():
    """Below min_signals → None → brick fallback (the 13/14 no-pick case)."""
    from regime_platform.sweep_orchestrator.tiered_atr import select_tp_sl
    env = np.zeros((5, 48, 2))  # only 5 paths
    pick = select_tp_sl(
        envelopes=env, tp_grid=[2.0], sl_grid=[1.0],
        min_rr=1.0, max_expiry=0.6, cost_atr=0.05, min_signals=80,
    )
    assert pick is None, "insufficient paths must fail open to brick"
