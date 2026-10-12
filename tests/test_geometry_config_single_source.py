"""E2E: geometry config is a SINGLE source (2026-10-12, docs/scopes/
20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md §1, §3.1).

Pins three things the redesign fixed:

  1. FOREX is defined ONCE (by_symbol.FOREX). The duplicate by_class.forex
     band — which disagreed on the tp grid — is gone, so every forex major
     resolves to the SAME band.
  2. A symbol's OWN band always wins when the symbol is passed; passing
     symbol=None skips by_symbol and silently resolves to the class/global
     band (the divergence that stored BTCUSD tp=13.0 against its own max
     8.80). The pooled fallback therefore passes the inheriting symbol.
  3. The pooled solve is keyed by the resolved BAND, so symbols sharing a
     band (forex majors) share one solve while distinct bands (metals) do not.
"""
from __future__ import annotations

import pytest

from regime_platform.core.feature_flags import get_dynamic_atr_config
from regime_platform.sweep_orchestrator import dynamic_tier_optimizer as opt
from regime_platform.sweep_orchestrator import tiered_atr as ta

FOREX_MAJORS = ("AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDCHF", "USDJPY")


def _band_fp(c: dict):
    return (
        tuple(c.get("tp_atr") or ()),
        tuple(c.get("sl_atr") or ()),
        c.get("tp_atr_min"), c.get("tp_atr_max"),
        c.get("sl_atr_min"), c.get("sl_atr_max"),
    )


def _pick(tp=2.0, sl=1.0, n=200):
    return ta.DynamicAtrPick(tp_atr=tp, sl_atr=sl, exp_atr=0.1, exp_h1=0.1,
                             exp_h2=0.1, expiry=0.1, n=n)


def test_forex_majors_share_one_band():
    """Every forex major resolves to the SAME band — by_symbol.FOREX. The
    removed by_class.forex grid ([1.65,1.71,1.80]) must not reappear."""
    bands = {s: get_dynamic_atr_config("forex", s) for s in FOREX_MAJORS}
    fps = {_band_fp(b) for b in bands.values()}
    assert len(fps) == 1, f"forex majors disagree: { {s: _band_fp(b) for s, b in bands.items()} }"
    # the surviving definition is by_symbol.FOREX's grid, not the deleted by_class one
    assert tuple(bands["EURUSD"]["tp_atr"]) == (1.65, 1.71, 1.75, 1.80)
    assert bands["EURUSD"]["tp_atr_max"] == 1.85


def test_no_by_class_forex_band_remains():
    """symbol=None skips by_symbol → global (NOT a forex-specific band). This
    documents WHY callers must pass a symbol; a forex-specific by_class band
    would have masked the divergence."""
    glob = get_dynamic_atr_config("forex", None)
    assert glob["tp_atr_max"] == 20.0   # global, not 1.85
    assert tuple(glob["tp_atr"]) == (1.71, 2.0, 3.0, 5.0, 8.0, 13.0)


def test_symbol_band_wins_and_symbol_none_falls_through():
    """The BTCUSD violation the redesign fixes: with the symbol, tp_atr_max is
    the symbol's own 8.80; without it, the wide global 20.0 (which is how a
    pooled pick stored 13.0)."""
    with_sym = get_dynamic_atr_config("crypto", "BTCUSD")
    assert with_sym["tp_atr_max"] == 8.80
    without = get_dynamic_atr_config("crypto", None)
    assert without["tp_atr_max"] == 20.0


@pytest.mark.asyncio
async def test_pooled_pick_inherits_each_symbols_own_band(monkeypatch):
    """The pooled fallback resolves the INHERITING symbol's band and shares one
    pooled solve per distinct band: forex majors (one band) → 1 solve; a
    crypto symbol with its own band → 1 more."""
    seen: list[tuple[str, float | None]] = []   # (class, tp_atr_max) per pooled call

    async def _fake_pick(asset_class, *, cfg, min_rr, symbol=None, use_store=True):
        if symbol is None:                       # the pooled call
            seen.append((asset_class, cfg.get("tp_atr_max")))
            return _pick()
        return None                              # per-symbol: too few paths

    monkeypatch.setattr(ta, "dynamic_atr_pick", _fake_pick)

    async def _no_store(*a, **k):
        return True
    monkeypatch.setattr(ta, "write_pick_to_store", _no_store)

    async def _no_thread(fn, *a, **k):
        return 0
    monkeypatch.setattr(opt.asyncio, "to_thread", _no_thread)

    peers = [
        {"symbol": "EURUSD", "asset_class": "forex", "tradingview_symbol": "OANDA:EURUSD"},
        {"symbol": "GBPUSD", "asset_class": "forex", "tradingview_symbol": "OANDA:GBPUSD"},
        {"symbol": "USDJPY", "asset_class": "forex", "tradingview_symbol": "OANDA:USDJPY"},
        {"symbol": "BTCUSD", "asset_class": "crypto", "tradingview_symbol": "COINBASE:BTCUSD"},
    ]
    picks = await opt._refresh_dynamic_atr(peers)
    assert set(picks) == {"EURUSD", "GBPUSD", "USDJPY", "BTCUSD"}
    # forex majors share by_symbol.FOREX → ONE pooled solve at tp_atr_max 1.85;
    # BTCUSD has its own band → ONE pooled solve at 8.80. Total 2, not 4.
    assert len(seen) == 2, seen
    assert sorted(v for _, v in seen) == [1.85, 8.80]
