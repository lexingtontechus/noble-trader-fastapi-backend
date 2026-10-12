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
    _band_center_pick,
    dynamic_atr_pick,
    select_tp_sl,
)

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
