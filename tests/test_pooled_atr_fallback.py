"""E2E: asset-class pooled ATR fallback (2026-10-12, worklog
20261012_POOLED_ATR_AND_SCORECARD_FILTER.md).

The per-symbol ATR solve fails for 13/14 symbols (2-3 paths each < min_signals
80), so the dynamic-ATR geometry never ships. The refresh must fall back to an
asset-class POOLED pick (solved once per class, enough paths) and inherit it
under each symbol. Fail-open chain: per-symbol -> pooled -> brick.

These tests drive the real _refresh_dynamic_atr with a mocked picker to pin
the fallback contract without a live TradingView/Supabase walk.
"""
from __future__ import annotations

import asyncio
import pytest

from regime_platform.sweep_orchestrator import dynamic_tier_optimizer as opt
from regime_platform.sweep_orchestrator import tiered_atr as ta


def _pick(tp=2.0, sl=1.0, n=200):
    return ta.DynamicAtrPick(tp_atr=tp, sl_atr=sl, exp_atr=0.1, exp_h1=0.1,
                             exp_h2=0.1, expiry=0.1, n=n)


PEERS = [
    {"symbol": "EURUSD", "asset_class": "forex", "tradingview_symbol": "OANDA:EURUSD"},
    {"symbol": "GBPUSD", "asset_class": "forex", "tradingview_symbol": "OANDA:GBPUSD"},
    {"symbol": "USDJPY", "asset_class": "forex", "tradingview_symbol": "OANDA:USDJPY"},
    {"symbol": "XAUUSD", "asset_class": "metal", "tradingview_symbol": "OANDA:XAUUSD"},
]


@pytest.mark.asyncio
async def test_per_symbol_none_falls_back_to_pooled(monkeypatch):
    """When the per-symbol solve returns None, the symbol inherits the pooled
    class pick and it is persisted under the symbol."""
    calls = {"per_symbol": 0, "pooled": 0}

    async def _fake_pick(asset_class, *, cfg, min_rr, symbol=None, use_store=True):
        if symbol is None:
            calls["pooled"] += 1
            return _pick(tp=3.0, sl=1.5, n=500)   # pooled has enough paths
        calls["per_symbol"] += 1
        return None                                # per-symbol: too few paths

    monkeypatch.setattr(ta, "dynamic_atr_pick", _fake_pick)
    # neutralize the store write + prune (param_store is imported inside the
    # refresh fn; stub to_thread so the delete_keys call is a no-op)
    async def _no_store(*a, **k):
        return True
    monkeypatch.setattr(ta, "write_pick_to_store", _no_store)
    async def _no_thread(fn, *a, **k):
        return 0
    monkeypatch.setattr(opt.asyncio, "to_thread", _no_thread)

    picks = await opt._refresh_dynamic_atr(PEERS)
    # every symbol got a pick via the pooled fallback
    assert set(picks) == {"EURUSD", "GBPUSD", "USDJPY", "XAUUSD"}
    # pooled solved ONCE per class (forex once, metal once), not per symbol
    assert calls["pooled"] == 2
    assert calls["per_symbol"] == 4
    # the inherited pick is the pooled geometry
    assert picks["EURUSD"].tp_atr == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_per_symbol_pick_preferred_over_pooled(monkeypatch):
    """When the per-symbol solve succeeds, it is used — pooled is only a
    fallback, not a replacement."""
    async def _fake_pick(asset_class, *, cfg, min_rr, symbol=None, use_store=True):
        if symbol is None:
            return _pick(tp=9.0, sl=1.0, n=999)    # pooled (should NOT be used)
        return _pick(tp=2.0, sl=1.0, n=100)        # per-symbol wins

    monkeypatch.setattr(ta, "dynamic_atr_pick", _fake_pick)
    async def _no_store(*a, **k):
        return True
    monkeypatch.setattr(ta, "write_pick_to_store", _no_store)
    async def _no_thread(fn, *a, **k):
        return 0
    monkeypatch.setattr(opt.asyncio, "to_thread", _no_thread)

    picks = await opt._refresh_dynamic_atr(PEERS)
    assert all(p.tp_atr == pytest.approx(2.0) for p in picks.values())


@pytest.mark.asyncio
async def test_both_none_fails_open_to_brick(monkeypatch):
    """Per-symbol None AND pooled None -> no pick -> brick fallback (fail-open
    chain fully preserved)."""
    async def _fake_pick(asset_class, *, cfg, min_rr, symbol=None, use_store=True):
        return None

    monkeypatch.setattr(ta, "dynamic_atr_pick", _fake_pick)
    async def _no_thread(fn, *a, **k):
        return 0
    monkeypatch.setattr(opt.asyncio, "to_thread", _no_thread)

    picks = await opt._refresh_dynamic_atr(PEERS)
    assert picks == {}
