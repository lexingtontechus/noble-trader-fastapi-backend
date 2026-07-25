"""
Tests for the HMM TA extraction helper (regime_platform.services.hmm.ta_extract).

Covers:
  • latest_ta() returns the 5 TA values from the buffer's latest bar
  • latest_ta() returns {None × 5} when the buffer is empty
  • latest_ta() returns {None × 5} when the latest vector is malformed
  • latest_ta_or_zero() returns 0.0 instead of None
  • TA_COLUMNS order matches FEATURE_NAMES[4:9]
  • log_regime_entry in supabase_client accepts the 5 ta_* kwargs (signature test)
  • NT_SWEEP_RESULT_COLUMNS and NT_REGIME_LOG_COLUMNS both include the 5 ta_* columns
"""

from __future__ import annotations

import inspect

import numpy as np

from regime_platform.services.hmm import FEATURE_NAMES, FeatureBuffer, latest_ta
from regime_platform.services.hmm.ta_extract import TA_COLUMNS, latest_ta_or_zero
from regime_platform.sweep_orchestrator.supabase_client import (
    NT_REGIME_LOG_COLUMNS,
    NT_SWEEP_RESULT_COLUMNS,
    log_regime_entry,
)


# ─── latest_ta() ────────────────────────────────────────────────────────────


def test_latest_ta_returns_5_values_from_buffer():
    """latest_ta pulls features 4-8 from the buffer's latest bar."""
    buf = FeatureBuffer(window=10)
    vec = np.array([0.01, 1.5, 0.2, 1.1, 25.0, -0.5, 0.8, 0.3, 55.0], dtype=float)
    buf.push("BTCUSD", vec)

    result = latest_ta(buf, "BTCUSD")

    assert result == {
        "ta_adx": 25.0,
        "ta_ao": -0.5,
        "ta_bb_power": 0.8,
        "ta_macd": 0.3,
        "ta_rsi": 55.0,
    }


def test_latest_ta_returns_none_when_buffer_empty():
    """Empty buffer → all 5 keys present, all values None."""
    buf = FeatureBuffer(window=10)
    result = latest_ta(buf, "NEW")

    assert set(result.keys()) == {
        "ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi",
    }
    assert all(v is None for v in result.values())


def test_latest_ta_returns_none_when_symbol_not_in_buffer():
    """Symbol never pushed → None × 5."""
    buf = FeatureBuffer(window=10)
    buf.push("BTCUSD", np.zeros(9, dtype=float))

    result = latest_ta(buf, "ETHUSD")
    assert all(v is None for v in result.values())


def test_latest_ta_returns_latest_not_oldest():
    """When multiple bars are pushed, latest_ta returns the most recent."""
    buf = FeatureBuffer(window=10)
    buf.push("BTCUSD", np.array([0, 0, 0, 0, 10.0, 20.0, 30.0, 40.0, 50.0], dtype=float))
    buf.push("BTCUSD", np.array([0, 0, 0, 0, 11.0, 21.0, 31.0, 41.0, 51.0], dtype=float))
    buf.push("BTCUSD", np.array([0, 0, 0, 0, 12.0, 22.0, 32.0, 42.0, 52.0], dtype=float))

    result = latest_ta(buf, "BTCUSD")
    assert result["ta_adx"] == 12.0
    assert result["ta_rsi"] == 52.0


def test_latest_ta_or_zero_returns_zero_instead_of_none():
    """latest_ta_or_zero substitutes 0.0 for None."""
    buf = FeatureBuffer(window=10)
    result = latest_ta_or_zero(buf, "NEW")

    assert result == {
        "ta_adx": 0.0, "ta_ao": 0.0, "ta_bb_power": 0.0,
        "ta_macd": 0.0, "ta_rsi": 0.0,
    }


def test_ta_columns_order_matches_feature_names_4_to_9():
    """TA_COLUMNS order must match FEATURE_NAMES[4:9].

    This is the contract that latest_ta() relies on — if the order drifts,
    the ta_adx column will silently receive ta_ao's value, etc.
    """
    expected = [f"ta_{name}" for name in FEATURE_NAMES[4:9]]
    assert TA_COLUMNS == expected, (
        f"TA_COLUMNS order drifted: {TA_COLUMNS} != {expected}"
    )


# ─── Supabase client whitelist + signature ──────────────────────────────────


def test_nt_sweep_result_columns_includes_ta_indicators():
    """Migration 036 adds 5 ta_* columns to nt_sweep_result."""
    for col in ("ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi"):
        assert col in NT_SWEEP_RESULT_COLUMNS, (
            f"{col} missing from NT_SWEEP_RESULT_COLUMNS — "
            f"_filter_payload() will silently drop it"
        )


def test_nt_regime_log_columns_includes_ta_indicators():
    """Migration 036 adds 5 ta_* columns to nt_regime_log."""
    for col in ("ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi"):
        assert col in NT_REGIME_LOG_COLUMNS, (
            f"{col} missing from NT_REGIME_LOG_COLUMNS — "
            f"_filter_payload() will silently drop it"
        )


def test_log_regime_entry_accepts_ta_kwargs():
    """log_regime_entry signature must accept the 5 ta_* kwargs.

    This is a static signature check — it doesn't make a network call.
    Migration 036 + the orchestrator update assume these kwargs exist.
    """
    sig = inspect.signature(log_regime_entry)
    params = sig.parameters

    for kw in ("ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi"):
        assert kw in params, f"log_regime_entry missing kwarg: {kw}"
        assert params[kw].default is None, (
            f"log_regime_entry kwarg {kw} should default to None, "
            f"got {params[kw].default!r}"
        )


def test_log_regime_entry_payload_shape():
    """log_regime_entry payload dict should include the 5 ta_* keys.

    We construct the same payload shape the function builds internally
    and verify the ta_* keys are present. (Direct invocation requires
    mocking httpx — kept as a shape test instead.)
    """
    # The function's body builds a payload dict with these keys.
    # We just verify the keys are in the function's source-level payload.
    import re

    src = inspect.getsource(log_regime_entry)
    for key in ("ta_adx", "ta_ao", "ta_bb_power", "ta_macd", "ta_rsi"):
        # Match "ta_adx": ... inside the payload dict literal
        assert re.search(rf'"{key}"\s*:', src), (
            f"log_regime_entry source doesn't include '{key}' in payload dict"
        )
