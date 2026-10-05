"""Tests for the regime-label stability replay's pure core
(scripts/replay_regime_label_stability.py).

No network: ``sweep_symbol`` is exercised on a synthetic price series and the
flip-rate helpers on hand-built sequences. The live TDVA fetch in ``main`` is
not tested here (it is the replay harness, not logic) — same precedent as the
other ``scripts/replay_*.py`` artifacts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "replay_regime_label_stability", ROOT / "scripts" / "replay_regime_label_stability.py"
)
R = importlib.util.module_from_spec(_spec)
# Register before exec — module-level @dataclass needs the module in
# sys.modules to resolve its own namespace.
sys.modules[_spec.name] = R
_spec.loader.exec_module(R)


# ─── flip_rate ────────────────────────────────────────────────────────────


def test_flip_rate_all_same_is_zero():
    assert R.flip_rate(["a", "a", "a", "a"]) == 0.0


def test_flip_rate_every_tick_flips_is_one():
    assert R.flip_rate(["a", "b", "a", "b"]) == 1.0


def test_flip_rate_half():
    # pairs: a→a (no), a→b (yes), b→b (no) => 1/3
    assert R.flip_rate(["a", "a", "b", "b"]) == 1 / 3


def test_flip_rate_skips_none_without_counting_a_flip():
    # None is a missing read, not a change: a, None, a => 0 flips.
    assert R.flip_rate(["a", None, "a"]) == 0.0
    # a, None, b => one comparable pair (a→b) => 1.0
    assert R.flip_rate(["a", None, "b"]) == 1.0


def test_flip_rate_single_value_is_zero():
    assert R.flip_rate(["a"]) == 0.0
    assert R.flip_rate([]) == 0.0


# ─── major_minor_flips ────────────────────────────────────────────────────


def test_major_flip_is_direction_family_change():
    seq = ["low_vol_bull", "low_vol_bear"]  # bull -> bear = major
    assert R.major_minor_flips(seq) == (1, 0)


def test_minor_flip_is_adjacent_bucket_same_family():
    seq = ["low_vol_bull", "low_vol_strong_bull"]  # both bull = minor
    assert R.major_minor_flips(seq) == (0, 1)


def test_major_minor_counts_mixed_sequence():
    seq = [
        "low_vol_bull", "low_vol_strong_bull",  # minor
        "high_vol_bear",                         # major
        "high_vol_strong_bear",                  # minor
    ]
    assert R.major_minor_flips(seq) == (1, 2)


def test_major_minor_skips_none():
    assert R.major_minor_flips([None, "low_vol_bull", None, "low_vol_bear"]) == (1, 0)


# ─── sweep_symbol (pure core, synthetic prices) ───────────────────────────


def _prices(seed=0, n=260, trend=0.0):
    rng = np.random.RandomState(seed)
    return 100 + np.cumsum(rng.normal(trend, 0.01, size=n))


def test_sweep_symbol_produces_all_series_and_debouncer_levels():
    r = R.sweep_symbol("TEST", list(_prices()), window=150, ticks=60)
    assert r.n_ticks == 60
    assert len(r.raw_full) == 60
    assert len(r.raw_cell) == 60
    assert len(r.raw_trend) == 60
    assert len(r.confidences) == 60
    assert set(r.debounced.keys()) == set(R.N_CONFIRM_LEVELS)
    for n in R.N_CONFIRM_LEVELS:
        assert len(r.debounced[n].labels) == 60
    assert 0.0 <= r.consolidating_share <= 1.0


def test_sweep_symbol_debouncer_reduces_flip_rate():
    # On a noisy series the raw label churns; the debouncer must not churn
    # MORE than raw (it can only hold a label longer).
    r = R.sweep_symbol("TEST", list(_prices(seed=3)), window=150, ticks=80)
    s = r.summary()
    assert s["full_flip_n3"] <= s["raw_full_flip"] + 1e-9
    assert s["full_flip_n4"] <= s["full_flip_n3"] + 1e-9


def test_sweep_symbol_summary_has_expected_keys():
    r = R.sweep_symbol("TEST", list(_prices(seed=5)), window=150, ticks=40)
    s = r.summary()
    for k in (
        "symbol", "n_ticks", "raw_full_flip", "raw_cell_flip", "raw_trend_flip",
        "raw_major_flips", "raw_minor_flips", "consolidating_share",
        "mean_confidence", "full_flip_n2", "full_flip_n3", "full_flip_n4",
    ):
        assert k in s, f"missing summary key {k}"


def test_sweep_symbol_insufficient_data_raises():
    import pytest
    with pytest.raises(ValueError):
        R.sweep_symbol("TEST", [100.0] * 50, window=150, ticks=10)


def test_sweep_symbol_trend_filter_reads_consolidating_on_flat_tape():
    # A zig-zag around a constant mean is the trend filter's canonical
    # 'consolidating' shape — the platform recognising faded momentum.
    n = 260
    flat = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(n)]
    r = R.sweep_symbol("FLAT", flat, window=150, ticks=40)
    assert r.consolidating_share > 0.9


# ─── symbol-def mapping ───────────────────────────────────────────────────


def test_symbol_defs_exchange_prefixes():
    defs = {d["symbol"]: d["tradingview_symbol"] for d in R._symbol_defs(["AAPL", "XAUUSD", "BTCUSD"])}
    assert defs["AAPL"] == "NASDAQ:AAPL"
    assert defs["XAUUSD"] == "OANDA:XAUUSD"
    assert defs["BTCUSD"] == "COINBASE:BTCUSD"
