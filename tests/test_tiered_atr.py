"""Tiered ATR unit tests (worklog 20260918_TIERED_ATR_GEOMETRY_SCOPE.md)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from regime_platform.core.feature_flags import (
    get_dynamic_atr_mode,
    get_dynamic_atr_risk_cap,
)
from regime_platform.sweep_orchestrator.tiered_atr import (
    asset_class_key,
    combo_tp_distance_atr,
    in_preferred_band,
    preferred_tp_atr_band,
    quality_filter,
    risk_capped_pick,
    vol_family,
)


def _c(brick, sl, tp, sharpe, trades=20):
    return SimpleNamespace(
        brick_size=brick, sl_bricks=sl, tp_bricks=tp,
        sharpe=sharpe, total_trades=trades,
    )


class TestPriors:
    def test_asset_keys(self):
        assert asset_class_key("Crypto") == "crypto"
        assert asset_class_key("Forex") == "forex"
        assert asset_class_key("metals") == "metal"
        assert asset_class_key(None) == "default"

    def test_bands(self):
        assert preferred_tp_atr_band("forex") == (0.3, 1.5)
        assert preferred_tp_atr_band("equity")[1] == 2.0
        assert in_preferred_band(1.0, "forex")
        assert not in_preferred_band(5.0, "forex")

    def test_vol_family(self):
        assert vol_family("high_vol") == "high"
        # med_low_vol contains both tokens; current impl checks low before med
        assert vol_family("med_low_vol") == "low"
        assert vol_family("med_high_vol") == "high"
        assert vol_family(None) == "unknown"


class TestQualityFilter:
    def test_filters_rr_trades_sharpe(self):
        combos = [
            _c(1.0, 3, 6, sharpe=2.0),   # rr=2 ok
            _c(1.0, 5, 5, sharpe=2.0),   # rr=1 fail
            _c(1.0, 3, 6, sharpe=99.0),  # sharpe cap
            _c(1.0, 3, 6, sharpe=1.0, trades=2),  # trades
        ]
        out = quality_filter(combos, min_rr=1.5, max_sharpe=10.0, min_trades=5)
        assert len(out) == 1
        assert out[0].sharpe == 2.0


class TestRiskCappedPick:
    def test_prefers_low_risk_narrow_tp(self):
        # Wide 4+ ATR combo has higher Sharpe; narrow has worse Sharpe but lower risk
        wide = _c(brick=5.0, sl=2, tp=10, sharpe=3.0)   # tp_atr = 10*5/10=5
        narrow = _c(brick=0.5, sl=2, tp=4, sharpe=1.5)  # tp_atr = 4*0.5/10=0.2
        atr = 10.0
        exp = {"4+": 0.9, "0.5-1": 0.05, "1-2": 0.2}
        # bucket for 5.0 is 4+; 0.2 is <0.5
        exp["<0.5"] = 0.05
        p90 = {"4+": 120.0, "<0.5": 20.0}
        r = risk_capped_pick(
            [wide, narrow], atr=atr, asset_class="forex",
            min_rr=1.5, max_sharpe=10.0, min_trades=5,
            risk_cap=0.35,
            pct_expired_by_bucket=exp,
            p90_by_bucket=p90,
            mode="shadow",
        )
        assert r.baseline_key == (5.0, 2, 10)
        assert r.capped_key == (0.5, 2, 4)
        assert r.pick_changed is True
        assert r.nothing_survived is False
        assert r.asset_class == "forex"

    def test_nothing_survived_when_all_over_cap(self):
        wide = _c(5.0, 2, 10, sharpe=3.0)
        r = risk_capped_pick(
            [wide], atr=10.0, asset_class="fx",
            min_rr=1.5, max_sharpe=10.0, min_trades=5,
            risk_cap=0.1,
            pct_expired_by_bucket={"4+": 0.95},
            p90_by_bucket={"4+": 200.0},
        )
        assert r.nothing_survived is True
        assert r.pick_changed is False  # same object

    def test_empty_pool(self):
        r = risk_capped_pick([], atr=10.0)
        assert r.nothing_survived is True
        assert r.baseline_key is None

    def test_combo_tp_distance(self):
        c = _c(2.0, 3, 5, sharpe=1.0)
        assert combo_tp_distance_atr(c, 10.0) == pytest.approx(1.0)


class TestFlags:
    def test_dynamic_atr_defaults_shadow(self):
        assert get_dynamic_atr_mode() in ("off", "shadow", "live")
        assert get_dynamic_atr_mode() == "shadow"
        assert get_dynamic_atr_risk_cap() == pytest.approx(0.35)
