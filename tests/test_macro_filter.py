"""
Tests for the macro filter (regime_platform/sweep_orchestrator/macro_filter.py),
worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md "Macro filter — full
build scope" (2026-08-26/28).

``compute_macro_regime``/``kelly_macro_multiplier`` are pure functions (no
I/O) — tested here against hand-computed expected values using
config/feature_flags.yaml's shipped defaults (sma_window_days=200,
ema_half_life_days=10, bear_bull_threshold=0.05, strength_saturation=0.15,
boost_scale=0.3, penalty_scale=0.5). The Supabase-backed
refresh/get_macro_regime functions are read-only diagnostics verified via
a standalone real-data run instead (same precedent as
bracket_reachability.py/calibration_bias_bucket.py, neither of which has a
mocked-Supabase pytest suite).
"""

from pytest import approx as pytest_approx

from regime_platform.sweep_orchestrator.macro_filter import (
    compute_macro_regime,
    kelly_macro_multiplier,
)

_WINDOW = 200  # config/feature_flags.yaml: macro.sma_window_days
_HALF_LIFE = 10  # macro.ema_half_life_days
_ALPHA = 1.0 - 0.5 ** (1.0 / _HALF_LIFE)


def _closes(last_close: float, window: int = _WINDOW) -> list[float]:
    """`window` daily closes, all 100.0 except the final bar."""
    return [100.0] * (window - 1) + [last_close]


# ─── compute_macro_regime ────────────────────────────────────────────────


def test_compute_macro_regime_insufficient_history_returns_none():
    assert compute_macro_regime([100.0] * (_WINDOW - 1)) is None


def test_compute_macro_regime_flat_price_is_neutral_zero_strength():
    result = compute_macro_regime(_closes(100.0))
    assert result["macro_score"] == pytest_approx(0.0)
    assert result["macro_score_smoothed"] == pytest_approx(0.0)
    assert result["macro_label"] == "neutral"
    assert result["macro_strength"] == pytest_approx(0.0)
    assert result["sma_window_days"] == _WINDOW


def test_compute_macro_regime_bootstrap_score_matches_hand_computed_sma():
    closes = _closes(110.0)
    sma = ((_WINDOW - 1) * 100.0 + 110.0) / _WINDOW
    expected_score = (110.0 - sma) / sma

    result = compute_macro_regime(closes, prev_score_smoothed=None)

    assert result["macro_score"] == pytest_approx(expected_score, abs=1e-6)
    # No previous value -> smoothed bootstraps to the raw score itself.
    assert result["macro_score_smoothed"] == pytest_approx(expected_score, abs=1e-6)
    assert result["macro_label"] == "bull"  # ~0.0995 > 0.05 threshold


def test_compute_macro_regime_ema_continuity_uses_prev_smoothed():
    closes = _closes(110.0)
    sma = ((_WINDOW - 1) * 100.0 + 110.0) / _WINDOW
    raw_score = (110.0 - sma) / sma
    prev_smoothed = 0.02
    expected_smoothed = prev_smoothed + _ALPHA * (raw_score - prev_smoothed)

    result = compute_macro_regime(closes, prev_score_smoothed=prev_smoothed)

    assert result["macro_score"] == pytest_approx(raw_score, abs=1e-6)
    assert result["macro_score_smoothed"] == pytest_approx(expected_smoothed, abs=1e-6)


def test_compute_macro_regime_bear_label_below_negative_threshold():
    closes = _closes(90.0)
    result = compute_macro_regime(closes)
    assert result["macro_score_smoothed"] < -0.05
    assert result["macro_label"] == "bear"


def test_compute_macro_regime_strength_saturates_at_one():
    # A large enough drop that |score| far exceeds the 0.15 saturation.
    closes = _closes(50.0)
    result = compute_macro_regime(closes)
    assert result["macro_strength"] == pytest_approx(1.0)


def test_compute_macro_regime_strength_scales_linearly_below_saturation():
    closes = _closes(105.0)
    sma = ((_WINDOW - 1) * 100.0 + 105.0) / _WINDOW
    raw_score = (105.0 - sma) / sma
    result = compute_macro_regime(closes, prev_score_smoothed=None)
    expected_strength = min(1.0, abs(raw_score) / 0.15)
    assert result["macro_strength"] == pytest_approx(expected_strength, abs=1e-4)


# ─── kelly_macro_multiplier ──────────────────────────────────────────────


def test_kelly_macro_multiplier_neutral_label_is_noop():
    assert kelly_macro_multiplier("neutral", 0.9, "buy") == pytest_approx(1.0)


def test_kelly_macro_multiplier_missing_label_is_noop():
    assert kelly_macro_multiplier(None, None, "buy") == pytest_approx(1.0)


def test_kelly_macro_multiplier_aligned_buy_boosts():
    # direction=buy -> implied "bull"; label "bull" agrees -> boost.
    mult = kelly_macro_multiplier("bull", 1.0, "buy")
    assert mult == pytest_approx(1.0 + 1.0 * 0.3)  # boost_scale default 0.3


def test_kelly_macro_multiplier_aligned_sell_boosts():
    # direction=sell -> implied "bear"; label "bear" agrees -> boost.
    mult = kelly_macro_multiplier("bear", 0.5, "sell")
    assert mult == pytest_approx(1.0 + 0.5 * 0.3)


def test_kelly_macro_multiplier_opposed_buy_penalizes():
    # direction=buy -> implied "bull"; label "bear" disagrees -> penalty.
    mult = kelly_macro_multiplier("bear", 1.0, "buy")
    assert mult == pytest_approx(1.0 - 1.0 * 0.5)  # penalty_scale default 0.5


def test_kelly_macro_multiplier_opposed_sell_penalizes():
    mult = kelly_macro_multiplier("bull", 0.4, "sell")
    assert mult == pytest_approx(1.0 - 0.4 * 0.5)


def test_kelly_macro_multiplier_clamps_strength_input_to_unit_range():
    # An out-of-range strength (shouldn't happen, but defends the formula)
    # clamps to 1.0 before the boost/penalty scale is applied — same
    # result as strength=1.0, not a runaway multiplier.
    assert kelly_macro_multiplier("bull", 5.0, "buy") == pytest_approx(
        kelly_macro_multiplier("bull", 1.0, "buy")
    )


def test_kelly_macro_multiplier_never_exceeds_hard_clamp_band(monkeypatch):
    # This mechanism alone can never zero out or double a position —
    # verify the [0.25, 1.5] clamp actually engages, using an
    # artificially large boost/penalty scale (real config defaults
    # 0.3/0.5 never reach the clamp on their own).
    import regime_platform.sweep_orchestrator.macro_filter as macro_filter_mod

    monkeypatch.setattr(macro_filter_mod, "get_macro_boost_scale", lambda: 5.0)
    monkeypatch.setattr(macro_filter_mod, "get_macro_penalty_scale", lambda: 5.0)

    assert kelly_macro_multiplier("bull", 1.0, "buy") == pytest_approx(1.5)
    assert kelly_macro_multiplier("bear", 1.0, "buy") == pytest_approx(0.25)
