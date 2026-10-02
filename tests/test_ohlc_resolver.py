"""
Tests for `eod_resolver.resolve_signal_outcome_ohlc` — first-touch resolution.

The load-bearing case is `test_ghost_edge_close_walk_misses_intrabar_touch`:
a bar whose CLOSE sits safely between SL and TP but whose LOW wicks through the
stop. The close-walk scores it "no touch" (eventually tp_hit/expired); the
intrabar walk scores it sl_hit. That difference IS the ghost edge.
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.eod_resolver import (
    _resolve_signal_outcome_legacy,
    resolve_signal_outcome_ohlc,
)

# Long entry 100, SL 98, TP 105
E, SL, TP = 100.0, 98.0, 105.0


def _b(h, l, c):
    return {"h": h, "l": l, "c": c}


# ─── The ghost edge, isolated ─────────────────────────────────────────────


def test_ghost_edge_close_walk_misses_intrabar_touch():
    """The defining case. Bar 1 closes at 102 (between SL and TP) but wicks to
    97 — through the stop. Bar 2 closes at 106 (through TP).

    close-walk  -> tp_hit   (never saw the wick)  = the ghost edge
    intrabar    -> sl_hit   (the stop filled first)
    """
    bars = [_b(103.0, 97.0, 102.0), _b(107.0, 101.0, 106.0)]

    ohlc = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    legacy = _resolve_signal_outcome_legacy(E, SL, TP, [102.0, 106.0], "buy")

    assert legacy["outcome"] == "tp_hit"      # optimistic
    assert ohlc["outcome"] == "sl_hit"        # honest
    assert ohlc["outcome"] != legacy["outcome"]


def test_ghost_edge_mae_is_larger_than_close_mae():
    """The MAE basis differs for the same reason — intrabar MAE >= close MAE.

    Note the legacy close-walk can report a NEGATIVE MAE (it takes the max over
    the walked path with no floor), so the assertion is a comparison, not an
    absolute: the intrabar walk sees the wick the close-walk cannot.
    """
    bars = [_b(103.0, 97.0, 102.0)]
    ohlc = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    legacy = _resolve_signal_outcome_legacy(E, SL, TP, [102.0], "buy")
    assert ohlc["mae"] == pytest.approx(3.0)      # 100 - 97, the wick
    assert legacy["mae"] < ohlc["mae"]            # close-walk never saw it


# ─── First-touch semantics ────────────────────────────────────────────────


def test_tp_first_when_target_touched_on_earlier_bar():
    bars = [_b(106.0, 99.0, 105.5), _b(108.0, 96.0, 97.0)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["outcome"] == "tp_hit"
    assert r["touch_bar"] == 0


def test_sl_first_when_stop_touched_on_earlier_bar():
    bars = [_b(101.0, 97.0, 98.5), _b(110.0, 100.0, 109.0)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["outcome"] == "sl_hit"
    assert r["touch_bar"] == 0


def test_short_direction_inverts_levels():
    # short: entry 100, SL 102 (above), TP 95 (below)
    bars = [_b(101.0, 94.0, 96.0)]
    r = resolve_signal_outcome_ohlc(100.0, 102.0, 95.0, bars, "sell")
    assert r["outcome"] == "tp_hit"


def test_short_stop_hit():
    bars = [_b(103.0, 99.0, 102.5)]
    r = resolve_signal_outcome_ohlc(100.0, 102.0, 95.0, bars, "sell")
    assert r["outcome"] == "sl_hit"


# ─── Ambiguity is reported, not hidden ────────────────────────────────────


def test_same_bar_both_touched_is_flagged_and_conservative():
    """A bar spanning both levels cannot be ordered from OHLC. It must be
    flagged AND resolved conservatively (SL first), matching live semantics."""
    bars = [_b(110.0, 90.0, 100.0)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["ambiguous"] is True
    assert r["outcome"] == "sl_hit"


def test_ambiguous_false_on_clean_bars():
    bars = [_b(103.0, 99.0, 101.0)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["ambiguous"] is False
    assert r["outcome"] == "expired"


# ─── Expiry and degenerate input ──────────────────────────────────────────


def test_expired_when_neither_level_touched():
    bars = [_b(103.0, 99.0, 101.0), _b(104.0, 99.5, 102.0)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["outcome"] == "expired"
    assert r["exit_price"] == pytest.approx(102.0)   # last close
    assert r["hold_bars"] == 2


def test_expiry_window_is_respected():
    """A touch beyond the expiry window must not count."""
    bars = [_b(103.0, 99.0, 101.0)] * 2 + [_b(110.0, 100.0, 109.0)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy", expiry_bars=2)
    assert r["outcome"] == "expired"


def test_bars_missing_high_low_fall_back_to_close():
    """A partial series degrades to a close-walk rather than being dropped."""
    bars = [{"c": 106.0}]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["outcome"] == "tp_hit"


def test_empty_bars_is_expired():
    r = resolve_signal_outcome_ohlc(E, SL, TP, [], "buy")
    assert r["outcome"] == "expired"
    assert r["exit_price"] is None
    assert r["hold_bars"] == 0


def test_malformed_bars_are_skipped_not_fatal():
    bars = [{"h": None, "l": None, "c": None}, _b(106.0, 101.0, 105.5)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["outcome"] == "tp_hit"


def test_zero_expiry_yields_expired():
    r = resolve_signal_outcome_ohlc(E, SL, TP, [_b(110.0, 90.0, 100.0)],
                                    "buy", expiry_bars=0)
    assert r["outcome"] == "expired"


# ─── Return shape ─────────────────────────────────────────────────────────


def test_return_keys_superset_of_legacy():
    """Downstream writers expect the legacy keys; the new ones are additive."""
    legacy = _resolve_signal_outcome_legacy(E, SL, TP, [101.0], "buy")
    ohlc = resolve_signal_outcome_ohlc(E, SL, TP, [_b(102.0, 100.0, 101.0)], "buy")
    missing = set(legacy) - set(ohlc)
    assert not missing, f"ohlc resolver dropped keys: {missing}"
    assert {"ambiguous", "touch_bar"} <= set(ohlc)


def test_hold_bars_counts_to_the_touch():
    bars = [_b(101.0, 99.0, 100.5), _b(102.0, 98.5, 100.0), _b(106.0, 101.0, 105.5)]
    r = resolve_signal_outcome_ohlc(E, SL, TP, bars, "buy")
    assert r["outcome"] == "tp_hit"
    assert r["hold_bars"] == 3
