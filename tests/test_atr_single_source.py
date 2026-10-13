"""ATR geometry single source of truth — worklog 20261009_ATR_GEOMETRY_SINGLE_SOURCE.md.

Closes orchestrator-chain gaps 1+2:
  * the ``dynamic_atr`` tier carries USABLE geometry (not a bucket label);
  * the light path reads the SAME pick the heavy path chose;
  * the optimizer refreshes the store by CALLING the one picker.

These are wiring tests: they drive the real public entry points and assert
the observable contract, not the internals.
"""
from __future__ import annotations

import asyncio

import pytest

from regime_platform.core import param_store
from regime_platform.sweep_orchestrator import tiered_atr as ta


def _pick(**kw):
    base = dict(tp_atr=1.71, sl_atr=0.35, exp_atr=0.61, exp_h1=0.2,
                exp_h2=0.9, expiry=0.04, n=232)
    base.update(kw)
    return ta.DynamicAtrPick(**base)


def _run(coro):
    return asyncio.run(coro)


# ── serialisation: every field survives ──────────────────────────────────

def test_pick_json_roundtrip():
    p = _pick()
    r = ta.DynamicAtrPick.from_json(p.to_json())
    assert r == p


def test_from_json_rejects_malformed():
    assert ta.DynamicAtrPick.from_json(None) is None
    assert ta.DynamicAtrPick.from_json("not a dict") is None
    assert ta.DynamicAtrPick.from_json({"sl_atr": 0.3}) is None  # no tp_atr


# ── the store carries the usable geometry ────────────────────────────────

def test_write_then_read_pick_from_store():
    p = _pick(tp_atr=2.13, sl_atr=0.91, n=232)
    assert _run(ta.write_pick_to_store("XAGUSD", p)) is True

    got = ta.read_pick_from_store("XAGUSD")
    assert got is not None
    assert got.tp_atr == pytest.approx(2.13)
    assert got.sl_atr == pytest.approx(0.91)
    assert got.n == 232
    # And it is a JSON OBJECT in the store, not a bare bucket label.
    raw = param_store.read_dict("XAGUSD", param_store.TIER_DYNAMIC_ATR, "latest")
    assert isinstance(raw, dict) and "tp_atr" in raw


def test_read_pick_none_when_cold():
    assert ta.read_pick_from_store("NOPE_NOT_STORED") is None


def test_read_pick_honours_max_age():
    """A stale stamp must not be served as fresh."""
    p = _pick()
    _run(ta.write_pick_to_store("STALESYM", p))
    # Backdate the computed_at stamp.
    _run(param_store.write_params([{
        "symbol": "STALESYM", "tier_type": param_store.TIER_DYNAMIC_ATR,
        "tier_key": "latest", "tier_value": p.to_json(), "n": p.n,
        "computed_at": "2020-01-01T00:00:00+00:00", "source": "test",
    }]))
    assert ta.read_pick_from_store("STALESYM", max_age_hours=6.0) is None
    assert ta.read_pick_from_store("STALESYM") is not None  # no bound → served


# ── the optimizer refreshes by CALLING the one picker ────────────────────

def test_optimizer_refreshes_store_by_calling_picker(monkeypatch):
    """The optimizer must no longer compute its own ATR — it must call the
    picker, and the store must end up with usable geometry."""
    from regime_platform.sweep_orchestrator import dynamic_tier_optimizer as opt

    calls = []

    async def _fake_pick(asset_class, *, cfg, min_rr, symbol=None, use_store=True):
        calls.append((symbol, use_store))
        assert use_store is False, "refresh must force a real recompute"
        pick = _pick(tp_atr=1.5, sl_atr=0.8, n=99)
        # The real picker persists its own result; mirror that here so the
        # assertion below tests the real contract (refresh → store has it).
        await ta.write_pick_to_store(symbol, pick, source="test")
        return pick

    monkeypatch.setattr(ta, "dynamic_atr_pick", _fake_pick)

    peers = [{"symbol": "EURUSD", "asset_class": "forex", "tradingview_symbol": "OANDA:EURUSD"}]
    picks = _run(opt._refresh_dynamic_atr(peers))
    assert "EURUSD" in picks
    assert calls == [("EURUSD", False)]
    # Usable geometry landed in the store (the picker's own write).
    stored = ta.read_pick_from_store("EURUSD")
    assert stored is not None and stored.tp_atr == pytest.approx(1.5)


def test_optimizer_has_no_parallel_atr_computation():
    """The old bucket/expectancy diagnostic is retired."""
    from regime_platform.sweep_orchestrator import dynamic_tier_optimizer as opt
    assert not hasattr(opt, "_compute_dynamic_atr")
    assert hasattr(opt, "_refresh_dynamic_atr")


# ── the picker returns a stored pick without walking ─────────────────────

def test_picker_serves_from_store_without_walk(monkeypatch):
    """use_store=True + a fresh stored pick that is IN BAND ⇒ no TradingView fetch.

    The band gate matters here (tiered_atr._pick_in_band): the store is a cache of
    PAST solves, so a pick fresh by AGE can still carry a bracket from a previous
    band. Only an in-band pick short-circuits; an out-of-band one is a miss and
    recomputes (see tests/test_geometry_always_returns.py). The fixture therefore
    stores a grid cell, not an arbitrary value.
    """
    _run(ta.write_pick_to_store("GBPUSD", _pick(tp_atr=1.6, sl_atr=1.5, n=204)))

    import regime_platform.sweep_orchestrator.price_fetcher as pf

    def _boom(*a, **k):
        raise AssertionError("must not fetch prices when the store has a fresh pick")

    monkeypatch.setattr(pf, "fetch_tradingview_batch", _boom)
    ta._PICK_CACHE.clear()

    got = _run(ta.dynamic_atr_pick("forex", cfg={"lookback_days": 10, "tp_atr": [1.6],
                                                  "sl_atr": [1.5], "min_signals": 60,
                                                  "max_expiry": 0.2, "cost_atr": 0.1},
                                   min_rr=1.5, symbol="GBPUSD", use_store=True))
    assert got is not None and got.tp_atr == pytest.approx(1.6)


def test_write_prunes_legacy_bucket_rows():
    """The tier used to hold per-bucket rows (keys like "4+"); writing the
    new "latest" row must remove them so they can't linger as parity drift."""
    from regime_platform.core import param_store

    # Seed a legacy-shaped row.
    _run(param_store.write_params([{
        "symbol": "EURUSD", "tier_type": param_store.TIER_DYNAMIC_ATR,
        "tier_key": "4+", "tier_value": "-0.25", "n": 133, "source": "legacy",
    }]))
    assert param_store.read_float("EURUSD", param_store.TIER_DYNAMIC_ATR, "4+") is not None

    _run(ta.write_pick_to_store("EURUSD", _pick(tp_atr=1.7, sl_atr=0.9)))

    assert param_store.read_float("EURUSD", param_store.TIER_DYNAMIC_ATR, "4+") is None
    keys = set(param_store.read_tier("EURUSD", param_store.TIER_DYNAMIC_ATR))
    assert keys == {"latest"}


def test_delete_keys_keeps_named_keys():
    from regime_platform.core import param_store
    _run(param_store.write_params([
        {"symbol": "X", "tier_type": "dynamic_atr", "tier_key": "latest", "tier_value": "1.0"},
        {"symbol": "X", "tier_type": "dynamic_atr", "tier_key": "2-4", "tier_value": "0.5"},
        {"symbol": "X", "tier_type": "dynamic_atr", "tier_key": "4+", "tier_value": "0.1"},
    ]))
    n = param_store.delete_keys("X", "dynamic_atr", keep_keys=("latest",))
    assert n == 2
    assert set(param_store.read_tier("X", "dynamic_atr")) == {"latest"}


# ── the light path applies only when live ────────────────────────────────

def test_light_apply_flag_defaults_live():
    """GEOMETRY HARMONY (2026-10-12, worklog 20261012_GEOMETRY_HARMONY.md):
    the ATR pick is the single source of truth, so the light path defaults
    live — it applies the pick, not logs-and-discards it. Brick is the
    fail-open fallback (see test_light_apply_live_overrides_geometry for the
    apply contract, and the shadow path is retained for A/B via monkeypatch
    in test_light_apply_shadow_keeps_brick_geometry)."""
    from regime_platform.core import feature_flags as ff
    assert ff.get_dynamic_atr_light_mode() == "live"


def test_light_apply_shadow_keeps_brick_geometry(monkeypatch):
    """In shadow, the published bracket is byte-identical to the brick
    geometry — the pick is logged, not applied."""
    from regime_platform.core import feature_flags as ff

    _run(ta.write_pick_to_store("EURUSD", _pick(tp_atr=5.0, sl_atr=0.1)))

    monkeypatch.setattr(ff, "get_dynamic_atr_light_mode", lambda: "shadow")
    # Re-derive the bracket exactly as _process_symbol does.
    entry, brick, sl_b, tp_b, atr = 100.0, 0.5, 3, 6, 1.0
    signal = "buy"
    tp0, sl0 = entry + brick * tp_b, entry - brick * sl_b
    pick = ta.read_pick_from_store("EURUSD", max_age_hours=6.0)
    mode = ff.get_dynamic_atr_light_mode()
    tp, sl = tp0, sl0
    if mode == "live" and pick is not None:
        tp, sl = pick.to_price_levels(entry, atr, signal)
    assert (tp, sl) == (tp0, sl0)          # shadow: unchanged
    # The pick WOULD have moved it a long way — proving the flag matters.
    would_tp, would_sl = pick.to_price_levels(entry, atr, signal)
    assert (would_tp, would_sl) != (tp0, sl0)


def test_light_apply_live_overrides_geometry(monkeypatch):
    from regime_platform.core import feature_flags as ff

    _run(ta.write_pick_to_store("EURUSD", _pick(tp_atr=5.0, sl_atr=0.1)))
    monkeypatch.setattr(ff, "get_dynamic_atr_light_mode", lambda: "live")

    entry, brick, sl_b, tp_b, atr = 100.0, 0.5, 3, 6, 1.0
    tp0, sl0 = entry + brick * tp_b, entry - brick * sl_b
    pick = ta.read_pick_from_store("EURUSD", max_age_hours=6.0)
    tp, sl = tp0, sl0
    if ff.get_dynamic_atr_light_mode() == "live" and pick is not None:
        tp, sl = pick.to_price_levels(entry, atr, "buy")
    assert tp == pytest.approx(entry + 5.0)   # 5.0 ATR
    assert sl == pytest.approx(entry - 0.1)   # 0.1 ATR
