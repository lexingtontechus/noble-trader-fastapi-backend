"""Single-resolver harmonization — worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md.

Proves the Stage 1-5 contract:
  * ONE primitive (`settlement_contract.resolve_bracket_path`) defines a win.
  * The envelope walk, the price walk and the live resolver all agree.
  * The picker measures on the LIVE basis (intrabar, sl_first) by default.
  * Same-bar ambiguity is detected and the tie-break honours the policy flag.
  * The Stage-5 consistency gate catches a walk contradicting its own bucket.
"""
from __future__ import annotations

import numpy as np
import pytest

from regime_platform.sweep_orchestrator import settlement_contract as sc
from regime_platform.sweep_orchestrator import tiered_atr as ta
from regime_platform.sweep_orchestrator.eod_resolver import resolve_signal_outcome_ohlc


def _window(seed: int, n: int = 48):
    rng = np.random.default_rng(seed)
    entry = 100.0
    close = entry + rng.normal(0, 0.6, n).cumsum()
    hi = close + np.abs(rng.normal(0, 0.4, n))
    lo = close - np.abs(rng.normal(0, 0.4, n))
    return entry, [
        {"h": float(hi[i]), "l": float(lo[i]), "c": float(close[i])} for i in range(n)
    ]


# ── Stage 1: one primitive, three shapes agree ────────────────────────────

def test_envelope_walk_matches_live_resolver():
    """walk_envelopes (picker) and the live resolver must agree 100%."""
    mismatch = 0
    for seed in range(300):
        entry, bars = _window(seed)
        direction = "buy" if seed % 2 == 0 else "sell"
        tp_atr, sl_atr = 1.0 + (seed % 5) * 0.4, 0.25 + (seed % 4) * 0.25
        d = 1.0 if direction == "buy" else -1.0
        tp_price, sl_price = entry + tp_atr * d, entry - sl_atr * d
        live = resolve_signal_outcome_ohlc(entry, sl_price, tp_price, bars, direction=direction)
        fav, adv = sc.bracket_envelope(entry_price=entry, direction=direction,
                                       bars=bars, basis=sc.BASIS_INTRABAR)
        env = np.stack([np.array(fav), np.array(adv)], axis=1)[None, :, :]
        pnl, exp = ta.walk_envelopes(env, tp_atr, sl_atr, same_bar_policy=sc.SAME_BAR_SL_FIRST)
        got = "expired" if exp[0] else ("tp_hit" if pnl[0] > 0 else "sl_hit")
        if got != live["outcome"]:
            mismatch += 1
    assert mismatch == 0, f"{mismatch}/300 envelope walks disagree with the live resolver"


def test_resolve_bracket_path_matches_live_resolver():
    mismatch = 0
    for seed in range(300):
        entry, bars = _window(seed)
        direction = "buy" if seed % 2 == 0 else "sell"
        tp_atr, sl_atr = 1.0 + (seed % 5) * 0.4, 0.25 + (seed % 4) * 0.25
        d = 1.0 if direction == "buy" else -1.0
        tp_price, sl_price = entry + tp_atr * d, entry - sl_atr * d
        live = resolve_signal_outcome_ohlc(entry, sl_price, tp_price, bars, direction=direction)
        new = sc.resolve_bracket_path(entry_price=entry, stop_loss=sl_price,
                                      take_profit=tp_price, direction=direction, bars=bars,
                                      basis=sc.BASIS_INTRABAR, same_bar_policy=sc.SAME_BAR_SL_FIRST)
        if new["outcome"] != live["outcome"]:
            mismatch += 1
    assert mismatch == 0, f"{mismatch}/300 price walks disagree with the live resolver"


def test_envelope_accepts_fetcher_short_keys():
    """The price fetcher emits h/l/c — the primitive must read those."""
    bars = [{"h": 101.0, "l": 99.0, "c": 100.5}, {"h": 103.0, "l": 100.0, "c": 102.0}]
    closes, highs, lows = sc.normalize_bars(bars)
    assert closes == [100.5, 102.0]
    assert highs == [101.0, 103.0]
    assert lows == [99.0, 100.0]


# ── Stage 2: policy flag is honoured, not hardcoded ───────────────────────

def test_same_bar_policy_is_honoured():
    # one bar spans both levels
    bars = [{"h": 103.0, "l": 97.0, "c": 100.0}] * 5
    sl = sc.resolve_bracket_path(entry_price=100.0, stop_loss=97.0, take_profit=103.0,
                                 direction="buy", bars=bars, basis=sc.BASIS_INTRABAR,
                                 same_bar_policy=sc.SAME_BAR_SL_FIRST)
    tp = sc.resolve_bracket_path(entry_price=100.0, stop_loss=97.0, take_profit=103.0,
                                 direction="buy", bars=bars, basis=sc.BASIS_INTRABAR,
                                 same_bar_policy=sc.SAME_BAR_TP_FIRST)
    assert sl["outcome"] == "sl_hit" and sl["ambiguous"] is True
    assert tp["outcome"] == "tp_hit" and tp["ambiguous"] is True


def test_close_basis_is_walkable_and_never_ambiguous():
    """A close-walk can only see one price per bar — no same-bar tie."""
    bars = [{"h": 103.0, "l": 97.0, "c": 100.0}] * 5
    r = sc.resolve_bracket_path(entry_price=100.0, stop_loss=97.0, take_profit=103.0,
                                direction="buy", bars=bars, basis=sc.BASIS_CLOSE)
    assert r["ambiguous"] is False
    assert r["outcome"] == "expired"
    assert r["bar_mode"] == "close"


# ── Stage 4: the picker defaults to the live basis ────────────────────────

def test_live_basis_and_policy_defaults():
    assert ta.live_basis() == "intrabar"
    assert ta.live_same_bar_policy() == "sl_first"


def test_select_tp_sl_prefers_live_envelope_shape():
    """A tight-stop bracket that a close-walk scores as a winner must NOT be
    picked when the envelope carries the wick that trips it."""
    n = 120
    rng = np.random.default_rng(3)
    # closes drift up (favourable for a buy), but every bar has a deep wick
    # down that trips a tight stop.
    close = 100.0 + np.linspace(0, 2.0, 48)
    hi = close + 0.1
    lo = close - 0.9          # deep wick: trips a 0.35 stop
    close_paths = np.vstack([(close - 100.0) for _ in range(n)])
    fav = np.vstack([(hi - 100.0) for _ in range(n)])
    adv = np.vstack([(lo - 100.0) for _ in range(n)])
    env = np.stack([fav, adv], axis=2)
    # close-walk sees only the rising closes -> tight stop looks great
    pick_close = ta.select_tp_sl(close_paths, tp_grid=[1.6], sl_grid=[0.35],
                                 min_rr=1.5, max_expiry=0.2, cost_atr=0.0,
                                 min_signals=60)
    # envelope sees the wick -> the same tight stop is stopped out
    pick_env = ta.select_tp_sl(envelopes=env, tp_grid=[1.6], sl_grid=[0.35],
                               min_rr=1.5, max_expiry=0.2, cost_atr=0.0,
                               min_signals=60, same_bar_policy=sc.SAME_BAR_SL_FIRST)
    assert pick_close is not None and pick_close.exp_atr > 0
    assert pick_env is None, "envelope walk should reject the wick-tripped bracket"


# ── Stage 5: consistency gate ─────────────────────────────────────────────

def test_consistency_gate_rejects_contradiction():
    ok, reason = ta.check_pick_reachability_consistency(0.48, 0.05, 400)
    assert ok is False and "0.43" in reason


def test_consistency_gate_fails_open_on_cold_or_missing():
    assert ta.check_pick_reachability_consistency(0.48, 0.05, 5)[0] is True
    assert ta.check_pick_reachability_consistency(0.48, None, 400)[0] is True
    assert ta.check_pick_reachability_consistency(None, 0.05, 400)[0] is True


def test_consistency_gate_passes_when_agreeing():
    assert ta.check_pick_reachability_consistency(0.20, 0.18, 200)[0] is True


# ── atr_reopt measures on the live basis ──────────────────────────────────

def test_atr_reopt_eval_cell_takes_envelope():
    from regime_platform.sweep_orchestrator.atr_reopt import eval_cell
    rng = np.random.default_rng(5)
    n = 120
    close = 100.0 + rng.normal(0, 0.6, (n, 48)).cumsum(axis=1)
    paths = (close - 100.0)
    env = np.stack([paths, paths], axis=2)
    sc_ = eval_cell(env, 1.0, 1.0)
    assert sc_ is not None
    assert sc_["n"] == n
