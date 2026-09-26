"""Payoff geometry (worklog/kairos/20260926_PAYOFF_GEOMETRY.md)."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from regime_platform.core import feature_flags as ff
from regime_platform.sweep_orchestrator.payoff_geometry import (
    class_geometry,
    resolve_payoff_geometry,
)

TABLE = {
    "forex": {"tp_atr": 24.0, "sl_atr": 1.5},
    "equity": {"tp_atr": 17.0, "sl_atr": 2.0},
    "commodity": {"tp_atr": 6.0, "sl_atr": 1.0},
    "crypto": None,
}


class TestClassGeometry:
    @pytest.mark.parametrize("raw,expected", [
        ("forex", (24.0, 1.5)), ("stocks", (17.0, 2.0)),
        ("commodities", (6.0, 1.0)), ("crypto", None), ("etf", None), (None, None),
    ])
    def test_live_nt_symbol_asset_classes_map(self, raw, expected):
        # nt_symbol.asset_class values seen live: forex / stocks / commodities / crypto
        assert class_geometry(raw, TABLE) == expected

    @pytest.mark.parametrize("entry", [
        {"tp_atr": 1.0, "sl_atr": 2.0},      # TP must exceed SL
        {"tp_atr": 0, "sl_atr": 1.0},
        {"tp_atr": "x", "sl_atr": 1.0},
        {"tp_atr": 3.0},
        "24/1.5",
    ])
    def test_invalid_entries_keep_heavy(self, entry):
        assert class_geometry("forex", {"forex": entry}) is None

    def test_empty_table(self):
        assert class_geometry("forex", {}) is None
        assert class_geometry("forex", None) is None


class TestResolve:
    def test_forex_bricks_and_prices(self):
        g = resolve_payoff_geometry("forex", 0.002, table=TABLE, brick_atr=0.5)
        assert (g.sl_bricks, g.tp_bricks) == (3, 48)
        assert g.brick_size == pytest.approx(0.001)
        assert g.brick_size * g.tp_bricks == pytest.approx(24 * 0.002)
        assert g.brick_size * g.sl_bricks == pytest.approx(1.5 * 0.002)
        assert (g.tp_atr, g.sl_atr) == (24.0, 1.5)

    def test_rounds_to_brick_unit(self):
        g = resolve_payoff_geometry("equity", 3.0, table={"equity": {"tp_atr": 17.2, "sl_atr": 1.9}},
                                    brick_atr=0.5)
        assert (g.sl_bricks, g.tp_bricks) == (4, 34)
        assert (g.sl_atr, g.tp_atr) == (2.0, 17.0)

    def test_min_one_sl_brick_and_tp_above_sl(self):
        g = resolve_payoff_geometry("forex", 1.0, table={"forex": {"tp_atr": 0.3, "sl_atr": 0.1}},
                                    brick_atr=0.5)
        assert g.sl_bricks == 1 and g.tp_bricks >= 2

    @pytest.mark.parametrize("atr", [None, 0.0, -1.0])
    def test_no_atr_keeps_heavy(self, atr):
        assert resolve_payoff_geometry("forex", atr, table=TABLE, brick_atr=0.5) is None

    def test_unlisted_class_keeps_heavy(self):
        assert resolve_payoff_geometry("crypto", 500.0, table=TABLE, brick_atr=0.5) is None

    def test_bad_brick_unit_keeps_heavy(self):
        assert resolve_payoff_geometry("forex", 0.002, table=TABLE, brick_atr=0) is None

    def test_rr_clears_min_rr(self):
        # EV Step 6: rr = tp_bricks/sl_bricks must clear min_rr (1.5) for every configured class
        for ac in ("forex", "equity", "commodity"):
            g = resolve_payoff_geometry(ac, 1.0, table=TABLE, brick_atr=0.5)
            assert g.tp_bricks / g.sl_bricks >= 1.5


class TestFlags:
    def _with(self, monkeypatch, cfg):
        monkeypatch.setattr(ff, "_load", lambda: cfg)

    def test_defaults_are_inert(self, monkeypatch):
        self._with(monkeypatch, {})
        assert ff.get_payoff_geometry_mode() == "shadow"
        assert ff.get_payoff_geometry() == {}          # no table → no override even in live
        assert ff.get_payoff_geometry_brick_atr() == 0.5

    @pytest.mark.parametrize("v,exp", [("LIVE", "live"), ("off", "off"), ("bogus", "shadow")])
    def test_mode(self, monkeypatch, v, exp):
        self._with(monkeypatch, {"payoff_geometry_mode": v})
        assert ff.get_payoff_geometry_mode() == exp

    @pytest.mark.parametrize("v", ["x", 0, -1, None])
    def test_brick_atr_invalid(self, monkeypatch, v):
        self._with(monkeypatch, {"payoff_geometry_brick_atr": v})
        assert ff.get_payoff_geometry_brick_atr() == 0.5

    def test_table_non_dict(self, monkeypatch):
        self._with(monkeypatch, {"payoff_geometry": ["forex"]})
        assert ff.get_payoff_geometry() == {}

    def test_shipped_yaml(self):
        cfg = yaml.safe_load(Path("config/feature_flags.yaml").read_text(encoding="utf-8"))
        assert cfg["payoff_geometry_mode"] in ("off", "shadow", "live")
        t = cfg["payoff_geometry"]
        assert class_geometry("forex", t) == (24.0, 1.5)
        assert class_geometry("stocks", t) == (17.0, 2.0)
        assert class_geometry("commodities", t) == (6.0, 1.0)
        assert class_geometry("crypto", t) is None


class TestOrchestratorWiring:
    """p_win must not move with the bracket: the Markov T^N horizon keeps the
    heavy tp_bricks (one lever — geometry only)."""

    def _src(self):
        from regime_platform.sweep_orchestrator import orchestrator
        return inspect.getsource(orchestrator.SweepOrchestrator._process_symbol)

    def test_markov_horizon_uses_heavy_tp_bricks(self):
        src = self._src()
        assert "resolve_markov_n_steps(tp_bricks)" not in src
        assert src.count("resolve_markov_n_steps(_markov_tp_bricks)") == 2
        # captured before the payoff override can reassign tp_bricks
        assert src.index("_markov_tp_bricks = tp_bricks") < src.index("_pg.brick_size, _pg.sl_bricks, _pg.tp_bricks")

    def test_override_before_trade_signal(self):
        src = self._src()
        assert src.index("resolve_payoff_geometry(") < src.index("trade_signal = _compute_trade_signal(")

    def test_only_live_reassigns(self):
        src = self._src()
        block = src[src.index("_pg_mode = get_payoff_geometry_mode()"):src.index("[payoff:geometry] %s failed")]
        assert 'if _pg_mode == "live":' in block
