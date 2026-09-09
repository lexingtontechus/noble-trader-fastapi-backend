"""Tests for regime_platform/sweep_orchestrator/meta_regime_model.py —
the GaussianHMM meta-state model over the signal-generation stream.
Worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §3-5.
"""

import pytest

from regime_platform.sweep_orchestrator.meta_regime_model import (
    MIN_TICKS_TO_FIT,
    STATE_COHERENT,
    STATE_NOISY,
    _build_features,
    fit,
    predict_latest,
    summarize_since_last_transition,
)


def _coherent_row(ts, label="bull"):
    return {"ts": ts, "direction": "buy", "regime_label": label, "regime_conf": 0.90, "p_win": 0.80}


def _noisy_row(ts, i):
    # Alternates label every tick, low/wobbling confidence and p_win near 0.5
    # -- the exact XAUUSD flicker pattern from the worklog's real example.
    label = "bull" if i % 2 == 0 else "bear"
    return {"ts": ts, "direction": "buy", "regime_label": label, "regime_conf": 0.35, "p_win": 0.52}


def _synthetic_history(n_coherent=40, n_noisy=40):
    history = [_coherent_row(float(i)) for i in range(n_coherent)]
    history += [_noisy_row(float(n_coherent + i), i) for i in range(n_noisy)]
    return history


def test_fit_returns_none_below_min_ticks():
    history = _synthetic_history(n_coherent=10, n_noisy=10)
    assert len(history) < MIN_TICKS_TO_FIT
    assert fit(history) is None


def test_fit_succeeds_at_min_ticks():
    history = _synthetic_history(n_coherent=25, n_noisy=25)
    assert len(history) == MIN_TICKS_TO_FIT
    fitted = fit(history)
    assert fitted is not None
    assert fitted.n_ticks_fit == MIN_TICKS_TO_FIT
    assert set(fitted.state_by_component.values()) == {STATE_COHERENT, STATE_NOISY}


def test_model_distinguishes_coherent_from_noisy_segments():
    history = _synthetic_history(n_coherent=40, n_noisy=40)
    fitted = fit(history)
    assert fitted is not None

    # Predict on just the coherent prefix vs. the full (ending in noisy).
    coherent_only = history[:40]
    result_coherent = predict_latest(fitted, coherent_only)
    result_noisy = predict_latest(fitted, history)

    assert result_coherent["state"] == STATE_COHERENT
    assert result_noisy["state"] == STATE_NOISY
    assert result_coherent["coherent_prob"] > result_noisy["coherent_prob"]


def test_summarize_since_last_transition_reports_current_run():
    history = _synthetic_history(n_coherent=40, n_noisy=40)
    fitted = fit(history)
    assert fitted is not None

    summary = summarize_since_last_transition(fitted, history)

    assert summary["current_state"] == STATE_NOISY
    # The noisy segment is 40 ticks -- allow slack for EM boundary noise,
    # but it should be a large majority of that segment, not a sliver.
    assert summary["ticks_in_current_state"] >= 20
    assert summary["since_ts"] is not None
    assert summary["n_transitions_total"] >= 1


def test_summarize_on_empty_history_returns_zero_shape_not_none():
    history = _synthetic_history(n_coherent=25, n_noisy=25)
    fitted = fit(history)
    assert fitted is not None

    summary = summarize_since_last_transition(fitted, [])

    assert summary == {
        "current_state": None, "ticks_in_current_state": 0,
        "since_ts": None, "n_transitions_total": 0,
    }


def test_build_features_first_row_has_neutral_label_persistence():
    history = [_coherent_row(0.0)]
    features = _build_features(history)
    assert features.shape == (1, 3)
    assert features[0, 2] == 1.0  # no predecessor -> neutral persistence


def test_build_features_missing_p_win_and_regime_conf_default_neutrally():
    history = [{"ts": 0.0, "direction": "buy", "regime_label": "bull",
                "regime_conf": None, "p_win": None}]
    features = _build_features(history)
    assert features[0, 0] == pytest.approx(0.0)  # abs(0.5-0.5) fallback
    assert features[0, 1] == pytest.approx(0.0)  # regime_conf fallback


def test_build_features_label_change_sets_persistence_zero():
    history = [_coherent_row(0.0, label="bull"), _coherent_row(1.0, label="bear")]
    features = _build_features(history)
    assert features[0, 2] == 1.0
    assert features[1, 2] == 0.0
