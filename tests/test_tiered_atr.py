"""Dynamic ATR selector (tiered_atr.py) unit tests."""

from __future__ import annotations

import numpy as np
import pytest

from regime_platform.core.feature_flags import (
    get_dynamic_atr_config,
    get_dynamic_atr_mode,
    get_dynamic_atr_risk_cap,
)
from regime_platform.sweep_orchestrator.tiered_atr import (
    DynamicAtrPick,
    asset_class_key,
    build_paths,
    select_tp_sl,
    walk,
)

GRID = dict(tp_grid=[1.5, 3.0, 6.0, 12.0], sl_grid=[1.0, 2.0], min_rr=1.5,
            max_expiry=0.25, cost_atr=0.0, min_signals=10)


def _path(values: list[float]) -> np.ndarray:
    """48-bar path that steps linearly to each value in turn, then holds."""
    out = np.full(48, values[-1], dtype=float)
    out[: len(values)] = values
    return out


class TestWalk:
    def test_tp_first_sl_first_expired(self):
        P = np.vstack([
            _path([1.0, 3.5]),          # TP 3 on bar 1
            _path([-0.5, -2.5]),        # SL 2 on bar 1
            _path([0.5]),               # never touches → expired at +0.5
        ])
        pnl, expired = walk(P, 3.0, 2.0)
        assert pnl.tolist() == pytest.approx([3.5, -2.5, 0.5])   # booked at the triggering close
        assert expired.tolist() == [False, False, True]

    def test_same_bar_tie_is_tp(self):
        P = np.vstack([_path([5.0])])
        pnl, _ = walk(P, 3.0, 0.0001)
        assert pnl[0] == pytest.approx(5.0)


class TestSelect:
    def test_picks_far_tp_when_trends_pay(self):
        # 30% trend +0.5 ATR/bar to +13, 70% stop out just past -1: far TP / close SL wins.
        trend = list(np.arange(0.5, 13.01, 0.5))
        rows = [_path(trend) if i % 10 < 3 else _path([-1.1]) for i in range(100)]
        pick = select_tp_sl(np.vstack(rows), **GRID)
        assert pick is not None
        assert (pick.tp_atr, pick.sl_atr) == (12.0, 1.0)
        assert pick.exp_h1 > 0 and pick.exp_h2 > 0

    def test_none_when_negative(self):
        rows = [_path([-3.0]) for _ in range(50)]
        assert select_tp_sl(np.vstack(rows), **GRID) is None

    def test_none_when_one_half_loses(self):
        trend = list(np.arange(0.5, 13.01, 0.5))
        good = [_path(trend) if i % 10 < 3 else _path([-1.1]) for i in range(50)]
        bad = [_path([-3.0]) for _ in range(50)]
        assert select_tp_sl(np.vstack(good + bad), **GRID) is None

    def test_expiry_cap(self):
        rows = [_path([0.2]) for _ in range(50)]   # nothing ever resolves
        assert select_tp_sl(np.vstack(rows), **GRID) is None

    def test_min_signals(self):
        rows = [_path([13.0]) for _ in range(5)]
        assert select_tp_sl(np.vstack(rows), **GRID) is None

    def test_min_rr(self):
        rows = [_path([1.6]) if i % 2 else _path([-0.5]) for i in range(40)]
        pick = select_tp_sl(np.vstack(rows), **{**GRID, "tp_grid": [1.5], "sl_grid": [1.0, 2.0]})
        assert pick is None or pick.tp_atr / pick.sl_atr >= 1.5


class TestBricks:
    def test_to_bricks_keeps_sl_bricks(self):
        p = DynamicAtrPick(tp_atr=24.0, sl_atr=1.5, exp_atr=1.0, exp_h1=1.0, exp_h2=1.0, expiry=0.2, n=100)
        brick, sl, tp = p.to_bricks(atr=0.002, sl_bricks=3)
        assert sl == 3 and tp == 48
        assert brick * sl == pytest.approx(1.5 * 0.002)
        assert brick * tp == pytest.approx(24.0 * 0.002)


class TestPaths:
    def test_build_paths_direction_and_window(self):
        bars = {"X": [{"t": 3600.0 * i, "c": 100.0 + i} for i in range(60)]}
        sigs = [
            {"symbol": "X", "signal": "buy", "entry_price": 100.0, "atr": 2.0, "_ts": 10.0},
            {"symbol": "X", "signal": "sell", "entry_price": 100.0, "atr": 2.0, "_ts": 20.0},
            {"symbol": "X", "signal": "buy", "entry_price": 100.0, "atr": 2.0, "_ts": 3600.0 * 30},  # window incomplete
        ]
        P = build_paths(sigs, bars)
        assert P.shape == (2, 48)
        assert P[0, 1] == pytest.approx(0.5) and P[1, 1] == pytest.approx(-0.5)


class TestConfig:
    def test_asset_keys(self):
        assert asset_class_key("Crypto") == "crypto"
        assert asset_class_key("Forex") == "forex"
        assert asset_class_key(None) == "default"

    def test_flags(self):
        assert get_dynamic_atr_mode() in ("off", "shadow", "live")
        assert get_dynamic_atr_risk_cap() == pytest.approx(0.35)
        cfg = get_dynamic_atr_config()
        assert cfg["lookback_days"] == 10 and cfg["min_signals"] == 60
        # 2026-09-29 balanced engine: w in [0.40,0.60], exp<=0.20, EV>0 via RR
        assert cfg["max_expiry"] == 0.20
        assert cfg["tp_atr_max"] == 13.0 and cfg["sl_atr_min"] == 0.5


class TestBandClamp:
    def test_clamp_excludes_wide_tp(self):
        """tp=24 would win on exp but is outside the replay band — clamp drops it."""
        # 48-step path that ends at +24 so tp=24 is reachable.
        trend = list(np.arange(0.5, 24.01, 0.5))  # 48 values
        rows = [_path(trend) if i % 10 < 3 else _path([-1.1]) for i in range(100)]
        wide = select_tp_sl(
            np.vstack(rows),
            tp_grid=[12.0, 24.0], sl_grid=[1.0],
            min_rr=1.5, max_expiry=0.25, cost_atr=0.0, min_signals=10,
        )
        assert wide is not None and wide.tp_atr == 24.0
        clamped = select_tp_sl(
            np.vstack(rows),
            tp_grid=[12.0, 24.0], sl_grid=[1.0],
            min_rr=1.5, max_expiry=0.25, cost_atr=0.0, min_signals=10,
            tp_atr_min=9.0, tp_atr_max=17.0, sl_atr_min=0.5, sl_atr_max=2.0,
        )
        assert clamped is not None
        assert clamped.tp_atr == 12.0
        assert 9.0 <= clamped.tp_atr <= 17.0

    def test_clamp_excludes_tight_sl_below_floor(self):
        # Winners hit TP 12; losers run to -2 so both sl=0.25 and sl=1.0 resolve.
        # Without the floor, 0.25 loses less and wins on exp; clamp keeps 1.0.
        rows = [
            _path([12.0]) if i % 2 == 0 else _path([-2.0])
            for i in range(40)
        ]
        unclamped = select_tp_sl(
            np.vstack(rows),
            tp_grid=[12.0], sl_grid=[0.25, 1.0],
            min_rr=1.5, max_expiry=0.25, cost_atr=0.0, min_signals=10,
        )
        assert unclamped is not None and unclamped.sl_atr == 0.25
        pick = select_tp_sl(
            np.vstack(rows),
            tp_grid=[12.0], sl_grid=[0.25, 1.0],
            min_rr=1.5, max_expiry=0.25, cost_atr=0.0, min_signals=10,
            sl_atr_min=0.5, sl_atr_max=2.0,
        )
        assert pick is not None
        assert pick.sl_atr >= 0.5

    def test_no_clamp_keeps_legacy_behaviour(self):
        trend = list(np.arange(0.5, 13.01, 0.5))
        rows = [_path(trend) if i % 10 < 3 else _path([-1.1]) for i in range(100)]
        pick = select_tp_sl(
            np.vstack(rows),
            tp_grid=[1.5, 3.0, 6.0, 12.0], sl_grid=[1.0, 2.0],
            min_rr=1.5, max_expiry=0.25, cost_atr=0.0, min_signals=10,
        )
        assert pick is not None and pick.tp_atr == 12.0
