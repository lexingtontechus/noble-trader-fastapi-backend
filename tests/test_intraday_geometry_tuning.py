"""T1–T4 intraday geometry tuning tests."""

from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import (
    get_geometry_intraday_scale_mode,
    get_geometry_tuning_sweep_mode,
)
from regime_platform.sweep_orchestrator.geometry_risk import brick_vol_scale


class TestBrickVolScale:
    def test_unknown_atr_is_one(self):
        assert brick_vol_scale(None, 1.0) == 1.0
        assert brick_vol_scale(2.0, None) == 1.0

    def test_deadband_no_scale(self):
        assert brick_vol_scale(10.0, 10.0) == 1.0
        assert brick_vol_scale(10.5, 10.0) == 1.0  # ratio 1.05 in deadband

    def test_vol_expand_scales_up(self):
        s = brick_vol_scale(15.0, 10.0)  # 1.5
        assert s == pytest.approx(1.5)

    def test_clipped(self):
        assert brick_vol_scale(50.0, 10.0) == pytest.approx(1.5)
        assert brick_vol_scale(2.0, 10.0) == pytest.approx(0.5)


class TestFlags:
    def test_defaults_shadow(self):
        assert get_geometry_intraday_scale_mode() == "shadow"
        assert get_geometry_tuning_sweep_mode() == "shadow"
