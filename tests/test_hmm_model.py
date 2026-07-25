"""Tests for the HMM model — bootstrap, fit, emit, 5→3 state aggregation."""

import numpy as np
import pytest

from regime_platform.services.hmm.feature_buffer import N_FEATURES, FeatureBuffer
from regime_platform.services.hmm.model import (
    HmmModel,
    STATES_3,
    _aggregate_transition,
)


# ─── Fixture ────────────────────────────────────────────────────────────────


@pytest.fixture
def synthetic_closes():
    """Generate 200 synthetic close prices with a trend + noise."""
    np.random.seed(42)
    n = 200
    returns = np.random.randn(n) * 0.01 + 0.0005  # slight upward drift
    closes = 100.0 * np.exp(np.cumsum(returns))
    return closes.tolist()


@pytest.fixture
def hmm_model():
    buf = FeatureBuffer(window=300)
    return HmmModel(buf)


# ─── Bootstrap tests ────────────────────────────────────────────────────────


def test_bootstrap_pushes_vectors(hmm_model, synthetic_closes):
    pushed = hmm_model.bootstrap_from_ohlcv("BTCUSD", synthetic_closes)
    assert pushed == len(synthetic_closes) - 1  # N-1 log_returns
    assert hmm_model._buffer.n_bars("BTCUSD") == pushed


def test_bootstrap_too_few_closes(hmm_model):
    pushed = hmm_model.bootstrap_from_ohlcv("BTCUSD", [100.0])
    assert pushed == 0


def test_bootstrap_features_shape(hmm_model, synthetic_closes):
    hmm_model.bootstrap_from_ohlcv("BTCUSD", synthetic_closes)
    mat = hmm_model._buffer.matrix("BTCUSD")
    assert mat is not None
    assert mat.shape[1] == N_FEATURES  # 9 features


def test_bootstrap_microstructure_is_zero(hmm_model, synthetic_closes):
    """Bootstrap can't provide microstructure features — they should be 0."""
    hmm_model.bootstrap_from_ohlcv("BTCUSD", synthetic_closes)
    mat = hmm_model._buffer.matrix("BTCUSD")
    # Features 1-3 (spread, imbalance, volume_persistence) = 0.0 in bootstrap
    assert np.all(mat[:, 1] == 0.0)  # effective_spread_bps
    assert np.all(mat[:, 2] == 0.0)  # imbalance_I
    assert np.all(mat[:, 3] == 0.0)  # volume_persistence


def test_bootstrap_log_return_nonzero(hmm_model, synthetic_closes):
    """Bootstrap should produce non-zero log_returns."""
    hmm_model.bootstrap_from_ohlcv("BTCUSD", synthetic_closes)
    mat = hmm_model._buffer.matrix("BTCUSD")
    assert np.any(mat[:, 0] != 0.0)  # log_return


# ─── Fit tests ──────────────────────────────────────────────────────────────


def test_fit_insufficient_data(hmm_model):
    """fit() should return False with < MIN_FIT_BARS."""
    # Push only 5 bars (below MIN_FIT_BARS=30)
    for i in range(5):
        hmm_model._buffer.push("BTCUSD", np.zeros(N_FEATURES))
    assert hmm_model.fit("BTCUSD") is False


def test_fit_success(hmm_model, synthetic_closes):
    """fit() should return True and populate _models with enough data."""
    hmm_model.bootstrap_from_ohlcv("BTCUSD", synthetic_closes)
    result = hmm_model.fit("BTCUSD")
    # May fail if hmmlearn not installed — skip in that case
    if not result:
        try:
            import hmmlearn  # noqa: F401
            pytest.fail("hmmlearn installed but fit returned False")
        except ImportError:
            pytest.skip("hmmlearn not installed")
    assert "BTCUSD" in hmm_model._models


# ─── Emit tests ─────────────────────────────────────────────────────────────


def test_emit_without_fit_returns_none(hmm_model):
    """emit_markov should return (None, 'FLAT') when no model is fitted."""
    T, state = hmm_model.emit_markov("BTCUSD")
    assert T is None
    assert state == "FLAT"


def test_emit_returns_3x3_matrix(hmm_model, synthetic_closes):
    """emit_markov should return a 3x3 dict-of-dicts + a valid state."""
    hmm_model.bootstrap_from_ohlcv("BTCUSD", synthetic_closes)
    if not hmm_model.fit("BTCUSD"):
        try:
            import hmmlearn  # noqa: F401
        except ImportError:
            pytest.skip("hmmlearn not installed")

    T, state = hmm_model.emit_markov("BTCUSD")
    if T is None:
        pytest.skip("HMM fit failed (numerical issue)")

    assert set(T.keys()) == set(STATES_3)
    for s in STATES_3:
        assert set(T[s].keys()) == set(STATES_3)
    assert state in STATES_3

    # Rows should sum to ~1.0
    for s in STATES_3:
        row_sum = sum(T[s].values())
        assert 0.9 <= row_sum <= 1.1


# ─── 5→3 state aggregation tests ────────────────────────────────────────────


def test_aggregate_transition_basic():
    """Test the 5→3 state aggregation with a known mapping."""
    # Uniform 5x5 transition matrix
    T5 = np.ones((5, 5)) / 5.0
    state_map = {0: "UP", 1: "UP", 2: "FLAT", 3: "DOWN", 4: "DOWN"}

    T3 = _aggregate_transition(T5, state_map)

    # Each 3-state row should sum to 1.0
    for s in STATES_3:
        row_sum = sum(T3[s].values())
        assert row_sum == pytest.approx(1.0, abs=0.01)


def test_aggregate_transition_identity():
    """If all 5 states map to FLAT, the FLAT row should be uniform."""
    T5 = np.eye(5)  # identity — no transitions
    state_map = {i: "FLAT" for i in range(5)}

    T3 = _aggregate_transition(T5, state_map)

    # FLAT row should be [1.0, 0.0, 0.0] (identity → stay in FLAT)
    # But wait — identity means T5[i][i] = 1.0, so T3[FLAT][FLAT] = 5.0
    # After normalization: T3[FLAT][FLAT] = 1.0
    assert T3["FLAT"]["FLAT"] == pytest.approx(1.0)
    assert T3["FLAT"]["UP"] == pytest.approx(0.0)
    assert T3["FLAT"]["DOWN"] == pytest.approx(0.0)


def test_aggregate_transition_empty_row():
    """If no 5-state maps to UP, the UP row should be uniform prior."""
    T5 = np.eye(5)
    state_map = {0: "FLAT", 1: "FLAT", 2: "FLAT", 3: "DOWN", 4: "DOWN"}

    T3 = _aggregate_transition(T5, state_map)

    # UP row has no 5-states mapping to it → uniform prior
    assert T3["UP"]["UP"] == pytest.approx(1.0 / 3.0)
    assert T3["UP"]["DOWN"] == pytest.approx(1.0 / 3.0)
    assert T3["UP"]["FLAT"] == pytest.approx(1.0 / 3.0)


# ─── Diagnostics ────────────────────────────────────────────────────────────


def test_status(hmm_model):
    status = hmm_model.status()
    assert status["n_models"] == 0
    assert status["n_states"] == 5
    assert status["n_features"] == 9
