"""G2 + G3 (2026-10-12) — the geometry solve ALWAYS returns geometry.

docs/scopes/20261012_GEOMETRY_COHERENT_FLOW_REDESIGN.md §3.2 (G2) and §3.3 (G3).

G2: ``select_tp_sl`` / ``dynamic_atr_pick`` never return None when a bracket can
    be formed, so nothing downstream can fall back to a stale or brick bracket.
    When no in-band candidate clears the strict both-halves bar, the solve
    returns the best-expectancy in-band bracket from the CURRENT window.
G3: the light path resolves through that one solve instead of reading the pick
    store directly and leaving the brick geometry on a miss.
"""
import numpy as np
import pytest

from regime_platform.sweep_orchestrator.tiered_atr import (
    EXPIRY_BARS,
    DynamicAtrPick,
    _band_center_pick,
    dynamic_atr_pick,
    select_tp_sl,
)


def _pick(tp: float, sl: float, n: int = 200) -> DynamicAtrPick:
    """A walked-looking pick, for exercising the band/store guards."""
    return DynamicAtrPick(tp_atr=tp, sl_atr=sl, exp_atr=0.1, exp_h1=0.1,
                          exp_h2=0.1, expiry=0.1, n=n)

GRID_TP = [1.0, 2.0, 5.0]
GRID_SL = [1.0, 2.0, 5.0]


def _two_regime_env(n: int = 100) -> np.ndarray:
    """Half the rows reach +10 ATR favourable with a shallow adverse excursion;
    the other half is the mirror. No bracket is positive in BOTH halves, but the
    wide-TP/tight-SL bracket has the highest overall expectancy (+2.0)."""
    env = np.zeros((n, EXPIRY_BARS, 2), dtype="float64")
    h = n // 2
    env[:h, :, 0], env[:h, :, 1] = 10.0, -0.5     # TP hit, SL not
    env[h:, :, 0], env[h:, :, 1] = 0.4, -10.0     # SL hit, TP not
    return env


def _always_tp_env(n: int = 100) -> np.ndarray:
    """Every row reaches TP and never the SL — so several brackets clear the
    strict bar, and the best one must still win."""
    env = np.zeros((n, EXPIRY_BARS, 2), dtype="float64")
    env[:, :, 0], env[:, :, 1] = 10.0, -0.5
    return env


def _select(env, **kw):
    args = dict(tp_grid=GRID_TP, sl_grid=GRID_SL, min_rr=1.0, max_expiry=0.2,
                cost_atr=0.0, min_signals=1, envelopes=env)
    args.update(kw)
    return select_tp_sl(**args)


# ── G2: select_tp_sl ────────────────────────────────────────────────────────

def test_best_effort_returns_highest_expectancy_in_band_bracket():
    """Nothing clears both halves, but the solve still returns the best in-band
    bracket rather than None (G2)."""
    pick = _select(_two_regime_env())
    assert pick is not None, "G2: solve must not return None"
    assert (pick.tp_atr, pick.sl_atr) == (5.0, 1.0)   # highest overall expectancy
    assert pick.exp_atr == pytest.approx(2.0)
    assert pick.exp_h1 > 0 and pick.exp_h2 < 0, "this env is deliberately a regime split"


def test_best_effort_can_be_switched_off_for_parity():
    """The old strict behaviour stays reachable — explicit opt-out only."""
    assert _select(_two_regime_env(), allow_best_effort=False) is None


def test_best_effort_respects_the_published_clamps():
    """The fallback is still an IN-BAND bracket — never outside the band."""
    pick = _select(_two_regime_env(), tp_atr_max=2.0)
    assert pick is not None
    assert pick.tp_atr <= 2.0, "fallback must not escape the tp clamp"
    assert (pick.tp_atr, pick.sl_atr) == (2.0, 1.0)   # best bracket inside tp<=2


def test_strict_winner_still_beats_best_effort():
    """A bracket that clears the bar wins outright; best-effort is a fallback,
    not a replacement."""
    pick = _select(_always_tp_env())
    assert pick is not None
    assert (pick.tp_atr, pick.sl_atr) == (5.0, 1.0)
    assert pick.exp_h1 > 0 and pick.exp_h2 > 0, "a genuine both-halves pass"


# ── G2: the deliberate band-centre last resort ──────────────────────────────

def test_band_center_pick_is_the_mid_band_bracket():
    pick = _band_center_pick({"tp_atr": [2.0, 4.0, 6.0], "sl_atr": [1.0, 2.0, 3.0]})
    assert isinstance(pick, type(_band_center_pick({"tp_atr": [1.0], "sl_atr": [1.0]})))
    assert (pick.tp_atr, pick.sl_atr) == (4.0, 2.0)   # middle of each band
    assert pick.n == 0, "no evidence walked — this is the operator's deliberate value"


def test_band_center_pick_none_only_when_band_unconfigured():
    assert _band_center_pick({"tp_atr": [], "sl_atr": [1.0]}) is None
    assert _band_center_pick({}) is None


# ── G2: dynamic_atr_pick never returns None on a resolvable band ────────────

CFG = {
    "tp_atr": [2.0, 4.0, 6.0], "sl_atr": [1.0, 2.0, 3.0],
    "tp_atr_min": None, "tp_atr_max": None, "sl_atr_min": None, "sl_atr_max": None,
    "max_expiry": 0.2, "cost_atr": 0.1, "min_signals": 60, "lookback_days": 10,
    "vol_ratio_min": None, "vol_ratio_max": None,
    "vol_ratio_short": 24, "vol_ratio_long": 168,
}


@pytest.mark.asyncio
async def test_dynamic_atr_pick_returns_band_centre_when_no_members(monkeypatch):
    """No active symbols is not a reason to hand back None — the solve returns
    the symbol's deliberate mid-band geometry (G2)."""
    async def _none():
        return []
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols", _none,
    )
    pick = await dynamic_atr_pick(
        "forex", cfg=dict(CFG), min_rr=1.5, symbol="EURUSD", use_store=False,
    )
    assert pick is not None, "G2: must not return None"
    assert (pick.tp_atr, pick.sl_atr) == (4.0, 2.0)


@pytest.mark.asyncio
async def test_dynamic_atr_pick_still_none_when_opt_out(monkeypatch):
    """Parity: the strict contract survives when explicitly requested."""
    async def _none():
        return []
    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols", _none,
    )
    pick = await dynamic_atr_pick(
        "forex", cfg=dict(CFG), min_rr=1.5, symbol="EURUSD",
        use_store=False, allow_best_effort=False,
    )
    assert pick is None


@pytest.mark.asyncio
async def test_whole_grid_below_min_rr_still_resolves(monkeypatch):
    """A band whose ENTIRE grid fails the min_rr pre-filter must still resolve to
    geometry (the band centre) — never None, because the light path reads None as
    "keep the brick bracket".

    Regression for git's XAGUSD (grid RR 0.99-1.07) and XAUAUD (1.23-1.34) bands:
    every cell was skipped, `select_tp_sl` returned None, and `dynamic_atr_pick`
    passed that None straight through — so the ATR geometry never reached those
    symbols at all. Verified empirically before the fix:
        select_tp_sl(GIT XAGUSD)       -> None
        dynamic_atr_pick(GIT XAGUSD)   -> None   (light path keeps BRICK)
    """
    from regime_platform.sweep_orchestrator import tiered_atr as t

    async def _members():
        return [{"symbol": "XAGUSD", "asset_class": "commodities",
                 "tradingview_symbol": "OANDA:XAGUSD"}]

    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols", _members)

    async def _sigs(symbols, since, until):
        return [{"symbol": "XAGUSD", "signal": "buy", "entry_price": 20.0,
                 "atr": 0.2, "_ts": float(i)} for i in range(80)]

    monkeypatch.setattr(t, "_load_class_signals", _sigs)

    async def _bars(rows, **kw):
        out = kw.get("ohlcv_out")
        if out is not None:
            out["XAGUSD"] = [{"t": 3600.0 * i, "c": 20.0 + i * 0.01} for i in range(400)]
        return out

    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.price_fetcher.fetch_tradingview_batch", _bars)
    monkeypatch.setattr(t, "build_envelopes",
                        lambda *a, **k: (np.zeros((80, EXPIRY_BARS, 2)), None))
    monkeypatch.setattr(t, "select_tp_sl", lambda *a, **k: None)   # the dead band

    async def _no_store(*a, **k):
        return True

    monkeypatch.setattr(t, "write_pick_to_store", _no_store)

    pick = await t.dynamic_atr_pick(
        None, cfg=dict(CFG), min_rr=1.5, symbol="XAGUSD", use_store=False,
    )
    assert pick is not None, "a configured band must never resolve to None (G2)"
    assert (pick.tp_atr, pick.sl_atr) == (4.0, 2.0), "must fall back to the band centre"


def test_pick_in_band_rejects_a_previous_bands_bracket():
    """G1 at the STORE boundary. The store is a cache of PAST solves, so a pick
    fresh by AGE can still carry a bracket from a previous band. Live examples:
    BTCUSD was served tp=13ATR against a band capping tp at 8.4/8.8, and US30USD
    was served tp=8/sl=5 against a 12.5-13.1 / 2.25-2.6 band."""
    from regime_platform.sweep_orchestrator.tiered_atr import _pick_in_band

    cfg = {"tp_atr": [7.8, 8.07, 8.4], "sl_atr": [1.65, 1.77, 1.95],
           "tp_atr_min": 7.5, "tp_atr_max": 8.8, "sl_atr_min": 1.55, "sl_atr_max": 2.1}
    assert _pick_in_band(_pick(tp=8.07, sl=1.77), cfg), "a grid cell is in band"
    assert not _pick_in_band(_pick(tp=13.0, sl=2.0), cfg), "BTCUSD's real case"
    assert not _pick_in_band(_pick(tp=8.0, sl=1.77), cfg), "in-clamp but not a grid cell"

    cfg30 = {"tp_atr": [12.5, 12.8, 13.1], "sl_atr": [2.25, 2.41, 2.6],
             "tp_atr_min": 12.0, "tp_atr_max": 13.5, "sl_atr_min": 2.1, "sl_atr_max": 2.8}
    assert not _pick_in_band(_pick(tp=8.0, sl=5.0), cfg30), "US30USD's real case"
    assert _pick_in_band(_pick(tp=12.8, sl=2.41), cfg30)


@pytest.mark.asyncio
async def test_out_of_band_store_hit_is_recomputed_not_applied(monkeypatch):
    """An out-of-band store hit is a MISS, not a hit — the symbol gets a freshly
    solved bracket rather than a stale one carried over from a previous band."""
    from regime_platform.sweep_orchestrator import tiered_atr as t

    # the store holds a bracket from a PREVIOUS band (BTCUSD's real case)
    monkeypatch.setattr(t, "read_pick_from_store", lambda *a, **k: _pick(tp=13.0, sl=2.0))

    async def _members():
        return [{"symbol": "XAGUSD", "asset_class": "commodities",
                 "tradingview_symbol": "OANDA:XAGUSD"}]

    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols", _members)

    async def _sigs(symbols, since, until):
        return [{"symbol": "XAGUSD", "signal": "buy", "entry_price": 20.0,
                 "atr": 0.2, "_ts": float(i)} for i in range(80)]

    monkeypatch.setattr(t, "_load_class_signals", _sigs)

    async def _bars(rows, **kw):
        out = kw.get("ohlcv_out")
        if out is not None:
            out["XAGUSD"] = [{"t": 3600.0 * i, "c": 20.0 + i * 0.01} for i in range(400)]
        return out

    monkeypatch.setattr(
        "regime_platform.sweep_orchestrator.price_fetcher.fetch_tradingview_batch", _bars)
    monkeypatch.setattr(t, "build_envelopes",
                        lambda *a, **k: (np.zeros((80, EXPIRY_BARS, 2)), None))
    monkeypatch.setattr(t, "select_tp_sl", lambda *a, **k: None)   # recompute → band centre

    async def _no_store(*a, **k):
        return True

    monkeypatch.setattr(t, "write_pick_to_store", _no_store)

    pick = await t.dynamic_atr_pick(None, cfg=dict(CFG), min_rr=1.5, symbol="XAGUSD")
    assert pick is not None
    assert (pick.tp_atr, pick.sl_atr) == (4.0, 2.0), (
        "must recompute within the current band, not apply the stale 13/2"
    )


# ── G3: the light path goes through the one solve ───────────────────────────

def test_light_path_resolves_through_the_solve_not_the_store():
    """G3 wiring: the light apply block calls dynamic_atr_pick (store-hit or
    recompute) and no longer reads the store directly, so a miss cannot leave
    the brick bracket in place."""
    import inspect

    from regime_platform.sweep_orchestrator import orchestrator as orch
    src = inspect.getsource(orch.SweepOrchestrator._process_symbol)
    block = src.split("Dynamic ATR geometry apply", 1)[1].split("R2: p_win_bracket", 1)[0]
    assert "dynamic_atr_pick(" in block, "G3: light path must call the one solve"
    assert "read_pick_from_store" not in block, "G3: no separate store read path"
    assert "G3" in block
