"""Tests for the RegimeHMM stability fixes (R1, 2026-08-23 — worklog/
20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md; feature standardization,
2026-08-24).

Covers:
  1. Feature standardization (``_fit_scale``/``_standardize``/
     ``_destandardize_means``) — the 2026-08-24 fix for chronic
     "Model is not converging" (root cause, not just the overconfidence
     symptom fix #1 originally targeted). Supersedes and removes
     ``_data_min_covar`` (the old data-relative covariance floor).
  2. ``RegimeHMM._label_states`` with ``previous_means`` — continuity
     matching across refits (fix #2, targets the FLIP-FLOPPING symptom).
  3. ``SweepOrchestrator._smoothed_trend_probs`` — the supplementary EMA
     smoother, mirroring ``_smoothed_markov_hold_prob``.
  4. ``RegimeHMM._fit_converged`` — the R4 convergence diagnostic (non-
     convergence as a live blend-weight trigger).
  5. ``RegimeHMM.trend_transition_matrix_3state`` — Part 1 Change B
     (2026-08-25): derives p_markov_hold_n's 3x3 {UP,DOWN,FLAT} matrix
     from the trend model instead of the separate, numerically-unstable
     legacy HmmModel (regime_platform/services/hmm/model.py).
"""
from types import SimpleNamespace

import numpy as np
from pytest import approx as pytest_approx
from unittest.mock import MagicMock

from regime_platform.core.regime_engine import RegimeHMM
from regime_platform.services.statistics.ev_engine import EVEngine, P_WIN_WEIGHTS_FULL
from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator


# ─── 1. Feature standardization (fix #3) ────────────────────────────────


def test_fit_scale_returns_mean_and_std_per_column():
    features = np.array([[0.0, 10.0], [1.0, 20.0], [2.0, 30.0]])
    mu, sigma = RegimeHMM._fit_scale(features)
    assert np.allclose(mu, [1.0, 20.0])
    assert np.allclose(sigma, np.std(features, axis=0))


def test_fit_scale_floors_zero_variance_column_to_avoid_division_by_zero():
    """A constant column (e.g. a degenerate window, or one of the
    real-world unscaled-feature cases confirmed live) must not produce
    inf/NaN when standardized — sigma floors to 1.0, making
    standardization a no-op (mean-centering only) for that column."""
    features = np.array([[5.0, 1.0], [5.0, 2.0], [5.0, 3.0]])
    mu, sigma = RegimeHMM._fit_scale(features)
    assert sigma[0] == 1.0
    standardized = RegimeHMM._standardize(features, (mu, sigma))
    assert np.all(np.isfinite(standardized))
    assert np.allclose(standardized[:, 0], 0.0)


def test_standardize_produces_zero_mean_unit_variance_columns():
    rng = np.random.RandomState(0)
    features = rng.normal(loc=[5.0, -3.0, 100.0], scale=[0.001, 2.0, 50.0], size=(200, 3))
    scale = RegimeHMM._fit_scale(features)
    standardized = RegimeHMM._standardize(features, scale)
    assert np.allclose(np.std(standardized, axis=0), 1.0, atol=1e-8)
    assert np.allclose(np.mean(standardized, axis=0), 0.0, atol=1e-8)


def test_destandardize_means_recovers_original_units():
    """After fitting in standardized space, means_ converted back must
    match the ORIGINAL feature scale — this is what keeps _label_states,
    continuity matching, and vol_label_means/trend_label_means working on
    the same units as before fix #3, with no changes needed to that code."""
    mu = np.array([5.0, -3.0, 100.0])
    sigma = np.array([0.001, 2.0, 50.0])
    standardized_means = np.array([[1.0, 0.5, -0.5], [-1.0, -0.5, 0.5]])
    fake_model = SimpleNamespace(means_=standardized_means.copy())
    RegimeHMM._destandardize_means(fake_model, (mu, sigma))
    expected = standardized_means * sigma + mu
    assert np.allclose(fake_model.means_, expected)


def test_build_hmm_uses_fixed_standardized_min_covar():
    """The old per-window _data_min_covar computation is gone — every
    model gets the same fixed floor, which is only correct because
    features are standardized (std=1) before fitting."""
    model = RegimeHMM(n_components=4)
    hmm_instance = model._build_hmm()
    assert hmm_instance.min_covar == RegimeHMM._STANDARDIZED_MIN_COVAR


def test_fit_converges_with_realistic_scale_disparity():
    """Regression test for the 2026-08-24 fix: reproduces the real-world
    feature-scale disparity confirmed live (trend model returns std
    ~0.0004 vs cum_long std ~0.003, roughly 8x — see
    scripts/diagnose_hmm_convergence.py, tested against real EURUSD/
    XAUUSD/BTCUSD data) via a synthetic price series with the same return
    scale, and asserts the resulting fit converges cleanly."""
    rng = np.random.RandomState(7)
    returns = rng.normal(0, 0.0004, size=250)
    prices = 100 * np.exp(np.cumsum(returns))
    model = RegimeHMM(n_components=4)
    model.fit(prices)
    snap = model.predict(prices)
    assert snap.trend_converged is True
    assert snap.vol_converged is True


# ─── 2. Label continuity across refits ──────────────────────────────────


def _fake_model(means_col):
    """A minimal stand-in with just what _label_states reads. Trend
    labels read feature column 2 (see _label_states' feature_idx logic),
    so pad with 3 columns and place the given values in column 2."""
    m = MagicMock()
    m.n_components = len(means_col)
    m.means_ = np.array([[0.0, 0.0, v] for v in means_col])
    return m


def test_no_previous_means_falls_back_to_rank_sort():
    """A symbol's first fit (no history yet) behaves exactly like the
    pre-fix rank-sort — unchanged behaviour when there's nothing to be
    continuous WITH."""
    model = _fake_model([0.001, -0.002, 0.003, -0.004])
    labels = ["strong_bear", "bear", "bull", "strong_bull"]
    label_map = RegimeHMM._label_states(model, labels, previous_means=None)
    # Ascending means -> strong_bear(-0.004), bear(-0.002), bull(0.001), strong_bull(0.003)
    means = model.means_[:, 2]
    order = np.argsort(means)
    expected = {int(order[i]): labels[i] for i in range(4)}
    assert label_map == expected


def test_continuity_prevents_relabel_on_near_tied_means():
    """The AAPL scenario: two components' means are nearly tied. Without
    continuity, a tiny perturbation between refits can swap their rank
    order and relabel 'bull' as 'strong_bull' with no real change.
    With continuity, each component sticks to whichever label it was
    closest to last time."""
    labels = ["strong_bear", "bear", "bull", "strong_bull"]
    # Fit 1: component 2 is "bull" (0.0011), component 3 is "strong_bull" (0.0012) — nearly tied.
    model_1 = _fake_model([-0.005, -0.001, 0.0011, 0.0012])
    map_1 = RegimeHMM._label_states(model_1, labels, previous_means=None)
    prev_means = {lbl: float(model_1.means_[i, 2]) for i, lbl in map_1.items()}
    assert prev_means["bull"] == 0.0011
    assert prev_means["strong_bull"] == 0.0012

    # Fit 2: a tiny perturbation swaps which RAW component index has the
    # higher mean (component 2 is now 0.00121, component 3 is 0.00119) —
    # a blind rank-sort would swap "bull" and "strong_bull" here even
    # though nothing about the market changed.
    model_2 = _fake_model([-0.0051, -0.0009, 0.00121, 0.00119])
    map_2 = RegimeHMM._label_states(model_2, labels, previous_means=prev_means)

    # Continuity must keep component 2 (still ~0.0011-ish) labeled "bull"
    # and component 3 labeled "strong_bull" — matched by proximity to the
    # PREVIOUS fit's means, not re-sorted blind.
    assert map_2[2] == "bull"
    assert map_2[3] == "strong_bull"


def test_continuity_still_tracks_a_genuine_regime_change():
    """Continuity matching must not permanently freeze a label — when a
    component's mean moves decisively (a real regime change), it should
    still end up closest to its own previous value, not get stuck stealing
    another label."""
    labels = ["strong_bear", "bear", "bull", "strong_bull"]
    model_1 = _fake_model([-0.01, -0.003, 0.003, 0.01])
    map_1 = RegimeHMM._label_states(model_1, labels, previous_means=None)
    prev_means = {lbl: float(model_1.means_[i, 2]) for i, lbl in map_1.items()}

    # Component 1 ("bear") drifts decisively toward neutral/positive —
    # still closest to its OWN previous value among the candidates.
    model_2 = _fake_model([-0.011, 0.0005, 0.0031, 0.0102])
    map_2 = RegimeHMM._label_states(model_2, labels, previous_means=prev_means)
    assert map_2[1] == "bear"
    assert map_2[0] == "strong_bear"
    assert map_2[2] == "bull"
    assert map_2[3] == "strong_bull"


def test_fit_persists_label_means_for_next_call():
    """Integration-level: fit() on real (tiny, synthetic) data populates
    trend_label_means/vol_label_means, and passing them back into a
    second fit() doesn't raise and produces a valid label map."""
    rng = np.random.RandomState(1)
    prices = 100 + np.cumsum(rng.normal(0, 0.01, size=150))
    model = RegimeHMM(n_components=4)
    model.fit(prices)
    means_1 = model.trend_label_means
    assert set(means_1.keys()) == set(RegimeHMM.TREND_LABELS_BY_N[4])

    model_2 = RegimeHMM(n_components=4)
    model_2.fit(prices, prev_trend_means=means_1, prev_vol_means=model.vol_label_means)
    assert set(model_2.trend_label_means.keys()) == set(RegimeHMM.TREND_LABELS_BY_N[4])


# ─── 3. Orchestrator-level trend_probs EMA smoothing ────────────────────


def _fake_orchestrator():
    return SimpleNamespace(_regime_trend_ema={})


def test_first_tick_seeds_unsmoothed():
    fake_self = _fake_orchestrator()
    probs = {"bull": 0.9, "bear": 0.05, "strong_bull": 0.03, "strong_bear": 0.02}
    smoothed = SweepOrchestrator._smoothed_trend_probs(fake_self, "AAPL", probs)
    assert smoothed == probs
    assert fake_self._regime_trend_ema["AAPL"] == probs


def test_regime_flip_is_dampened(monkeypatch):
    from regime_platform.core.settings import settings
    monkeypatch.setattr(settings, "REGIME_EMA_ALPHA", 0.3)

    fake_self = _fake_orchestrator()
    bull_heavy = {"bull": 0.95, "bear": 0.02, "strong_bull": 0.02, "strong_bear": 0.01}
    bear_heavy = {"bull": 0.02, "bear": 0.95, "strong_bull": 0.01, "strong_bear": 0.02}

    SweepOrchestrator._smoothed_trend_probs(fake_self, "AAPL", bull_heavy)
    smoothed_2 = SweepOrchestrator._smoothed_trend_probs(fake_self, "AAPL", bear_heavy)

    # A full flip to bear-heavy must land BETWEEN the two raw extremes,
    # not jump straight to the new raw distribution.
    assert 0.02 < smoothed_2["bear"] < 0.95
    assert smoothed_2["bull"] > 0.02


def test_probs_stay_normalized():
    fake_self = _fake_orchestrator()
    SweepOrchestrator._smoothed_trend_probs(
        fake_self, "AAPL", {"bull": 0.6, "bear": 0.4},
    )
    smoothed = SweepOrchestrator._smoothed_trend_probs(
        fake_self, "AAPL", {"bull": 0.1, "bear": 0.1, "strong_bull": 0.8},
    )
    assert abs(sum(smoothed.values()) - 1.0) < 1e-9


def test_per_symbol_isolation():
    fake_self = _fake_orchestrator()
    SweepOrchestrator._smoothed_trend_probs(fake_self, "AAPL", {"bull": 0.9, "bear": 0.1})
    SweepOrchestrator._smoothed_trend_probs(fake_self, "US500", {"bull": 0.1, "bear": 0.9})
    assert set(fake_self._regime_trend_ema.keys()) == {"AAPL", "US500"}
    assert fake_self._regime_trend_ema["AAPL"] != fake_self._regime_trend_ema["US500"]


# ─── 4. R4: convergence diagnostic + blend-weight trigger ──────────────


def _fake_monitor(history):
    return SimpleNamespace(history=history)


def _fake_model_with_monitor(history):
    return SimpleNamespace(monitor_=_fake_monitor(history))


def test_fit_converged_true_for_monotonic_history():
    """A clean EM run (log-likelihood non-decreasing throughout) must
    read as converged."""
    model = _fake_model_with_monitor([100.0, 105.0, 106.0, 106.001])
    assert RegimeHMM._fit_converged(model) is True


def test_fit_converged_false_for_any_decrease():
    """Exactly the condition that fires hmmlearn's own 'Model is not
    converging' warning — ANY consecutive decrease beyond the same
    precision tolerance hmmlearn uses internally, not just the last step."""
    model = _fake_model_with_monitor([100.0, 105.0, 104.97, 106.0])
    assert RegimeHMM._fit_converged(model) is False


def test_fit_converged_ignores_negligible_float_noise():
    """A decrease smaller than hmmlearn's own precision bound
    (sqrt(machine epsilon), ~1.5e-8) must NOT trip the flag — otherwise
    this would fire on virtually every real fit from floating-point noise
    alone."""
    model = _fake_model_with_monitor([100.0, 105.0, 105.0 - 1e-12, 106.0])
    assert RegimeHMM._fit_converged(model) is True


def test_fit_converged_true_when_history_too_short_to_compare():
    """Fewer than 2 history entries -> nothing to compare -> can't
    disprove convergence -> assume converged (fail open)."""
    assert RegimeHMM._fit_converged(_fake_model_with_monitor([100.0])) is True
    assert RegimeHMM._fit_converged(_fake_model_with_monitor([])) is True


def test_fit_converged_true_when_monitor_missing():
    """A model with no monitor_ at all (e.g. a bare mock in another test)
    must not raise -- fail open, same posture as the missing-source
    contract elsewhere in this codebase."""
    assert RegimeHMM._fit_converged(None) is True
    assert RegimeHMM._fit_converged(SimpleNamespace()) is True


def test_predict_populates_convergence_fields_on_real_fit():
    """Integration-level: a real (tiny synthetic) fit populates
    trend_converged/vol_converged on the returned RegimeSnapshot --
    confirms the wiring in predict(), not just the static helper."""
    rng = np.random.RandomState(2)
    prices = 100 + np.cumsum(rng.normal(0, 0.01, size=150))
    model = RegimeHMM(n_components=4)
    model.fit(prices)
    snap = model.predict(prices)
    assert isinstance(snap.trend_converged, bool)
    assert isinstance(snap.vol_converged, bool)


def test_regime_snapshot_defaults_converged_true():
    """Every pre-R4 call site that constructs RegimeSnapshot directly
    (gpu_engine.py, simulator.py, heavy_sweep.py's neutral fallback,
    other tests) doesn't know about convergence -- must default to True
    (fail open) so none of them need updating."""
    from regime_platform.core.regime_engine import RegimeSnapshot
    snap = RegimeSnapshot(
        vol_probs=[0.5, 0.5], trend_probs=[0.5, 0.5],
        vol_state="low", trend_state="bull", regime_label="low_vol_bull",
        confidence=0.5, n_bars_fitted=10,
    )
    assert snap.trend_converged is True
    assert snap.vol_converged is True


def test_temp_weights_empty_dict_is_a_noop():
    """R4's core contract: no diagnostic firing -> {} passed to
    temp_weights() -> byte-identical behaviour to no R4 at all."""
    engine = EVEngine()
    before = engine.get_weights()
    with engine.temp_weights({}):
        during = engine.get_weights()
    after = engine.get_weights()
    assert before == during == after == P_WIN_WEIGHTS_FULL


def test_temp_weights_discounts_p_regime_and_restores():
    engine = EVEngine()
    original = P_WIN_WEIGHTS_FULL["p_regime"]
    with engine.temp_weights({"p_regime": original * 0.5}):
        assert engine.get_weights()["p_regime"] == original * 0.5
        # Other weights untouched during the override.
        assert engine.get_weights()["p_historical"] == P_WIN_WEIGHTS_FULL["p_historical"]
    # Fully restored after exit.
    assert engine.get_weights() == P_WIN_WEIGHTS_FULL


def test_discounted_p_regime_reduces_its_blend_influence():
    """The end-to-end point of R4: a discounted p_regime weight makes a
    wrong-but-confident regime read swing p_win less than it otherwise
    would, relative to the other (converged) sources."""
    engine = EVEngine()
    extreme_bullish_regime = {"bull": 0.99, "bear": 0.01}
    common = dict(
        trend_probs=extreme_bullish_regime,
        direction="short",  # regime disagrees with the trade direction
        p_timesfm=0.5,
        p_historical=0.5,
        tp_bricks=5, sl_bricks=3, brick_size=0.5,
    )
    full_weight = engine.compute_ev(**common)
    with engine.temp_weights({"p_regime": P_WIN_WEIGHTS_FULL["p_regime"] * 0.5}):
        discounted = engine.compute_ev(**common)
    # Regime disagrees with "short" -> pulls p_win down. Discounting its
    # weight must pull p_win back UP toward the other sources' consensus.
    assert discounted.p_win > full_weight.p_win


# ─── 5. trend_transition_matrix_3state (Part 1 Change B) ────────────────


def _make_fitted_stub(
    transmat: np.ndarray,
    label_map: dict[int, str],
    means: dict[int, float] | None = None,
    sigma: float = 1.0,
) -> RegimeHMM:
    """A RegimeHMM instance with a hand-crafted trend model, bypassing a
    real fit — lets the aggregation math be checked against hand-computed
    expected values instead of whatever a real EM fit happens to produce.

    ``means``/``sigma`` feed the Tier 1 flat-gate (2026-08-26, worklog
    20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md) — a component's mean
    must clear ``markov_flat_gate_threshold_stds * sigma`` before keeping
    a directional label. Default: every component at +/-10.0 (10 std at
    the default 0.5-std threshold), i.e. comfortably clears the gate in
    whichever direction its rank-assigned label implies, so tests that
    predate the gate keep exercising the aggregation math unaffected by
    it unless they explicitly pass thinner means to test the gate itself.
    """
    n = len(label_map)
    if means is None:
        means = {
            i: (10.0 if RegimeHMM._TREND_TO_3STATE.get(lbl) == "UP" else -10.0)
            for i, lbl in label_map.items()
        }
    means_arr = np.zeros((n, 3))
    for i in range(n):
        means_arr[i, 2] = means.get(i, 0.0)

    model = RegimeHMM(n_components=n)
    model.fitted = True
    model._trend_model = SimpleNamespace(
        n_components=n, transmat_=transmat, means_=means_arr,
    )
    model._trend_label_map = label_map
    model._trend_scale = (np.zeros(3), np.array([1.0, 1.0, sigma]))
    return model


def test_trend_transition_matrix_3state_raises_before_fit():
    """Mirrors predict()'s own 'Call .fit() first' contract — this is a
    read of the trend model fit() already produced, not independent
    inference."""
    model = RegimeHMM(n_components=4)
    try:
        model.trend_transition_matrix_3state("bull")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_trend_transition_matrix_3state_aggregates_up_down_correctly():
    """Hand-computed expected values for a known 4x4 transmat_, confirming
    the UP/DOWN aggregation (and its renormalization) matches
    _aggregate_transition's algorithm in the legacy model this replaces."""
    # 0=strong_bear (DOWN), 1=bear (DOWN), 2=bull (UP), 3=strong_bull (UP)
    transmat = np.array([
        [0.5, 0.3, 0.1, 0.1],
        [0.2, 0.2, 0.3, 0.3],
        [0.1, 0.1, 0.4, 0.4],
        [0.0, 0.0, 0.5, 0.5],
    ])
    label_map = {0: "strong_bear", 1: "bear", 2: "bull", 3: "strong_bull"}
    model = _make_fitted_stub(transmat, label_map)

    T3, current_state = model.trend_transition_matrix_3state("bull")

    assert current_state == "UP"
    assert T3["DOWN"]["DOWN"] == pytest_approx(0.6)
    assert T3["DOWN"]["UP"] == pytest_approx(0.4)
    assert T3["DOWN"]["FLAT"] == 0.0
    assert T3["UP"]["UP"] == pytest_approx(0.9)
    assert T3["UP"]["DOWN"] == pytest_approx(0.1)
    assert T3["UP"]["FLAT"] == 0.0
    for row in T3.values():
        assert sum(row.values()) == pytest_approx(1.0)


def test_trend_transition_matrix_3state_flat_gets_uniform_fallback():
    """The trend model has no neutral state, so no component ever maps to
    FLAT — its row must fall through to the uniform prior, mirroring the
    legacy model's own 'no state maps to this label' handling rather than
    silently reporting an all-zero (invalid, non-stochastic) row."""
    transmat = np.array([
        [0.6, 0.1, 0.2, 0.1],
        [0.2, 0.5, 0.2, 0.1],
        [0.1, 0.2, 0.5, 0.2],
        [0.1, 0.1, 0.2, 0.6],
    ])
    label_map = {0: "strong_bear", 1: "bear", 2: "bull", 3: "strong_bull"}
    model = _make_fitted_stub(transmat, label_map)

    T3, _ = model.trend_transition_matrix_3state("bear")

    assert T3["FLAT"] == {"UP": pytest_approx(1 / 3), "DOWN": pytest_approx(1 / 3), "FLAT": pytest_approx(1 / 3)}


def test_trend_transition_matrix_3state_current_state_matches_grouping():
    """current_state must resolve through the SAME UP/DOWN grouping the
    matrix itself is built from, for every 4-state trend label — an
    unmapped/unexpected label (e.g. a future label-set change) fails open
    to FLAT rather than raising."""
    transmat = np.full((4, 4), 0.25)
    label_map = {0: "strong_bear", 1: "bear", 2: "bull", 3: "strong_bull"}
    model = _make_fitted_stub(transmat, label_map)

    assert model.trend_transition_matrix_3state("strong_bull")[1] == "UP"
    assert model.trend_transition_matrix_3state("bull")[1] == "UP"
    assert model.trend_transition_matrix_3state("bear")[1] == "DOWN"
    assert model.trend_transition_matrix_3state("strong_bear")[1] == "DOWN"
    assert model.trend_transition_matrix_3state("neutral")[1] == "FLAT"
    assert model.trend_transition_matrix_3state("unknown_label")[1] == "FLAT"


def test_trend_transition_matrix_3state_gates_component_on_wrong_side_of_zero():
    """Tier 1 HMM fix (2026-08-26, worklog
    20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md). Real data (AUDUSD)
    showed a "bear"-labeled component (assigned purely by rank order
    among 4 components) with a mean of +0.54 standard deviations — i.e.
    actually positive, on the wrong side of zero for its label. The old
    rank-only mapping passed this straight through to DOWN. Confirm the
    gate now gives it FLAT instead."""
    transmat = np.array([
        [0.4, 0.2, 0.2, 0.2],
        [0.2, 0.4, 0.2, 0.2],
        [0.2, 0.2, 0.4, 0.2],
        [0.2, 0.2, 0.2, 0.4],
    ])
    label_map = {0: "strong_bear", 1: "bear", 2: "bull", 3: "strong_bull"}
    # sigma=1.0, default threshold 0.5 std -> component 1 ("bear") at
    # +0.54std is ON THE WRONG SIDE of zero and must gate to FLAT;
    # component 0 ("strong_bear") at -0.88std clears the threshold and
    # keeps DOWN, matching the real AUDUSD reading from the worklog.
    means = {0: -0.88, 1: +0.54, 2: +1.11, 3: +1.30}
    model = _make_fitted_stub(transmat, label_map, means=means, sigma=1.0)

    T3, current_state = model.trend_transition_matrix_3state("bear")

    assert current_state == "FLAT"
    for row in T3.values():
        assert sum(row.values()) == pytest_approx(1.0)


def test_trend_transition_matrix_3state_gates_near_zero_component_to_flat():
    """A component whose mean is within the noise band (regardless of
    sign) must gate to FLAT even though its rank-assigned label is
    directional — the "AAPL 'bull' component was actually -2.09std"
    case from the worklog, simplified to a clean near-zero example."""
    transmat = np.full((4, 4), 0.25)
    label_map = {0: "strong_bear", 1: "bear", 2: "bull", 3: "strong_bull"}
    # threshold = 0.5 * sigma(2.0) = 1.0 — component 2 ("bull") at +0.3
    # is well within the noise band and must gate to FLAT.
    means = {0: -10.0, 1: -5.0, 2: +0.3, 3: +10.0}
    model = _make_fitted_stub(transmat, label_map, means=means, sigma=2.0)

    assert model.trend_transition_matrix_3state("bull")[1] == "FLAT"
    # strong_bull at +10.0 (5 std) clears the threshold comfortably.
    assert model.trend_transition_matrix_3state("strong_bull")[1] == "UP"


def test_trend_transition_matrix_3state_on_real_fit_returns_valid_matrix():
    """Integration-level: a real (tiny synthetic) fit + predict, then the
    new derivation — confirms the wiring against real hmmlearn output,
    not just the hand-crafted stub above."""
    rng = np.random.RandomState(3)
    prices = 100 + np.cumsum(rng.normal(0, 0.01, size=150))
    model = RegimeHMM(n_components=4)
    model.fit(prices)
    regime = model.predict(prices)

    T3, current_state = model.trend_transition_matrix_3state(regime.trend_state)

    assert current_state in ("UP", "DOWN", "FLAT")
    for row in T3.values():
        assert sum(row.values()) == pytest_approx(1.0)
        for p in row.values():
            assert 0.0 <= p <= 1.0
