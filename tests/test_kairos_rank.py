"""Ranking and regime-transition helpers (directional-edge worklog §2–§3)."""

from __future__ import annotations

import math

import pytest

from regime_platform.sweep_orchestrator import kairos_rank as R


class TestEventWindow:
    def test_bands(self):
        assert R.event_window(-3) == "pre"
        assert R.event_window(0) == "fresh"
        assert R.event_window(2.9) == "fresh"
        assert R.event_window(3) == "warm"
        assert R.event_window(12) == "stale"
        assert R.event_window(24) == "deep"
        assert R.event_window(100) == "deep"
        assert R.event_window(None) is None


class TestTransitionType:
    def test_types(self):
        order = R.VOL_ORDER if hasattr(R, "VOL_ORDER") else (
            "low_vol", "med_low_vol", "med_high_vol", "high_vol",
        )
        assert R.transition_type("low_vol", "bull", "low_vol", "bear", order) == "dir_flip"
        assert R.transition_type("low_vol", "bull", "high_vol", "bull", order) == "vol_expand"
        assert R.transition_type("high_vol", "bull", "low_vol", "bull", order) == "vol_contract"
        assert R.transition_type("low_vol", "bull", "high_vol", "bear", order) == "both"
        assert R.transition_type("low_vol", "bull", "low_vol", "bull", order) == "none"
        assert R.transition_type(None, "bull", "low_vol", "bear", order) is None


class TestSurprise:
    def test_rare_is_higher(self):
        counts = {("A|bull", "A|bear"): 100, ("A|bull", "B|bear"): 1}
        common = R.surprise_from_counts(counts, "A|bull", "A|bear")
        rare = R.surprise_from_counts(counts, "A|bull", "B|bear")
        assert rare > common


class TestYAccuracy:
    def test_vs_no_skill(self):
        assert R.y_accuracy(True, 1.0, 1.0) == pytest.approx(0.5)
        assert R.y_accuracy(False, 1.0, 1.0) == pytest.approx(-0.5)
        assert R.y_accuracy(True, None, 1.0) is None


class TestShrink:
    def test_shrink_toward_global(self):
        assert R.shrink_cell_mean([], 0.3, k=10) == pytest.approx(0.3)
        s = R.shrink_cell_mean([1.0] * 5, 0.0, k=10)
        assert s == pytest.approx((5 * 1.0 + 10 * 0.0) / 15)


class TestRankIC:
    def test_monotone(self):
        assert R.rank_ic([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert R.rank_ic([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
        assert R.rank_ic([1, 1, 1], [1, 2, 3]) is None


class TestAUC:
    def test_separation(self):
        assert R.auc_within([True, True, False, False], [0.9, 0.8, 0.2, 0.1]) == pytest.approx(1.0)
        assert R.auc_within([True, False], [0.4, 0.6]) == pytest.approx(0.0)
        assert R.auc_within([True, True], [0.5, 0.6]) is None


class TestRecentTP:
    def test_decay_weight(self):
        # last outcomes dominate
        outs = [False, False, False, True, True]
        r = R.recent_tp_rate(outs, half_life_h=1.0)
        assert r is not None and r > 0.5
        assert R.recent_tp_rate([None, None]) is None


class TestClusterAgree:
    def test_agree_share(self):
        flips = [("XAUUSD", "bull", 0.0), ("XAGUSD", "bull", 1.0), ("EURUSD", "bear", 0.0)]
        clusters = {"XAUUSD": "gold", "XAGUSD": "gold", "EURUSD": "fx"}
        out = R.cluster_agree(flips, clusters, window_h=2.0)
        assert out["XAUUSD"] == pytest.approx(1.0)
        assert out["EURUSD"] == pytest.approx(0.0)
