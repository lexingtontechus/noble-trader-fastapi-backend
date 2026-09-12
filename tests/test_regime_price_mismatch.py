"""Tests for the regime-vs-realized-price mismatch detector
(regime_platform/services/self_learning/regime_price_mismatch.py),
Phase 2 detector 1, worklog
20260910_AAPL_REGIME_STALENESS_AND_SELF_LEARNING_SCOPE.md SS2 /
self_learning_phase_2_detectors (RM-37).
"""

from regime_platform.services.self_learning.regime_price_mismatch import (
    detect_regime_price_mismatch,
    regime_implied_direction,
)


# ─── regime_implied_direction ────────────────────────────────────────────


def test_bull_family_is_up():
    assert regime_implied_direction("low_vol_bull") == "up"
    assert regime_implied_direction("low_vol_strong_bull") == "up"
    assert regime_implied_direction("med_high_vol_strong_bull") == "up"


def test_bear_family_is_down():
    assert regime_implied_direction("low_vol_bear") == "down"
    assert regime_implied_direction("med_high_vol_strong_bear") == "down"


def test_missing_or_unrecognized_is_none():
    assert regime_implied_direction(None) is None
    assert regime_implied_direction("") is None
    assert regime_implied_direction("neutral") is None
    assert regime_implied_direction("low_vol_neutral") is None


# ─── detect_regime_price_mismatch: the real, FULL AAPL history ───────────
#
# Real data pulled from nt_sweep_result 2026-09-12 (Supabase project
# pcvscowltlrxzgxjurcr), the complete 2026-08-23..2026-09-12 daily
# history for AAPL. This replaced an earlier, shorter 9-day fixture
# (09-01..09-09 only) that passed against a first draft of the detector
# but MISSED the case once run against this longer real history — the
# regime's bull-family run actually extends 08-28..09-10 (14 days, not
# 9), and by its very last day (09-10, $325.95) price had already
# partially recovered to only -0.65% off the $328.07 peak, hiding the
# real -3.85% mismatch that existed one day earlier (09-09, $315.45).
# This is exactly why the detector tracks the worst point WITHIN a run,
# not just its final day — see the module docstring's own account of
# this correction.

_AAPL_FULL_HISTORY = [
    {"regime_label": "low_vol_bull",             "regime_conf": 0.9970, "day_open": 309.63, "day_close": 309.63},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.8881, "day_open": 309.63, "day_close": 310.77},
    {"regime_label": "low_vol_bull",             "regime_conf": 0.8489, "day_open": 310.77, "day_close": 309.44},
    {"regime_label": "low_vol_bull",             "regime_conf": 0.8395, "day_open": 309.44, "day_close": 314.24},
    {"regime_label": "low_vol_strong_bear",      "regime_conf": 0.8548, "day_open": 314.24, "day_close": 314.14},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9997, "day_open": 314.14, "day_close": 319.86},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 1.0000, "day_open": 319.86, "day_close": 319.86},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9990, "day_open": 319.86, "day_close": 319.86},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9994, "day_open": 319.86, "day_close": 316.66},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9655, "day_open": 316.66, "day_close": 325.20},
    {"regime_label": "low_vol_bull",             "regime_conf": 0.9328, "day_open": 325.20, "day_close": 325.32},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9965, "day_open": 325.32, "day_close": 328.07},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9987, "day_open": 328.07, "day_close": 320.79},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9992, "day_open": 320.79, "day_close": 320.79},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9977, "day_open": 320.79, "day_close": 320.79},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9985, "day_open": 320.79, "day_close": 320.79},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9749, "day_open": 320.79, "day_close": 316.13},
    {"regime_label": "low_vol_strong_bull",      "regime_conf": 0.9206, "day_open": 316.13, "day_close": 315.45},
    {"regime_label": "low_vol_bull",             "regime_conf": 0.8400, "day_open": 315.45, "day_close": 325.95},
    {"regime_label": "med_high_vol_strong_bear", "regime_conf": 0.7089, "day_open": 325.95, "day_close": 332.51},
    {"regime_label": "high_vol_strong_bull",     "regime_conf": 0.6782, "day_open": 332.51, "day_close": 332.51},
]


def test_real_aapl_full_history_is_detected():
    result = detect_regime_price_mismatch(_AAPL_FULL_HISTORY)
    assert result is not None
    assert result["regime_direction"] == "up"
    assert result["extreme_price"] == 328.07
    assert result["worst_price_move_pct"] < -0.03  # real value ~ -0.0385
    # The worst day is 09-09 (index 17), not the run's final day (09-10,
    # index 18) -- confirming this is caught mid-run, not at the end.
    assert result["worst_day_index"] == 17


def test_final_day_of_the_real_run_alone_would_have_missed_it():
    # Sanity check on the fixture: the run's actual last day (09-10,
    # index 18) was only -0.65% off the peak -- confirming a
    # peak-vs-final-day design (the corrected function's predecessor)
    # would NOT have flagged this, and the worst-point tracking is what
    # actually catches it.
    last_day = _AAPL_FULL_HISTORY[18]
    final_day_pct = (last_day["day_close"] - 328.07) / 328.07
    assert abs(final_day_pct) < 0.02


def test_real_aapl_case_below_confidence_threshold_not_flagged():
    result = detect_regime_price_mismatch(_AAPL_FULL_HISTORY, min_confidence=0.999)
    assert result is None


def test_real_aapl_case_below_move_threshold_not_flagged():
    result = detect_regime_price_mismatch(_AAPL_FULL_HISTORY, min_price_move_pct=0.10)
    assert result is None


# ─── Synthetic edge cases ─────────────────────────────────────────────────


def test_no_mismatch_when_price_agrees_with_regime():
    rows = [
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 100.0, "day_close": 102.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 102.0, "day_close": 104.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 104.0, "day_close": 106.0},
    ]
    assert detect_regime_price_mismatch(rows) is None


def test_bear_regime_mismatched_by_a_rally():
    rows = [
        {"regime_label": "low_vol_bear", "regime_conf": 0.95, "day_open": 100.0, "day_close": 98.0},
        {"regime_label": "low_vol_bear", "regime_conf": 0.95, "day_open": 98.0, "day_close": 96.0},
        {"regime_label": "low_vol_bear", "regime_conf": 0.95, "day_open": 96.0, "day_close": 105.0},
    ]
    result = detect_regime_price_mismatch(rows)
    assert result is not None
    assert result["regime_direction"] == "down"
    assert result["worst_price_move_pct"] > 0.02


def test_transient_mismatch_that_recovers_before_run_ends_is_still_caught():
    # The exact shape of the real AAPL bug this detector's design was
    # corrected for: a mid-run drawdown that partially recovers by the
    # run's LAST day must still be flagged.
    rows = [
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 100.0, "day_close": 100.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 100.0, "day_close": 110.0},  # peak 110
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 110.0, "day_close": 95.0},   # trough mid-run: -13.6%
        {"regime_label": "low_vol_bull", "regime_conf": 0.95, "day_open": 95.0, "day_close": 108.0},   # recovers to only -1.8% off peak by the end
    ]
    result = detect_regime_price_mismatch(rows, min_price_move_pct=0.05)
    assert result is not None
    assert result["worst_price_move_pct"] < -0.05
    assert result["worst_day_index"] == 2  # the trough day, not the last day


def test_run_shorter_than_minimum_is_not_flagged():
    rows = [
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 100.0, "day_close": 110.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 110.0, "day_close": 90.0},
    ]
    assert detect_regime_price_mismatch(rows, min_consecutive_days=3) is None


def test_neutral_days_break_the_run():
    rows = [
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 100.0, "day_close": 110.0},
        {"regime_label": "low_vol_neutral", "regime_conf": 0.99, "day_open": 110.0, "day_close": 110.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 110.0, "day_close": 90.0},
    ]
    # Neither side of the break reaches min_consecutive_days=3 on its own.
    assert detect_regime_price_mismatch(rows) is None


def test_returns_earliest_qualifying_run_not_a_later_one():
    rows = [
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 100.0, "day_close": 100.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 100.0, "day_close": 100.0},
        {"regime_label": "low_vol_bull", "regime_conf": 0.99, "day_open": 100.0, "day_close": 80.0},
        {"regime_label": "low_vol_bear", "regime_conf": 0.99, "day_open": 80.0, "day_close": 80.0},
        {"regime_label": "low_vol_bear", "regime_conf": 0.99, "day_open": 80.0, "day_close": 80.0},
        {"regime_label": "low_vol_bear", "regime_conf": 0.99, "day_open": 80.0, "day_close": 95.0},
    ]
    result = detect_regime_price_mismatch(rows)
    assert result is not None
    assert result["start_index"] == 0
    assert result["regime_direction"] == "up"


def test_empty_input_returns_none():
    assert detect_regime_price_mismatch([]) is None
