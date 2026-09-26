"""Pure scorecard maths, era splits, and cluster-risk helpers (V3 §15 / triage D2–D8)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from regime_platform.sweep_orchestrator import kairos_quant as Q


def _ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


class TestEras:
    def test_era_bounds(self):
        assert Q.era_of(_ts(2026, 9, 21)) == "pre_debounce"
        assert Q.era_of(_ts(2026, 9, 22)) == "debounce_to_atr"
        assert Q.era_of(_ts(2026, 9, 24)) == "debounce_to_atr"
        assert Q.era_of(_ts(2026, 9, 25)) == "atr_to_resolver"
        assert Q.era_of(_ts(2026, 9, 26)) == "post_resolver"
        assert Q.era_of(_ts(2026, 10, 1)) == "post_resolver"

    def test_split_eras_indices(self):
        ts = [_ts(2026, 9, 20), _ts(2026, 9, 23), _ts(2026, 9, 26)]
        got = Q.split_eras(ts)
        assert got["pre_debounce"] == [0]
        assert got["debounce_to_atr"] == [1]
        assert got["post_resolver"] == [2]
        assert got["atr_to_resolver"] == []


class TestNoSkill:
    def test_coin_flip_bracket(self):
        assert Q.no_skill_line(1.0, 1.0) == pytest.approx(0.5)
        assert Q.no_skill_line(3.0, 1.0) == pytest.approx(0.25)
        assert Q.no_skill_line(1.0, 3.0) == pytest.approx(0.75)

    def test_rejects_nonpositive(self):
        assert Q.no_skill_line(0.0, 1.0) is None
        assert Q.no_skill_line(1.0, -1.0) is None
        assert Q.no_skill_line(None, 1.0) is None


class TestCalibration:
    def test_error_sign_convention(self):
        assert Q.calibration_error(0.60, True) == pytest.approx(-0.40)  # underconfident
        assert Q.calibration_error(0.60, False) == pytest.approx(0.60)  # overconfident
        assert Q.calibration_error(0.26, True) == pytest.approx(-0.74)

    def test_accuracy_and_claimed(self):
        # TP=1, SL=1 → no-skill 0.5
        assert Q.accuracy_vs_no_skill(True, 1, 1) == pytest.approx(0.5)
        assert Q.accuracy_vs_no_skill(False, 1, 1) == pytest.approx(-0.5)
        assert Q.claimed_edge(0.60, 1, 1) == pytest.approx(0.10)


class TestClusters:
    def test_symbol_day_count(self):
        rows = [
            (_ts(2026, 9, 26, 1), "AAPL"),
            (_ts(2026, 9, 26, 2), "AAPL"),  # same symbol-day
            (_ts(2026, 9, 26, 2), "XAUUSD"),
            (None, "XAUUSD"),
        ]
        assert Q.symbol_day_clusters(rows) == 2

    def test_pair_clusters_single_link(self):
        # A-B 0.8, B-C 0.8, A-C 0.1 → one cluster via B
        symbols = ["A", "B", "C"]
        corr = [
            [1.0, 0.8, 0.1],
            [0.8, 1.0, 0.8],
            [0.1, 0.8, 1.0],
        ]
        got = Q.pair_clusters(symbols, corr, threshold=0.7)
        assert got["A"] == got["B"] == got["C"]

    def test_pair_clusters_separate(self):
        symbols = ["A", "B"]
        corr = [[1.0, 0.2], [0.2, 1.0]]
        got = Q.pair_clusters(symbols, corr, threshold=0.7)
        assert got["A"] != got["B"]


class TestClusterRisk:
    def test_allows_under_cap(self):
        r = Q.ClusterRisk(cluster="gold", open_kelly=1.0, cap_mult=1.5)
        assert r.allows(0.4, single_symbol_max=1.0) is True
        assert r.allows(0.6, single_symbol_max=1.0) is False
        assert r.allows(-0.1) is False
