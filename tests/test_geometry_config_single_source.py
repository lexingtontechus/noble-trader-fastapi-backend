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
    removed by_class.forex grid must not reappear.

    Deliberately value-agnostic: the band is operator-owned and replay-tuned
    (retuned 2026-10-12 from the RR-dead tight grid to the wide class band), so
    the invariant under test is "all majors share ONE band, and it is
    by_symbol.FOREX" — not any particular set of numbers.
    """
    bands = {s: get_dynamic_atr_config("forex", s) for s in FOREX_MAJORS}
    fps = {_band_fp(b) for b in bands.values()}
    assert len(fps) == 1, f"forex majors disagree: { {s: _band_fp(b) for s, b in bands.items()} }"
    # the surviving definition is by_symbol.FOREX — read it, don't pin it
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))
    assert _band_fp(bands["EURUSD"]) == _band_fp(raw["dynamic_atr"]["by_symbol"]["FOREX"])
    # the deleted duplicate must not come back
    assert "by_class" not in raw["dynamic_atr"]


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


def test_no_band_is_rr_dead():
    """Every configured band must admit at least ONE cell that clears min_rr.

    A band where every cell fails the RR floor can never produce a pick — and
    because G2's last-resort fallback is the band CENTRE (inside the same dead
    range), the book would ship a bracket that violates its own RR floor.

    Regression for the 2026-10-12 finding: git's XAGUSD band (RR 0.99-1.07,
    tp≈7.96 vs sl≈7.76) and XAUAUD (1.23-1.34) were both RR-dead, so two of
    fourteen symbols could never resolve a valid bracket.
    """
    from pathlib import Path

    import yaml

    from regime_platform.core.feature_flags import get_min_rr

    min_rr = get_min_rr()
    root = Path(__file__).resolve().parents[1]
    da = yaml.safe_load(
        (root / "config" / "feature_flags.yaml").read_text(encoding="utf-8")
    )["dynamic_atr"]

    def valid_cells(b: dict) -> list:
        tp = [float(x) for x in b["tp_atr"]]
        sl = [float(x) for x in b["sl_atr"]]
        tmin, tmax = b.get("tp_atr_min"), b.get("tp_atr_max")
        smin, smax = b.get("sl_atr_min"), b.get("sl_atr_max")
        return [
            (t, s) for t in tp for s in sl
            if s > 0 and t / s >= min_rr
            and (tmin is None or t >= tmin) and (tmax is None or t <= tmax)
            and (smin is None or s >= smin) and (smax is None or s <= smax)
        ]

    bands = dict(da.get("by_symbol") or {})
    bands["<GLOBAL>"] = {
        k: v for k, v in da.items()
        if k in ("tp_atr", "sl_atr", "tp_atr_min", "tp_atr_max", "sl_atr_min", "sl_atr_max")
    }
    dead = sorted(name for name, b in bands.items() if not valid_cells(b))
    assert not dead, f"RR-dead bands (no cell clears min_rr={min_rr}): {dead}"


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
    # forex majors share by_symbol.FOREX → ONE pooled solve at that band's
    # tp_atr_max; BTCUSD has its own band → ONE more. Total 2, not 4. The
    # expected ceilings are DERIVED from the config so a band retune doesn't
    # break the invariant being tested (one solve per distinct band).
    assert len(seen) == 2, seen
    expected = sorted([
        get_dynamic_atr_config("forex", "EURUSD")["tp_atr_max"],
        get_dynamic_atr_config("crypto", "BTCUSD")["tp_atr_max"],
    ])
    assert sorted(v for _, v in seen) == expected


def test_code_defaults_mirror_yaml_global():
    """The code `_DEFAULTS["dynamic_atr"]` global must mirror the YAML global
    — ONE definition of the numbers (the alignment claim, previously
    CI-unverified because every test runs with the YAML present, shadowing the
    defaults). Also pins that the removed `by_class` duplicate is gone."""
    from pathlib import Path

    import yaml

    from regime_platform.core.feature_flags import _DEFAULTS

    root = Path(__file__).resolve().parents[1]
    y = yaml.safe_load((root / "config" / "feature_flags.yaml").read_text(encoding="utf-8"))["dynamic_atr"]
    d = _DEFAULTS["dynamic_atr"]
    for k, v in y.items():
        if k in ("by_symbol", "by_class"):
            continue
        assert d.get(k) == v, f"code default {k}={d.get(k)!r} != YAML {v!r}"
    # the removed duplicate must not reappear in code either
    assert "by_class" not in d
    assert "by_symbol" not in d
