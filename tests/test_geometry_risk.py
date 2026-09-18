"""Geometry risk unit tests (worklog 20260918_TIERED_ATR_GEOMETRY_SCOPE.md)."""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.geometry_risk import (
    GEOMETRY_RISK_CAP,
    compute_geometry_metrics,
    distance_over_atr,
    geometry_risk_hat,
    vol_drift_factor,
)
from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload


class TestDistances:
    def test_tp_distance(self):
        assert distance_over_atr(20.0, 10.0) == pytest.approx(2.0)

    def test_missing_atr(self):
        assert distance_over_atr(20.0, None) is None
        assert distance_over_atr(20.0, 0) is None


class TestVolDrift:
    def test_no_data_is_neutral(self):
        assert vol_drift_factor(None, None) == 1.0
        assert vol_drift_factor(10.0, 20.0) == 1.0  # vol contracted — no extra risk

    def test_expansion_adds_risk(self):
        assert vol_drift_factor(24.0, 10.0) > 1.0  # 2.4x vs soft 1.2


class TestRiskScore:
    def test_higher_long_lag_higher_risk(self):
        a = geometry_risk_hat(0.2, None)
        b = geometry_risk_hat(0.6, None)
        assert b > a

    def test_p90_tail_increases_risk(self):
        a = geometry_risk_hat(0.4, None)
        b = geometry_risk_hat(0.4, 120.0)  # p90 = 2.5 × 48h
        assert b > a

    def test_cap_constant(self):
        assert 0 < GEOMETRY_RISK_CAP < 1


class TestComputeMetrics:
    def test_wide_tp_flags_risk_when_expiry_high(self):
        m = compute_geometry_metrics(
            entry_price=2500.0,
            take_profit=2600.0,  # 10× ATR
            stop_loss=2490.0,
            atr=10.0,
            regime_label="med_low_vol_bull",
            pct_expired=0.7,
            p90_hours=120.0,
        )
        assert m.tp_distance_atr == pytest.approx(10.0)
        assert m.bucket == "4+"
        assert m.long_lag_hat == pytest.approx(0.7)
        assert m.long_lag_risk is True

    def test_tight_tp_low_expiry_ok(self):
        m = compute_geometry_metrics(
            entry_price=100.0,
            take_profit=100.8,
            stop_loss=99.5,
            atr=1.0,
            regime_label="high_vol_bull",
            pct_expired=0.1,
            p90_hours=20.0,
        )
        assert m.tp_distance_atr == pytest.approx(0.8)
        assert m.bucket == "0.5-1"
        assert m.long_lag_risk is False

    def test_prior_when_unscored(self):
        m = compute_geometry_metrics(
            entry_price=100.0,
            take_profit=105.0,
            stop_loss=97.0,
            atr=1.0,
            regime_label="high_vol_bull",
        )
        assert m.long_lag_hat is not None
        assert m.reason == "long_lag_prior"


class TestSimPayload:
    def test_geometry_fields_in_payload(self):
        p = _build_signal_sim_payload(
            symbol="XAUUSD", venue="OANDA", ts="2026-09-18T00:00:00Z",
            signal="buy", entry_price=2500.0, stop_loss=2470.0, take_profit=2560.0,
            brick_size=5.0, sl_bricks=6, tp_bricks=12,
            p_win=0.6, ev=0.1, regime="med_low_vol_bull", regime_conf=0.7,
            source="light", qualified=True,
            tp_distance_atr=1.2, sl_distance_atr=0.6,
            long_lag_hat=0.25, geometry_risk_hat=0.28, long_lag_risk=False,
        )
        assert p["tp_distance_atr"] == 1.2
        assert p["geometry_risk_hat"] == 0.28
        assert p["long_lag_risk"] is False
