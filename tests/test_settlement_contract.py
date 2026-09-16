"""
Settlement contract — Batch A unit tests (Phase 1).

Worklog: 20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md
Covers: close-walk parity with legacy resolver, OHLC touch, same-bar
policy, expired MTM, costs, feature-flag mode plumbing.
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator import settlement_contract as sc
from regime_platform.sweep_orchestrator.eod_resolver import (
    EXPIRY_BARS,
    compute_pnl,
    resolve_signal_outcome,
    _resolve_signal_outcome_legacy,
    _compute_pnl_legacy,
)


class TestNormalizeBars:
    def test_closes_only(self):
        closes, highs, lows = sc.normalize_bars([1.0, 2.0, 3.0])
        assert closes == [1.0, 2.0, 3.0]
        assert highs is None and lows is None

    def test_ohlc_dicts(self):
        bars = [
            {"close": 1.0, "high": 1.2, "low": 0.9},
            {"close": 2.0, "high": 2.5, "low": 1.8},
        ]
        closes, highs, lows = sc.normalize_bars(bars)
        assert closes == [1.0, 2.0]
        assert highs == [1.2, 2.5]
        assert lows == [0.9, 1.8]

    def test_empty(self):
        closes, highs, lows = sc.normalize_bars([])
        assert closes == [] and highs is None and lows is None


class TestCloseWalkParityWithLegacy:
    """tp_first close-only path must match the historical resolver."""

    def test_buy_tp_hit(self):
        r = sc.settle_from_closes(
            1.1000, 1.0900, 1.1100, [1.1090, 1.1120], direction="buy",
            same_bar_policy=sc.SAME_BAR_TP_FIRST,
        )
        assert r.outcome == "tp_hit"
        assert r.exit_idx == 1
        assert r.exit_price == pytest.approx(1.1120)

    def test_buy_sl_hit(self):
        r = sc.settle_from_closes(
            1.1000, 1.0900, 1.1100, [1.0990, 1.0880], direction="buy",
            same_bar_policy=sc.SAME_BAR_TP_FIRST,
        )
        assert r.outcome == "sl_hit"

    def test_buy_same_bar_tp_first(self):
        # Close-only buy: a single close cannot be both >= TP and <= SL
        # when brackets are correctly ordered — same-bar both is an OHLC
        # phenomenon (high and low both pierce).
        r = sc.settle_from_closes(
            1.1000, 1.0900, 1.1100, [1.1200], direction="buy",
            same_bar_policy=sc.SAME_BAR_TP_FIRST,
        )
        assert r.outcome == "tp_hit"
        assert r.exit_price == pytest.approx(1.1200)

    def test_ohlc_same_bar_policy_flips_outcome(self):
        bars = [{"close": 1.100, "high": 1.120, "low": 1.080}]
        tp_first = sc.settle(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            bars=bars, direction="buy", same_bar_policy=sc.SAME_BAR_TP_FIRST,
        )
        sl_first = sc.settle(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            bars=bars, direction="buy", same_bar_policy=sc.SAME_BAR_SL_FIRST,
        )
        assert tp_first.outcome == "tp_hit"
        assert sl_first.outcome == "sl_hit"
        assert tp_first.bar_mode == "ohlc"

    def test_expired_mtm_exit(self):
        r = sc.settle_from_closes(
            1.1000, 1.0900, 1.1100, [1.1010, 1.1020, 1.1030], direction="buy",
        )
        assert r.outcome == "expired"
        assert r.exit_price == pytest.approx(1.1030)
        assert r.exit_idx is None

    def test_expired_honors_window(self):
        r = sc.settle_from_closes(
            1.1000, 1.0900, 1.1100, [1.1005, 1.1010, 1.1120],
            direction="buy", expiry_bars=2,
        )
        assert r.outcome == "expired"
        assert r.hold_bars == 2

    def test_sell_tp(self):
        r = sc.settle_from_closes(
            1.2000, 1.2100, 1.1900, [1.1980, 1.1880], direction="sell",
        )
        assert r.outcome == "tp_hit"

    def test_default_expiry_48(self):
        assert sc.EXPIRY_BARS == 48
        assert EXPIRY_BARS == 48


class TestOhlcTouch:
    def test_intraday_tp_touch_not_visible_on_close(self):
        # Close stays inside brackets but high pierces TP
        bars = [
            {"close": 1.100, "high": 1.105, "low": 1.098},
            {"close": 1.102, "high": 1.112, "low": 1.100},  # high >= TP 1.110
        ]
        r = sc.settle(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            bars=bars, direction="buy", same_bar_policy=sc.SAME_BAR_TP_FIRST,
        )
        assert r.outcome == "tp_hit"
        assert r.bar_mode == "ohlc"
        assert r.exit_price == pytest.approx(1.1100)  # level fill

    def test_intraday_sl_touch(self):
        bars = [
            {"close": 1.100, "high": 1.102, "low": 1.099},
            {"close": 1.095, "high": 1.100, "low": 1.088},  # low <= SL 1.090
        ]
        r = sc.settle(
            entry_price=1.1000, stop_loss=1.0900, take_profit=1.1100,
            bars=bars, direction="buy", same_bar_policy=sc.SAME_BAR_TP_FIRST,
        )
        assert r.outcome == "sl_hit"
        assert r.exit_price == pytest.approx(1.0900)

    def test_close_only_would_expire_on_same_series(self):
        closes = [1.100, 1.102]  # neither crosses 1.110 / 1.090 on close
        r = sc.settle_from_closes(
            1.1000, 1.0900, 1.1100, closes, direction="buy",
        )
        assert r.outcome == "expired"
        assert r.bar_mode == "close"


class TestPnl:
    def test_tp_sl_bricks(self):
        r = sc.settle_from_closes(
            100.0, 97.0, 106.0, [107.0], direction="buy",
            tp_bricks=6, sl_bricks=3, brick_size=1.0,
        )
        assert r.pnl_bricks == 6.0
        assert r.pnl_dollars == 6.0

    def test_sl_bricks(self):
        r = sc.settle_from_closes(
            100.0, 97.0, 106.0, [96.0], direction="buy",
            tp_bricks=6, sl_bricks=3, brick_size=1.0,
        )
        assert r.pnl_bricks == -3.0

    def test_expired_mtm_bricks(self):
        r = sc.settle_from_closes(
            100.0, 97.0, 106.0, [101.0, 103.0], direction="buy",
            tp_bricks=6, sl_bricks=3, brick_size=1.0,
        )
        assert r.outcome == "expired"
        assert r.pnl_bricks == pytest.approx(3.0)

    def test_costs_deducted_on_terminal(self):
        costs = sc.SettlementCosts(spread_bps=10.0)  # 10 bps of entry
        r = sc.settle(
            entry_price=10000.0, stop_loss=9900.0, take_profit=10200.0,
            bars=[10300.0], direction="buy",
            tp_bricks=2, sl_bricks=1, brick_size=100.0,
            costs=costs,
        )
        assert r.outcome == "tp_hit"
        assert r.pnl_dollars == 200.0
        assert r.costs_dollars == pytest.approx(10.0)
        assert r.pnl_dollars_net == pytest.approx(190.0)

    def test_zero_costs_default_parity(self):
        r = sc.settle_from_closes(
            100.0, 97.0, 106.0, [107.0], direction="buy",
            tp_bricks=6, sl_bricks=3, brick_size=1.0,
        )
        assert r.pnl_dollars_net == r.pnl_dollars
        assert r.costs_dollars == 0.0


class TestResolverDelegation:
    def test_live_resolver_matches_legacy_tp_first(self):
        closes = [1.0990, 1.1120, 1.0880]
        a = resolve_signal_outcome(1.1000, 1.0900, 1.1100, closes, direction="buy")
        b = _resolve_signal_outcome_legacy(1.1000, 1.0900, 1.1100, closes, direction="buy")
        assert a["outcome"] == b["outcome"] == "tp_hit"
        assert a["exit_idx"] == b["exit_idx"]
        assert a["exit_price"] == pytest.approx(b["exit_price"])
        assert a["hold_bars"] == b["hold_bars"]
        assert a["mfe"] == pytest.approx(b["mfe"])
        assert a["drift_1h_pct"] == pytest.approx(b["drift_1h_pct"])

    def test_live_pnl_matches_legacy(self):
        a = compute_pnl("tp_hit", 6, 3, 1.0)
        b = _compute_pnl_legacy("tp_hit", 6, 3, 1.0)
        assert a == b
        a = compute_pnl(
            "expired", 6, 3, 1.0,
            entry_price=100.0, exit_price=103.0, direction="buy",
        )
        b = _compute_pnl_legacy(
            "expired", 6, 3, 1.0,
            entry_price=100.0, exit_price=103.0, direction="buy",
        )
        assert a == b

    def test_resolver_exposes_mode_fields(self):
        r = resolve_signal_outcome(100.0, 95.0, 110.0, [111.0], direction="buy")
        assert "settlement_mode" in r
        assert r["settlement_same_bar_policy"] in (
            sc.SAME_BAR_TP_FIRST, sc.SAME_BAR_SL_FIRST,
        )


class TestFeatureFlags:
    def test_defaults_present(self):
        from regime_platform.core import feature_flags as ff
        assert ff.get_settlement_contract_mode() in ("off", "shadow", "live")
        assert ff.get_settlement_same_bar_policy() in (
            sc.SAME_BAR_TP_FIRST, sc.SAME_BAR_SL_FIRST,
        )
