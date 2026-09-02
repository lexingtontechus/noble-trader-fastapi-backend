"""
Tests for RegimeHMM's EM warm-start (regime_platform/core/regime_engine.py),
worklog/20260901_REGIME_LABEL_STABILITY_SCOPE.md — root-cause fix for
regime_label flapping tick-to-tick.

``_rebase_params_to_scale`` is a pure function (no I/O) — tested with
hand-computed values. ``_seed_model`` needs a real ``hmm.GaussianHMM``
object to verify attribute assignment (mirrors the module's own
``_sanitize_model``, which similarly can't be tested as pure Python).
``vol_raw_params``/``trend_raw_params`` and a two-fit warm-start sequence
are tested against real ``.fit()`` calls, same synthetic-price-series
pattern already used in ``test_regime_stability.py``.
"""

import numpy as np
from hmmlearn import hmm
from pytest import approx as pytest_approx

from regime_platform.core.regime_engine import RegimeHMM

# ─── _rebase_params_to_scale ──────────────────────────────────────────────


def test_rebase_params_hand_computed():
    means_original = np.array([[10.0, 20.0], [30.0, 40.0]])
    covars_original = np.array([[4.0, 9.0], [16.0, 25.0]])
    mu = np.array([5.0, 10.0])
    sigma = np.array([2.0, 3.0])

    means_std, covars_std = RegimeHMM._rebase_params_to_scale(
        means_original, covars_original, (mu, sigma),
    )

    # mean_std = (mean_original - mu) / sigma
    expected_means = np.array([[(10 - 5) / 2, (20 - 10) / 3], [(30 - 5) / 2, (40 - 10) / 3]])
    assert means_std == pytest_approx(expected_means)
    # covar_std = covar_original / sigma**2
    expected_covars = np.array([[4 / 4, 9 / 9], [16 / 4, 25 / 9]])
    assert covars_std == pytest_approx(expected_covars)


def test_rebase_params_identity_scale_is_noop():
    means_original = np.array([[1.0, 2.0], [3.0, 4.0]])
    covars_original = np.array([[1.0, 1.0], [1.0, 1.0]])
    mu = np.array([0.0, 0.0])
    sigma = np.array([1.0, 1.0])

    means_std, covars_std = RegimeHMM._rebase_params_to_scale(
        means_original, covars_original, (mu, sigma),
    )
    assert means_std == pytest_approx(means_original)
    assert covars_std == pytest_approx(covars_original)


# ─── _seed_model ───────────────────────────────────────────────────────────


def _real_hmm(n_components=3, n_features=2):
    return hmm.GaussianHMM(
        n_components=n_components, covariance_type="diag", random_state=42, n_iter=100,
    )


def test_seed_model_sets_all_four_params_and_disables_auto_init():
    model = _real_hmm(n_components=2, n_features=2)
    prev_params = {
        "means": np.array([[1.0, 2.0], [3.0, 4.0]]),
        "covars": np.array([[1.0, 1.0], [1.0, 1.0]]),
        "transmat": np.array([[0.9, 0.1], [0.2, 0.8]]),
        "startprob": np.array([0.6, 0.4]),
    }
    scale = (np.array([0.0, 0.0]), np.array([1.0, 1.0]))  # identity — no rebasing needed

    RegimeHMM._seed_model(model, prev_params, scale)

    assert model.means_ == pytest_approx(prev_params["means"])
    assert model._covars_ == pytest_approx(prev_params["covars"])
    assert model.transmat_ == pytest_approx(prev_params["transmat"])
    assert model.startprob_ == pytest_approx(prev_params["startprob"])
    assert model.init_params == ""


def test_seed_model_noops_on_component_count_mismatch():
    model = _real_hmm(n_components=3, n_features=2)
    prev_params = {
        "means": np.array([[1.0, 2.0], [3.0, 4.0]]),  # only 2 components
        "covars": np.array([[1.0, 1.0], [1.0, 1.0]]),
        "transmat": np.array([[0.9, 0.1], [0.2, 0.8]]),
        "startprob": np.array([0.6, 0.4]),
    }
    scale = (np.array([0.0, 0.0]), np.array([1.0, 1.0]))
    default_init_params = model.init_params

    RegimeHMM._seed_model(model, prev_params, scale)

    # Should fall back to hmmlearn's normal auto-init, unchanged.
    assert model.init_params == default_init_params


def test_seed_model_noops_on_missing_key():
    model = _real_hmm(n_components=2, n_features=2)
    prev_params = {
        "means": np.array([[1.0, 2.0], [3.0, 4.0]]),
        "covars": np.array([[1.0, 1.0], [1.0, 1.0]]),
        "transmat": np.array([[0.9, 0.1], [0.2, 0.8]]),
        # startprob missing
    }
    scale = (np.array([0.0, 0.0]), np.array([1.0, 1.0]))
    default_init_params = model.init_params

    RegimeHMM._seed_model(model, prev_params, scale)

    assert model.init_params == default_init_params


def test_seed_model_actually_used_for_a_real_fit():
    # A real fit that converges with warm-started params, on data
    # perturbed from what those params were fitted to — mirrors the
    # rolling-window-shift scenario this fix targets.
    rng = np.random.RandomState(0)
    x1 = rng.randn(100, 2)
    seed_model = _real_hmm(n_components=3, n_features=2)
    seed_model.fit(x1)

    prev_params = {
        "means": seed_model.means_.copy(),
        "covars": seed_model._covars_.copy(),
        "transmat": seed_model.transmat_.copy(),
        "startprob": seed_model.startprob_.copy(),
    }
    scale = (np.array([0.0, 0.0]), np.array([1.0, 1.0]))

    x2 = np.vstack([x1[1:], rng.randn(1, 2)])  # rolling window: drop 1, add 1
    warm_model = _real_hmm(n_components=3, n_features=2)
    RegimeHMM._seed_model(warm_model, prev_params, scale)
    warm_model.fit(x2)

    assert np.all(np.isfinite(warm_model.means_))
    assert np.all(np.isfinite(warm_model._covars_))
    assert warm_model.monitor_.converged


# ─── vol_raw_params / trend_raw_params ─────────────────────────────────────


def _synthetic_prices(seed=3, n=150):
    rng = np.random.RandomState(seed)
    return 100 + np.cumsum(rng.normal(0, 0.01, size=n))


def test_raw_params_none_before_fit():
    model = RegimeHMM(n_components=4)
    assert model.vol_raw_params is None
    assert model.trend_raw_params is None


def test_raw_params_populated_and_correctly_shaped_after_fit():
    model = RegimeHMM(n_components=4)
    model.fit(_synthetic_prices())

    vol_params = model.vol_raw_params
    trend_params = model.trend_raw_params
    assert vol_params is not None and trend_params is not None
    # Both feature sets happen to be 3-column (_vol_features/_trend_features
    # each engineer 3 columns, not the same 3 columns, but the same count).
    for params, n_features in ((vol_params, 3), (trend_params, 3)):
        assert params["means"].shape == (4, n_features)
        assert params["covars"].shape == (4, n_features)
        assert params["transmat"].shape == (4, 4)
        assert params["startprob"].shape == (4,)
        assert np.all(np.isfinite(params["means"]))
        assert np.all(np.isfinite(params["covars"]))


# ─── End-to-end: two fits in sequence, second warm-started from the first ──


def test_two_tick_warm_start_sequence_does_not_crash_and_stays_finite():
    prices1 = _synthetic_prices(seed=7, n=150)
    model1 = RegimeHMM(n_components=4)
    model1.fit(prices1)

    prices2 = np.append(prices1[1:], prices1[-1] * 1.0001)  # rolling window shift
    model2 = RegimeHMM(n_components=4)
    model2.fit(
        prices2,
        prev_vol_params=model1.vol_raw_params,
        prev_trend_params=model1.trend_raw_params,
    )

    regime = model2.predict(prices2)
    assert regime.regime_label is not None
    assert np.all(np.isfinite(model2._trend_model.means_))
    assert np.all(np.isfinite(model2._trend_model._covars_))


def test_fit_without_prev_params_behaves_as_before():
    # Regression safety: omitting prev_vol_params/prev_trend_params
    # (the new kwargs' default) must be indistinguishable from a fit on
    # a codebase that never had this feature.
    model = RegimeHMM(n_components=4)
    model.fit(_synthetic_prices())
    assert model.fitted
    assert model._trend_model.init_params == "stmc"


# ─── _trend_separation_healthy / trend_raw_params gating ──────────────────
# Rescope of the AAPL regression found in the real-data comparison (worklog
# 20260901_REGIME_LABEL_STABILITY_SCOPE.md "Rescope"): a fit whose trend
# components have collapsed together shouldn't be handed forward as the
# next tick's warm-start basis.


def test_trend_separation_healthy_true_for_well_separated_fit():
    # A clear uptrend gives the trend components real separation on
    # feature index 2 (30-bar cumulative return).
    rng = np.random.RandomState(11)
    n = 150
    prices = 100 + np.cumsum(rng.normal(0.05, 0.01, size=n))
    model = RegimeHMM(n_components=4)
    model.fit(prices)
    assert model._trend_separation_healthy() is True
    assert model.trend_raw_params is not None


def test_trend_separation_healthy_false_when_forced_degenerate():
    # Directly force the collapsed-components case (rather than hoping
    # to find real data that reproduces it) by fitting normally, then
    # collapsing all 4 means onto one value before re-checking — isolates
    # the health check itself from the harder question of what market
    # data produces this.
    model = RegimeHMM(n_components=4)
    model.fit(_synthetic_prices())
    collapsed = np.array(model._trend_model.means_)
    collapsed[:, 2] = collapsed[0, 2]  # all components identical on feature 2
    model._trend_model.means_ = collapsed
    assert model._trend_separation_healthy() is False

    # trend_raw_params reads the cached flag set at fit() time, not a live
    # recomputation on every access (cheap, computed once per tick) — a
    # real caller only ever sees this by fitting on genuinely degenerate
    # data, not by mutating an already-fitted model's internals.
    model._trend_params_healthy = model._trend_separation_healthy()
    assert model.trend_raw_params is None


def test_vol_raw_params_not_gated_by_trend_health():
    # The regression was specific to the trend feature — vol_raw_params
    # is deliberately left ungated (see the property's own docstring).
    model = RegimeHMM(n_components=4)
    model.fit(_synthetic_prices())
    model._trend_params_healthy = False
    assert model.trend_raw_params is None
    assert model.vol_raw_params is not None
