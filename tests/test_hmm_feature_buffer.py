"""Tests for the HMM feature buffer."""

import numpy as np
import pytest

from regime_platform.services.hmm.feature_buffer import (
    FEATURE_NAMES,
    N_FEATURES,
    FeatureBuffer,
)


def test_feature_names_count():
    assert len(FEATURE_NAMES) == 9
    assert N_FEATURES == 9


def test_push_and_matrix():
    buf = FeatureBuffer(window=10)
    vec = np.zeros(N_FEATURES)
    vec[0] = 0.01  # log_return

    buf.push("BTCUSD", vec)
    buf.push("BTCUSD", vec)

    mat = buf.matrix("BTCUSD")
    assert mat is not None
    assert mat.shape == (2, N_FEATURES)
    assert mat[0, 0] == pytest.approx(0.01)


def test_matrix_empty_returns_none():
    buf = FeatureBuffer(window=10)
    assert buf.matrix("BTCUSD") is None


def test_matrix_single_bar_returns_none():
    """A single bar can't form a matrix — need at least 2."""
    buf = FeatureBuffer(window=10)
    buf.push("BTCUSD", np.zeros(N_FEATURES))
    assert buf.matrix("BTCUSD") is None


def test_window_trims():
    buf = FeatureBuffer(window=3)
    for i in range(5):
        vec = np.zeros(N_FEATURES)
        vec[0] = float(i)
        buf.push("BTCUSD", vec)

    mat = buf.matrix("BTCUSD")
    assert mat is not None
    assert mat.shape == (3, N_FEATURES)
    # Should keep the last 3 bars (indices 2, 3, 4)
    assert mat[0, 0] == pytest.approx(2.0)
    assert mat[2, 0] == pytest.approx(4.0)


def test_latest():
    buf = FeatureBuffer(window=10)
    vec1 = np.zeros(N_FEATURES)
    vec1[0] = 0.01
    vec2 = np.zeros(N_FEATURES)
    vec2[0] = 0.02

    buf.push("BTCUSD", vec1)
    buf.push("BTCUSD", vec2)

    latest = buf.latest("BTCUSD")
    assert latest is not None
    assert latest[0] == pytest.approx(0.02)


def test_latest_empty():
    buf = FeatureBuffer(window=10)
    assert buf.latest("BTCUSD") is None


def test_n_bars():
    buf = FeatureBuffer(window=10)
    assert buf.n_bars("BTCUSD") == 0
    buf.push("BTCUSD", np.zeros(N_FEATURES))
    assert buf.n_bars("BTCUSD") == 1
    buf.push("BTCUSD", np.zeros(N_FEATURES))
    assert buf.n_bars("BTCUSD") == 2


def test_symbols():
    buf = FeatureBuffer(window=10)
    buf.push("BTCUSD", np.zeros(N_FEATURES))
    buf.push("ETHUSD", np.zeros(N_FEATURES))
    syms = buf.symbols()
    assert "BTCUSD" in syms
    assert "ETHUSD" in syms


def test_clear_one():
    buf = FeatureBuffer(window=10)
    buf.push("BTCUSD", np.zeros(N_FEATURES))
    buf.push("ETHUSD", np.zeros(N_FEATURES))
    buf.clear("BTCUSD")
    assert buf.n_bars("BTCUSD") == 0
    assert buf.n_bars("ETHUSD") == 1


def test_clear_all():
    buf = FeatureBuffer(window=10)
    buf.push("BTCUSD", np.zeros(N_FEATURES))
    buf.push("ETHUSD", np.zeros(N_FEATURES))
    buf.clear()
    assert buf.n_bars("BTCUSD") == 0
    assert buf.n_bars("ETHUSD") == 0


def test_push_wrong_shape():
    buf = FeatureBuffer(window=10)
    with pytest.raises(ValueError):
        buf.push("BTCUSD", np.zeros(N_FEATURES + 1))


def test_multiple_symbols_independent():
    buf = FeatureBuffer(window=10)
    vec_btc = np.zeros(N_FEATURES)
    vec_btc[0] = 0.01
    vec_eth = np.zeros(N_FEATURES)
    vec_eth[0] = 0.02

    buf.push("BTCUSD", vec_btc)
    buf.push("BTCUSD", vec_btc)
    buf.push("ETHUSD", vec_eth)

    assert buf.n_bars("BTCUSD") == 2
    assert buf.n_bars("ETHUSD") == 1
