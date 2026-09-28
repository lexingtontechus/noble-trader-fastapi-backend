"""Event-time half-life helpers (P0 / T2). Scope: 20260927_P0_HALFLIFE_T2_SCOPE.md."""

from __future__ import annotations

import math

import pytest

from regime_platform.sweep_orchestrator import kairos_rank as R


class TestExpDecayFit:
    def test_recovers_half_life(self):
        # y = 0.2 + 0.4 * 2^(-τ/6)  → h=6, b=0.4
        tau = [0.0, 1.0, 2.0, 3.0, 6.0, 9.0, 12.0, 18.0, 24.0, 36.0, 48.0]
        y = [0.2 + 0.4 * 2.0 ** (-t / 6.0) for t in tau]
        fit = R.exp_decay_fit(tau, y)
        assert fit is not None
        assert fit["h"] == pytest.approx(6.0)
        assert fit["b"] == pytest.approx(0.4, abs=1e-3)
        assert fit["a"] == pytest.approx(0.2, abs=1e-3)
        assert R.event_time_halflife(tau, y) == pytest.approx(6.0)

    def test_flat_series_small_b(self):
        tau = [0.0, 3.0, 6.0, 12.0, 24.0, 48.0]
        y = [0.1] * 6
        fit = R.exp_decay_fit(tau, y)
        assert fit is not None
        assert abs(fit["b"]) < 1e-6

    def test_too_few(self):
        assert R.exp_decay_fit([0.0, 1.0], [0.1, 0.2]) is None
        assert R.event_time_halflife([], []) is None


class TestHalflifeNull:
    def test_shuffled_no_strong_p(self):
        # deterministic alternating signal → circular shift should not give tiny p_b
        tau = [float(i) for i in range(20)]
        y = [0.4 if i % 2 == 0 else -0.4 for i in range(20)]
        out = R.halflife_null(tau, y, n_draws=50, seed=1)
        assert out["n"] == 20
        assert out["p_b"] is not None
        assert 0.0 <= out["p_b"] <= 1.0

    def test_true_decay_low_p(self):
        tau = [0.0, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0, 48.0]
        y = [0.3 + 0.5 * 2.0 ** (-t / 6.0) for t in tau]
        out = R.halflife_null(tau, y, n_draws=80, seed=2)
        assert out["b_obs"] == pytest.approx(0.5, abs=0.05)
        assert out["h_obs"] == pytest.approx(6.0, rel=0.25)
        assert out["p_b"] is not None and out["p_b"] < 0.2
