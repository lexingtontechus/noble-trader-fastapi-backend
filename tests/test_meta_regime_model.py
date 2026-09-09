"""Tests for regime_platform/sweep_orchestrator/meta_regime_model.py —
the GaussianHMM meta-state model over the signal-generation stream.
Worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §3-5.
"""

import random

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
    assert features.shape == (1, 4)
    assert features[0, 2] == 1.0  # no predecessor -> neutral persistence
    assert features[0, 3] == 0.0  # < 2 samples -> no volatility signal yet


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


# ─── v2 regression: confidence spike WITHOUT a label change ─────────────────
# Worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §9's v1
# limitation, reproduced directly: the real XAUUSD 2026-09-07 13:01 case had
# regime_label constant ("med_low_vol_strong_bear") throughout, p_win barely
# moving (0.546->0.562), and regime_conf spiking 0.477->0.896 in one tick and
# staying elevated (0.88-0.96) for the next 11 ticks. v1 (3 features, no
# rolling volatility) relied almost entirely on label_persistence -- which is
# CONSTANT here and therefore carries zero discriminating information -- so a
# regime_conf spike with no label change went uncaught. v2 adds
# regime_conf_volatility specifically to give the fit a signal that does NOT
# depend on the label ever changing.


def test_build_features_regime_conf_spike_has_elevated_volatility():
    """Feature-level regression, using the REAL recorded XAUUSD sequence
    (regime_conf values from the worklog's table) rather than synthetic
    round numbers -- proves the volatility feature is keyed off regime_conf
    (column 1), not p_win/confidence_deviation (column 0), and correctly
    flags the spike tick and its immediate aftermath as volatile while a
    calm run before it is not."""
    # entered_at, regime_conf, p_win -- straight from the worklog's table,
    # label constant throughout (the whole point of the regression).
    real_sequence = [
        (0.477, 0.546), (0.482, 0.535), (0.896, 0.562),  # <- spike at index 2
        (0.889, 0.536), (0.882, 0.555), (0.957, 0.578),
    ]
    history = [
        {"ts": float(i), "direction": "sell", "regime_label": "med_low_vol_strong_bear",
         "regime_conf": rc, "p_win": pw}
        for i, (rc, pw) in enumerate(real_sequence)
    ]
    features = _build_features(history)

    # Every row shares the same label -> zero discriminating power there.
    assert (features[:, 2] == 1.0).all()

    # The spike tick (index 2) must show materially higher regime_conf
    # volatility than the calm tick right before it (index 1).
    assert features[2, 3] > features[1, 3]
    assert features[2, 3] > 0.15  # a jump of ~0.4 in the rolling window is not noise


def test_model_distinguishes_stable_from_oscillating_confidence_under_constant_label():
    """End-to-end HMM regression: label NEVER changes (label_persistence
    provides zero information, forcing the model to rely on the v2
    features), p_win is identical in both segments (confidence_deviation
    provides zero information too), and both segments have roughly the
    same MEAN regime_conf (~0.4-0.95 uniform, midpoint ~0.675, close to
    the stable segment's constant 0.70) -- differing only in whether
    regime_conf is stable or irregularly jittering tick-to-tick. Uses a
    fixed-seed RANDOM jitter, not a deterministic period-2 alternation --
    an early version of this test used strict alternation (0.5, 0.9, 0.5,
    0.9, ...) and the HMM correctly treated that as two perfectly
    predictable, non-noisy states (a clean periodic pattern IS what a
    2-state Markov chain represents best) rather than as one noisy state
    the way real, irregular confidence jitter should be treated.
    """
    def _stable_row(ts):
        return {"ts": ts, "direction": "sell", "regime_label": "bear",
                "regime_conf": 0.70, "p_win": 0.55}

    rng = random.Random(42)

    def _noisy_row(ts):
        return {"ts": ts, "direction": "sell", "regime_label": "bear",
                "regime_conf": rng.uniform(0.4, 0.95), "p_win": 0.55}

    stable_segment = [_stable_row(float(i)) for i in range(40)]
    noisy_segment = [_noisy_row(float(40 + i)) for i in range(40)]
    history = stable_segment + noisy_segment

    fitted = fit(history)
    assert fitted is not None

    result_stable = predict_latest(fitted, stable_segment)
    result_noisy = predict_latest(fitted, history)

    assert result_stable["state"] == STATE_COHERENT
    assert result_noisy["state"] == STATE_NOISY
    assert result_stable["coherent_prob"] > result_noisy["coherent_prob"]
