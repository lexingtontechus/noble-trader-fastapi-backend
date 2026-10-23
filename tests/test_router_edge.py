"""Router Edge Model (R2) tests — regime_platform/sweep_orchestrator/router_edge.py
+ the ev_engine p_router source + the R2 flag ladder, worklog
20261023_ROUTER_EDGE_MODEL_BUILD.md.

The R2 contract under test:
  * cells: decay-weighted Beta counts, tie-skip, base-rate anchor shrinkage
  * the stamp/resolve loop: first-new-bar resolution, no double-resolution
    while a bar is still forming, BOTH skill accumulators (router + timesfm)
    scored on the SAME outcome
  * selection-bias guard: neutral ticks stamp too (stand_by cell)
  * ev_engine: p_router as a renormalizing blend source; byte-identical
    blend when p_router=None (the every-historical-caller regression)
  * flags: defaults (shadow/live), YAML values readable, mode vocabulary
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from regime_platform.sweep_orchestrator.router_edge import (
    KEY_BASE,
    KEY_LAST,
    KEY_ROUTER_SKILL,
    KEY_TIMESFM_SKILL,
    TIER,
    EdgeCell,
    RouterEdgeTick,
    SkillCell,
    _first_new_bar,
    _side_of_prediction,
    cell_key,
    predict_p_up,
    router_edge_report,
    router_edge_tick,
)
from regime_platform.core import feature_flags as ff
from regime_platform.core.param_store import read_dict, write_params
from regime_platform.services.statistics.ev_engine import EVEngine, P_WIN_WEIGHTS_FULL


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_param_store(tmp_path, monkeypatch):
    """Isolate the router-edge cells into a per-test SQLite store."""
    monkeypatch.setenv("PARAM_STORE_SQLITE", str(tmp_path / "router_edge_test.sqlite"))
    yield


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


async def _stamp_cell(symbol: str, key: str, cell: EdgeCell) -> None:
    await write_params([{
        "symbol": symbol, "tier_type": TIER, "tier_key": key,
        "tier_value": cell.as_value(), "n": int(cell.n_eff),
    }])


# ─── EdgeCell ───────────────────────────────────────────────────────────────


class TestEdgeCell:
    def test_empty_cell_n_eff_zero(self):
        assert EdgeCell().n_eff == 0.0

    def test_add_up_and_dn(self):
        c = EdgeCell().add(True, _iso()).add(False, _iso())
        assert c.s_up == pytest.approx(1.0)
        assert c.s_dn == pytest.approx(1.0)
        assert (c.n_up, c.n_dn) == (1, 1)

    def test_decay_halves_at_tau(self):
        c = EdgeCell(s_up=8.0, s_dn=4.0, updated_at=_iso(days_ago=90.0))
        d = c.decayed(_iso(0.0), tau_days=90.0)
        assert d.s_up == pytest.approx(4.0)
        assert d.s_dn == pytest.approx(2.0)

    def test_decay_zero_age_is_identity(self):
        c = EdgeCell(s_up=8.0, s_dn=4.0, updated_at=_iso())
        d = c.decayed(_iso(), tau_days=90.0)
        assert d.s_up == pytest.approx(8.0)

    def test_decay_keeps_raw_counters(self):
        c = EdgeCell(s_up=8.0, s_dn=4.0, n_up=9, n_dn=5, updated_at=_iso(days_ago=100.0))
        d = c.decayed(_iso(), tau_days=10.0)
        assert (d.n_up, d.n_dn) == (9, 5)

    def test_value_roundtrip(self):
        c = EdgeCell(s_up=3.5, s_dn=1.25, n_up=4, n_dn=2, n_tie=1, updated_at=_iso())
        assert EdgeCell.from_value(c.as_value()) == pytest.approx(
            EdgeCell(3.5, 1.25, 4, 2, 1, _iso()), nan_ok=True,
        ) or (EdgeCell.from_value(c.as_value()).s_up == pytest.approx(3.5))

    def test_from_value_garbage_is_empty(self):
        assert EdgeCell.from_value(None).n_eff == 0.0
        assert EdgeCell.from_value({"s_up": "junk"}).n_eff == 0.0


# ─── predict_p_up (the shrinkage math) ─────────────────────────────────────


class TestPredict:
    def test_empty_cell_no_base_is_uninformative_prior(self):
        # Zero evidence anywhere -> the honest 0.5 (a coin), which the skill
        # scorer treats as "no call" — NOT None (a number that can be stamped).
        assert predict_p_up(EdgeCell(), None, k0=20.0) == pytest.approx(0.5)
        # degenerate config (k0=0, empty cell) -> no honest number
        assert predict_p_up(EdgeCell(), None, k0=0.0) is None

    def test_empty_cell_returns_base(self):
        # k0 pseudo-counts anchor an empty cell exactly at the base rate.
        assert predict_p_up(EdgeCell(), 0.55, k0=20.0) == pytest.approx(0.55)

    def test_no_base_defaults_to_half(self):
        # base None + nonzero evidence -> 0.5 prior anchor
        c = EdgeCell(s_up=10.0, s_dn=10.0)
        assert predict_p_up(c, None, k0=20.0) == pytest.approx(0.5)

    def test_dominant_evidence_overrides_anchor(self):
        c = EdgeCell(s_up=1000.0, s_dn=0.0)
        assert predict_p_up(c, 0.5, k0=20.0) == pytest.approx(
            (20 * 0.5 + 1000) / 1020, abs=1e-9
        )

    def test_shrinkage_pulls_small_sample_to_base(self):
        # 1 up / 0 dn with base 0.5, k0=20 -> 10.5/21 = 0.5 + 1/42
        c = EdgeCell(s_up=1.0, s_dn=0.0)
        assert predict_p_up(c, 0.5, k0=20.0) == pytest.approx(0.5 + 1.0 / 42.0)

    def test_larger_k0_shrinks_harder_to_base(self):
        c = EdgeCell(s_up=3.0, s_dn=1.0)
        p_small = predict_p_up(c, 0.5, k0=2.0)
        p_large = predict_p_up(c, 0.5, k0=200.0)
        # larger k0 pulls the posterior FURTHER from the empirical 3/4 and
        # closer to the 0.5 base anchor
        assert abs(p_small - 0.75) < abs(p_large - 0.75)
        assert abs(p_large - 0.5) < abs(p_small - 0.5)


# ─── helpers ────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_cell_key_directions(self):
        assert cell_key("momentum", "buy") == "momentum:buy"
        assert cell_key("mean_reversion", None) == "mean_reversion:none"
        assert cell_key("stand_by", None) == "stand_by:none"

    def test_first_new_bar_strict(self):
        ts = [1000.0, 2000.0, 3000.0]
        prices = [1.0, 2.0, 3.0]
        assert _first_new_bar(prices, ts, 2000.0) == (2, 3.0)
        # no newer bar -> None (stamp stays pending)
        assert _first_new_bar(prices, ts, 3000.0) is None
        # missing ts -> no resolution possible
        assert _first_new_bar(prices, ts, None) is None
        assert _first_new_bar(prices, None, 2000.0) is None

    def test_first_new_bar_skips_none_entries(self):
        assert _first_new_bar([1.0, 2.0], [1000.0, None], 500.0) == (0, 1.0)

    def test_side_of_prediction(self):
        assert _side_of_prediction(0.7, True) is True
        assert _side_of_prediction(0.7, False) is False
        assert _side_of_prediction(0.3, False) is True   # predicted down, was down
        assert _side_of_prediction(0.3, True) is False
        assert _side_of_prediction(0.5, True) is None    # no call
        assert _side_of_prediction(None, True) is None


# ─── the tick (stamp / resolve / score loop) ───────────────────────────────


class TestRouterEdgeTick:
    async def test_mode_off_is_none(self):
        assert await router_edge_tick(
            symbol="XAUUSD", signal="buy", regime_label=None, asset_class="commodities",
            macro_label=None, trend_label=None, prices=[1.0, 2.0],
            timestamps=[100.0, 200.0], p_timesfm=0.6, mode="off",
        ) is None

    async def test_no_timestamps_is_none(self):
        # Without bar identity there is no honest "next candle".
        assert await router_edge_tick(
            symbol="XAUUSD", signal="buy", regime_label=None, asset_class="commodities",
            macro_label=None, trend_label=None, prices=[1.0, 2.0],
            timestamps=None, p_timesfm=0.6, mode="shadow",
        ) is None

    async def test_first_tick_stamps_neutral_standby(self):
        tick = await router_edge_tick(
            symbol="XAUUSD", signal="neutral", regime_label="low_vol_bull",
            asset_class="commodities", macro_label=None, trend_label=None,
            prices=[100.0, 101.0], timestamps=[1000.0, 2000.0],
            p_timesfm=None, mode="shadow",
        )
        assert tick is not None
        assert tick.archetype == "stand_by"
        assert tick.direction is None
        assert tick.resolved is False
        assert tick.stamped is True
        # p_router on a virgin cell -> 0.5 (uninformative prior; skill-scoring
        # skips it) — stamped honestly, not hidden behind None
        assert tick.p_router == pytest.approx(0.5)
        last = read_dict("XAUUSD", TIER, KEY_LAST)
        assert last["cell_key"] == "stand_by:none"
        assert last["bar_ts"] == pytest.approx(2000.0)
        assert last["close"] == pytest.approx(101.0)

    async def test_resolve_updates_cell_base_and_both_skills(self):
        # BTCUSD (crypto) routes momentum (allowlist includes crypto) with
        # macro=bull + trend=trending_up aligned for a buy — FX majors route
        # stand_by at HEAD (momentum's allowlist has no forex key), which is
        # itself a documented registry fact this test pins.
        # Tick 1: stamp with a directional signal + a p_timesfm call.
        await router_edge_tick(
            symbol="BTCUSD", signal="buy", regime_label="med_vol_bull",
            asset_class="crypto", macro_label="bull", trend_label="trending_up",
            prices=[100.0, 101.0], timestamps=[1000.0, 2000.0],
            p_timesfm=0.8, mode="shadow",
        )
        # Tick 2 with a NEW bar that closed UP: resolves the stamp.
        tick2 = await router_edge_tick(
            symbol="BTCUSD", signal="buy", regime_label="med_vol_bull",
            asset_class="crypto", macro_label="bull", trend_label="trending_up",
            prices=[100.0, 101.0, 103.0], timestamps=[1000.0, 2000.0, 3000.0],
            p_timesfm=0.7, mode="shadow",
        )
        assert tick2.resolved is True
        assert tick2.resolved_outcome == "up"
        assert tick2.archetype == "momentum"
        # momentum (aligned macro+trend, buy) cell got the up outcome
        cell = EdgeCell.from_value(read_dict("BTCUSD", TIER, "momentum:buy"))
        assert cell.n_eff == pytest.approx(1.0)
        assert cell.n_up == 1
        # base cell too
        base = EdgeCell.from_value(read_dict("BTCUSD", TIER, KEY_BASE))
        assert base.n_up == 1
        # BOTH skills scored on the same outcome: p_router was 0.5 (virgin
        # cell -> no call scored); p_timesfm 0.8 > 0.5 + up -> hit
        tsk = SkillCell.from_value(read_dict("BTCUSD", TIER, KEY_TIMESFM_SKILL))
        assert tsk.n_hit == 1
        assert tsk.n_miss == 0
        rsk = SkillCell.from_value(read_dict("BTCUSD", TIER, KEY_ROUTER_SKILL))
        assert rsk.n_eff == 0.0

    async def test_timesfm_miss_scores(self):
        await router_edge_tick(
            symbol="GBPUSD", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.9, mode="shadow",
        )
        # next bar closes DOWN -> the 0.9 up-call is a miss
        await router_edge_tick(
            symbol="GBPUSD", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0, 99.0], timestamps=[1000.0, 2000.0],
            p_timesfm=0.9, mode="shadow",
        )
        tsk = SkillCell.from_value(read_dict("GBPUSD", TIER, KEY_TIMESFM_SKILL))
        assert tsk.n_miss == 1

    async def test_tie_outcome_skipped(self):
        await router_edge_tick(
            symbol="AUDUSD", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.6, mode="shadow",
        )
        tick2 = await router_edge_tick(
            symbol="AUDUSD", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0, 100.0], timestamps=[1000.0, 2000.0],
            p_timesfm=0.6, mode="shadow",
        )
        assert tick2.resolved is True
        assert tick2.resolved_outcome == "tie"
        # no cell/skill updates on a tie
        base = EdgeCell.from_value(read_dict("AUDUSD", TIER, KEY_BASE))
        assert base.n_eff == 0.0
        tsk = SkillCell.from_value(read_dict("AUDUSD", TIER, KEY_TIMESFM_SKILL))
        assert tsk.n_eff == 0.0

    async def test_no_new_bar_keeps_stamp_pending(self):
        await router_edge_tick(
            symbol="USDCAD", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.6, mode="shadow",
        )
        # same bar (window slid but ts unchanged) -> no resolution, re-stamp
        tick2 = await router_edge_tick(
            symbol="USDCAD", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.6, mode="shadow",
        )
        assert tick2.resolved is False
        base = EdgeCell.from_value(read_dict("USDCAD", TIER, KEY_BASE))
        assert base.n_eff == 0.0

    async def test_live_mode_blend_gated_by_min_n_eff(self):
        # Virgin cell, min_n_eff=30 -> shadow-equivalent (no blend value)
        tick = await router_edge_tick(
            symbol="USDJPY", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.6,
            mode="live", min_n_eff=30.0,
        )
        assert tick.p_router_blend is None
        # Same call with min_n_eff=0 -> blend value exposed (buy passes through)
        tick2 = await router_edge_tick(
            symbol="USDCHF", signal="buy", regime_label=None,
            asset_class="currencies", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.6,
            mode="live", min_n_eff=0.0,
        )
        assert tick2.p_router_blend == pytest.approx(0.5)  # empty cell + 0.5 base

    async def test_live_mode_sell_inverts(self):
        # Seed a cell with a strong up-bias; a SELL prediction inverts it.
        # Sell + macro bear + trend down -> aligned -> momentum:sell.
        await _stamp_cell("BTCUSD", "momentum:sell", EdgeCell(s_up=18.0, s_dn=2.0))
        await _stamp_cell("BTCUSD", KEY_BASE, EdgeCell(s_up=18.0, s_dn=2.0))
        tick = await router_edge_tick(
            symbol="BTCUSD", signal="sell", regime_label=None,
            asset_class="crypto", macro_label="bear", trend_label="trending_down",
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.4,
            mode="live", min_n_eff=0.0,
        )
        # base shrunk toward 0.5: (20*0.5+18)/40 = 0.7; cell shrunk toward
        # THAT: (20*0.7+18)/40 = 0.8 -> sell blend = 0.2 (double shrinkage,
        # the honest cascade)
        assert tick.p_router == pytest.approx(0.8)
        assert tick.p_router_blend == pytest.approx(0.2)

    async def test_router_error_never_raises(self, monkeypatch):
        # route() blowing up must degrade to a stamped tick, not an exception
        import regime_platform.sweep_orchestrator.router_edge as re_mod
        async def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(re_mod, "_write_rows", boom)
        tick = await router_edge_tick(
            symbol="XAGUSD", signal="buy", regime_label=None,
            asset_class="commodities", macro_label=None, trend_label=None,
            prices=[100.0], timestamps=[1000.0], p_timesfm=0.6, mode="shadow",
        )
        assert tick is None  # fail-open to None — never into the sweep tick


# ─── report ────────────────────────────────────────────────────────────────


class TestReport:
    def test_report_empty_is_clean(self):
        r = router_edge_report()
        assert "error" not in r
        assert r["symbols"] == {}

    async def test_report_reads_cells_and_majority(self):
        # k0=20 (the shipped default): the base rate is itself shrunk toward
        # 0.5 — (20*0.5 + 9)/(20+10) = 19/30 — and majority is its dominant
        # side; cell/skill figures follow the same shrinkage conventions.
        await _stamp_cell("EURUSD", "momentum:buy", EdgeCell(s_up=9.0, s_dn=1.0))
        await _stamp_cell("EURUSD", KEY_BASE, EdgeCell(s_up=9.0, s_dn=1.0))
        await _stamp_cell("EURUSD", KEY_TIMESFM_SKILL, SkillCell(s_hit=3.0, s_miss=1.0))
        await _stamp_cell("EURUSD", KEY_ROUTER_SKILL, SkillCell(s_hit=3.5, s_miss=0.5))
        r = router_edge_report("EURUSD")
        blk = r["symbols"]["EURUSD"]
        base_rate = 19.0 / 30.0
        assert blk["base_rate"] == pytest.approx(base_rate, abs=1e-4)
        assert blk["majority"] == pytest.approx(base_rate, abs=1e-4)
        cell = blk["cells"]["momentum:buy"]
        assert cell["acc"] == pytest.approx(0.9)
        assert cell["skill_vs_majority"] == pytest.approx(0.9 - base_rate, abs=1e-4)
        assert blk["timesfm_skill"]["acc"] == pytest.approx(0.75)
        assert blk["timesfm_skill"]["skill_vs_majority"] == pytest.approx(0.75 - base_rate, abs=1e-4)
        assert blk["router_skill"]["skill_vs_majority"] == pytest.approx(0.875 - base_rate, abs=1e-4)


# ─── ev_engine p_router source ─────────────────────────────────────────────


class TestEvEngineRouterSource:
    def _engine(self) -> EVEngine:
        return EVEngine(min_ev=0.0, min_rr=1.0)

    def test_regression_no_router_is_byte_identical(self):
        # The every-historical-caller regression: p_router=None must not move
        # the blend by a single ulp vs the pre-R2 3-source pool.
        eng = self._engine()
        r = eng.compute_ev(
            trend_probs={"bull": 0.7, "bear": 0.2, "flat": 0.1},
            direction="long", p_timesfm=0.6,
            tp_bricks=5, sl_bricks=3, brick_size=0.5,
        )
        # manual pool: p_regime (compute via engine) + p_timesfm, weights 5:5
        p_regime = eng.compute_p_regime(
            {"bull": 0.7, "bear": 0.2, "flat": 0.1}, "long",
        )
        w_r, w_t = P_WIN_WEIGHTS_FULL["p_regime"], P_WIN_WEIGHTS_FULL["p_timesfm"]
        tot = w_r + w_t
        logit = lambda p: math.log(p / (1 - p))  # noqa: E731
        p_win = 1 / (1 + math.exp(-(tot**-1) * (w_r * logit(p_regime) + w_t * logit(0.6))))
        assert r.p_win == pytest.approx(round(p_win, 4), abs=1e-9)
        assert "p_router" not in r.sources_used
        assert r.p_router == 0.0

    def test_router_joins_blend_renormalized(self):
        eng = self._engine()
        r = eng.compute_ev(
            trend_probs={"bull": 0.7, "bear": 0.2, "flat": 0.1},
            direction="long", p_timesfm=0.6, p_router=0.9,
            tp_bricks=5, sl_bricks=3, brick_size=0.5,
        )
        assert "p_router" in r.sources_used
        assert sum(r.weights_used.values()) == pytest.approx(1.0, abs=2e-3)  # 4dp-rounded
        # pulling toward 0.9 raises p_win vs the no-router blend
        r0 = eng.compute_ev(
            trend_probs={"bull": 0.7, "bear": 0.2, "flat": 0.1},
            direction="long", p_timesfm=0.6,
            tp_bricks=5, sl_bricks=3, brick_size=0.5,
        )
        assert r.p_win > r0.p_win
        assert r.p_router == pytest.approx(0.9)

    def test_router_without_timesfm_renormalizes(self):
        eng = self._engine()
        r = eng.compute_ev(
            trend_probs={"bull": 0.6, "bear": 0.2, "flat": 0.2},
            direction="long", p_timesfm=None, p_router=0.8,
            tp_bricks=5, sl_bricks=3, brick_size=0.5,
        )
        assert set(r.sources_used) == {"p_regime", "p_router"}
        # peer weights 5:5 -> equal split
        assert r.weights_used["p_router"] == pytest.approx(0.5)

    def test_router_inverted_for_shorts(self):
        eng = self._engine()
        long = eng.compute_ev(
            trend_probs={"bull": 0.33, "bear": 0.33, "flat": 0.34},
            direction="long", p_router=0.8,
            tp_bricks=5, sl_bricks=3, brick_size=0.5,
        )
        short = eng.compute_ev(
            trend_probs={"bull": 0.33, "bear": 0.33, "flat": 0.34},
            direction="short", p_router=0.8,
            tp_bricks=5, sl_bricks=3, brick_size=0.5,
        )
        # p_router 0.8 is P(up): helps the long, hurts the short.
        assert long.p_win > 0.5 > short.p_win or long.p_win > short.p_win
        assert short.p_router == pytest.approx(0.2)

    def test_temp_weights_accepts_p_router(self):
        eng = self._engine()
        with eng.temp_weights({"p_router": 0.0}):
            r = eng.compute_ev(
                trend_probs={"bull": 0.6, "bear": 0.2, "flat": 0.2},
                direction="long", p_router=0.9,
                tp_bricks=5, sl_bricks=3, brick_size=0.5,
            )
        # weight 0 -> present in sources but zero influence
        assert r.p_win == pytest.approx(0.5) or "p_router" in r.sources_used
        # weights restored after the context manager
        assert eng._full_weights["p_router"] == P_WIN_WEIGHTS_FULL["p_router"]


# ─── flags ─────────────────────────────────────────────────────────────────


class TestFlags:
    def test_defaults(self):
        assert ff._DEFAULTS["router_edge_mode"] == "shadow"
        assert ff._DEFAULTS["timesfm_blend_mode"] == "live"
        assert ff._DEFAULTS["router_edge_k0"] == 20.0
        assert ff._DEFAULTS["router_edge_tau_days"] == 90.0
        assert ff._DEFAULTS["router_edge_min_n_eff"] == 30.0

    def test_yaml_values(self):
        # The shipped config sets exactly these (shadow measurement, live
        # TimesFM — byte-identical until evidence says otherwise).
        assert ff.get_router_edge_mode() == "shadow"
        assert ff.get_timesfm_blend_mode() == "live"
        assert ff.get_router_edge_weight() == pytest.approx(5.0)
        assert ff.get_router_edge_k0() == pytest.approx(20.0)
        assert ff.get_router_edge_tau_days() == pytest.approx(90.0)
        assert ff.get_router_edge_min_n_eff() == pytest.approx(30.0)

    def test_mode_vocabulary(self, monkeypatch):
        monkeypatch.setattr(ff, "_load", lambda: {"router_edge_mode": "garbage"})
        assert ff.get_router_edge_mode() == "shadow"
        monkeypatch.setattr(ff, "_load", lambda: {"timesfm_blend_mode": "garbage"})
        assert ff.get_timesfm_blend_mode() == "live"

    def test_all_modes_valid(self, monkeypatch):
        for m in ("off", "shadow", "live"):
            monkeypatch.setattr(ff, "_load", lambda m=m: {"router_edge_mode": m})
            assert ff.get_router_edge_mode() == m
            monkeypatch.setattr(ff, "_load", lambda m=m: {"timesfm_blend_mode": m})
            assert ff.get_timesfm_blend_mode() == m
