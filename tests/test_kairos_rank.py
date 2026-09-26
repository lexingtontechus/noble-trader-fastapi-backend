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


class TestRankScore:
    def test_fresh_dir_flip_beats_deep_vol(self):
        good = R.rank_score(
            event_window="fresh", transition="dir_flip",
            recent_tp_rate=0.4, cell_p=0.4,
        )
        bad = R.rank_score(
            event_window="deep", transition="vol_expand",
            recent_tp_rate=0.2, cell_p=0.2,
        )
        assert good > bad
        assert 0.0 <= bad <= 1.0
        assert 0.0 <= good <= 1.0

    def test_shadow_gate_and_delta_d(self):
        assert R.shadow_entry_gate(0.6, 0.55) is True
        assert R.shadow_entry_gate(0.5, 0.55) is False
        dd = R.delta_d([0.1, 0.1], [-0.1, -0.1])
        assert dd == pytest.approx(0.2)
        assert R.delta_d([], [1.0]) is None


class TestRankScoreTracker:
    def test_note_regime_event_time(self):
        t = R.RankScoreTracker()
        typ, tau = t.note_regime("AAPL", "low_vol|bull", 0.0)
        assert tau == 0.0
        typ, tau = t.note_regime("AAPL", "low_vol|bull", 3600.0)
        assert typ is None
        assert tau == pytest.approx(1.0)
        typ, tau = t.note_regime("AAPL", "low_vol|bear", 7200.0)
        assert typ == "dir_flip"
        assert tau == 0.0

    def test_score_uses_trailing_outcomes_only(self):
        t = R.RankScoreTracker()
        t.note_regime("AAPL", "low_vol|bull", 0.0)
        # no outcomes yet → None
        assert t.score("AAPL", "low_vol|bull", now_ts=100.0) is None
        t.observe_outcome("AAPL", "low_vol|bull", True, 50.0)
        t.observe_outcome("AAPL", "low_vol|bull", True, 80.0)
        s = t.score("AAPL", "low_vol|bull", now_ts=100.0)
        assert s is not None and 0.0 < s < 1.0

    def test_roundtrip(self):
        t = R.RankScoreTracker()
        t.note_regime("XAUUSD", "high_vol|bear", 10.0)
        t.observe_outcome("XAUUSD", "high_vol|bear", False, 20.0)
        d = t.to_dict()
        t2 = R.RankScoreTracker.from_dict(d)
        assert t2._last_cell["XAUUSD"] == "high_vol|bear"
        assert t2._recent["XAUUSD"][0][1] is False

    def test_ingest_decided_rows(self):
        t = R.RankScoreTracker()
        n = R.ingest_decided_rows(t, [
            {"symbol": "AAPL", "regime": "low_vol_bull", "outcome": "tp_hit", "ts": "2026-09-26T12:00:00Z"},
            {"symbol": "AAPL", "regime": "low_vol_bull", "outcome": "expired", "ts": "2026-09-26T13:00:00Z"},
        ])
        assert n == 1
        assert len(t._recent["AAPL"]) == 1
