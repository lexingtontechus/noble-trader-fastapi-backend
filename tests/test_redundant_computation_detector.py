"""Tests for the redundant-computation detector
(regime_platform/services/self_learning/redundant_computation_detector.py),
Phase 2 detector 3, self_learning_phase_2_detectors (RM-37).
"""

from regime_platform.services.self_learning.redundant_computation_detector import (
    detect_redundant_computation,
)


def test_high_agreement_above_threshold_is_flagged():
    # 100 compared, 2 disagree -> 98% agreement, above the 95% default.
    result = detect_redundant_computation("pair_a", n_compared=100, n_disagree=2)
    assert result is not None
    assert result["agreement_rate"] == 0.98


def test_low_agreement_is_not_flagged():
    result = detect_redundant_computation("pair_a", n_compared=100, n_disagree=40)
    assert result is None


def test_exactly_at_threshold_is_flagged():
    result = detect_redundant_computation(
        "pair_a", n_compared=100, n_disagree=5, min_redundancy_threshold=0.95,
    )
    assert result is not None
    assert result["agreement_rate"] == 0.95


def test_below_min_sample_size_is_not_flagged_even_at_100pct_agreement():
    result = detect_redundant_computation(
        "pair_a", n_compared=5, n_disagree=0, min_sample_size=20,
    )
    assert result is None


def test_perfect_agreement_large_sample_is_flagged():
    result = detect_redundant_computation("pair_a", n_compared=1000, n_disagree=0)
    assert result is not None
    assert result["agreement_rate"] == 1.0


def test_custom_threshold_respected():
    # 90% agreement fails the default 95% bar but passes an 85% bar.
    assert detect_redundant_computation("pair_a", n_compared=100, n_disagree=10) is None
    result = detect_redundant_computation(
        "pair_a", n_compared=100, n_disagree=10, min_redundancy_threshold=0.85,
    )
    assert result is not None
