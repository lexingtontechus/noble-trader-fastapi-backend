"""Tests for the ``p_historical`` EV-blend source (R3, 2026-08-23 —
worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md).

Covers:
  1. ``EVEngine.compute_p_historical`` — the pass-through/clamp contract.
  2. ``EVEngine.compute_ev`` — p_historical participates in the blend and
     renormalizes out cleanly when unavailable (replacing the old
     always-None p_pattern source).

REMOVED 2026-09-09 (worklog/20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md):
this file used to also cover ``calibration_enforcement.fetch_historical_p_win``
(reads ``v_eod_calibration_bias_latest``, applies the Bayesian shrink,
respects the view's own INSUFFICIENT_DATA gate). That function was deleted
outright — it fed ``p_historical`` into this blend as a soft, continuous
correction, independently of (and stacked on top of)
``calibration_enforcement.apply_calibration_adjustment()``, a separate
hard, post-hoc correction applied to the SAME p_win after this blend
already ran, off the SAME underlying calibration-bias evidence. Consolidated
to ``apply_calibration_adjustment`` as the sole calibration-correction
mechanism, so those fetch-behavior tests were testing intentionally-removed
functionality, not surviving behavior — deleted, not adapted.

What remains below (``compute_p_historical``'s pass-through/clamp contract,
and ``compute_ev``'s renormalization when p_historical is absent/present)
is genuine, still-present ``ev_engine.py`` behavior: no production caller
(orchestrator.py, heavy_sweep.py) feeds this source anymore, but the
parameter/weight entry/renormalization contract itself was deliberately
left in place for direct callers that still supply their own value (e.g.
renko/router.py's ad-hoc ``/ev/compute`` endpoint) — see ev_engine.py's
module docstring NOTE.
"""
import pytest

from regime_platform.services.statistics.ev_engine import EVEngine, P_WIN_WEIGHTS_FULL


# ─── 1. EVEngine.compute_p_historical (pass-through contract) ──────────


def test_compute_p_historical_passthrough_and_clamp():
    assert EVEngine.compute_p_historical(None, "long") is None
    assert EVEngine.compute_p_historical(0.6, "long") == 0.6
    assert EVEngine.compute_p_historical(1.5, "long") == 1.0  # clamped
    assert EVEngine.compute_p_historical(-0.2, "long") == 0.0  # clamped


# ─── 2. compute_ev() blend integration ─────────────────────────────────


def test_weights_full_has_historical_not_pattern():
    """R2: p_pattern is gone; R3: p_historical replaces it at the same
    30% base weight. The weight table entry itself survives the 2026-09-09
    removal of the production fetch — only the live feed was retired."""
    assert "p_pattern" not in P_WIN_WEIGHTS_FULL
    assert P_WIN_WEIGHTS_FULL["p_historical"] == 0.30
    assert abs(sum(P_WIN_WEIGHTS_FULL.values()) - 1.0) < 1e-9


def test_p_historical_none_renormalizes_like_old_p_pattern():
    """Omitting p_historical (None — the default, and now what every
    production caller passes since the 2026-09-09 removal) must drop it
    from the blend and renormalize over the remaining 3 sources — the
    exact contract p_pattern always relied on, since the server always
    passed it as None."""
    engine = EVEngine()
    result = engine.compute_ev(
        trend_probs={"bull": 0.6, "bear": 0.2, "strong_bull": 0.1, "strong_bear": 0.1},
        direction="long",
        p_historical=None,
        p_timesfm=0.55,
        tp_bricks=5, sl_bricks=3, brick_size=0.5,
    )
    assert "p_historical" not in result.sources_used
    assert abs(sum(result.weights_used.values()) - 1.0) < 1e-9


def test_p_historical_present_participates_in_blend():
    """A direct caller that still supplies p_historical (e.g.
    renko/router.py's ad-hoc /ev/compute endpoint) gets it blended: a high
    value (symbol has a strong realized track record) pulls p_win up
    relative to the same call without it."""
    engine = EVEngine()
    common = dict(
        trend_probs={"bull": 0.5, "bear": 0.5},
        direction="long",
        p_timesfm=0.5,
        tp_bricks=5, sl_bricks=3, brick_size=0.5,
    )
    without = engine.compute_ev(p_historical=None, **common)
    with_high = engine.compute_ev(p_historical=0.9, **common)
    assert "p_historical" in with_high.sources_used
    assert with_high.p_win > without.p_win


def test_unknown_weight_key_error_mentions_p_historical():
    """set_weights() must reject the old p_pattern key with a message
    pointing at its replacement, not silently accept it."""
    engine = EVEngine()
    with pytest.raises(ValueError, match="p_historical"):
        engine.set_weights({"p_pattern": 0.5})
