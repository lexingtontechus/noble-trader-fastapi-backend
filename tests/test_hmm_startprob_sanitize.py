"""E2E: HMM startprob_ NaN sanitization (2026-10-12, worklog
20261012_HMM_STARTPROB_NAN.md).

A NaN startprob_ made every hmmlearn predict throw (startprob_ must sum to 1),
silently forcing FLAT regime 18×/24h across 5 symbols. The persistence
round-trip assigned startprob_/transmat_ raw. These tests pin the cleaners:
NaN/negative/unnormalized round-trip cleanly and predict no longer throws;
clean input unchanged; all-NaN falls back to uniform.
"""
from __future__ import annotations

import numpy as np
import pytest

from regime_platform.services.hmm.model import _clean_startprob, _clean_transmat


def test_nan_startprob_becomes_valid_distribution():
    sp = _clean_startprob(np.array([np.nan, 0.2, np.nan, 0.3]))
    assert np.isfinite(sp).all()
    assert sp.sum() == pytest.approx(1.0)
    assert (sp >= 0).all()


def test_negative_and_inf_clipped():
    sp = _clean_startprob(np.array([-0.5, np.inf, 0.3, 0.2]))
    assert np.isfinite(sp).all()
    assert (sp >= 0).all()
    assert sp.sum() == pytest.approx(1.0)


def test_unnormalized_renormalized():
    sp = _clean_startprob(np.array([2.0, 2.0, 2.0, 2.0]))
    assert sp.sum() == pytest.approx(1.0)
    assert np.allclose(sp, 0.25)


def test_all_nan_falls_back_to_uniform():
    sp = _clean_startprob(np.array([np.nan, np.nan, np.nan]))
    assert sp.sum() == pytest.approx(1.0)
    assert np.allclose(sp, 1.0 / 3.0)


def test_all_zero_falls_back_to_uniform():
    sp = _clean_startprob(np.array([0.0, 0.0, 0.0]))
    assert sp.sum() == pytest.approx(1.0)


def test_clean_startprob_unchanged():
    sp = _clean_startprob(np.array([0.1, 0.2, 0.3, 0.4]))
    assert np.allclose(sp, [0.1, 0.2, 0.3, 0.4])


def test_transmat_rows_normalized():
    tm = _clean_transmat(np.array([[1.0, 2.0, 1.0], [np.nan, 0.0, 0.0], [0.5, 0.5, 0.5]]))
    row_sums = tm.sum(axis=1)
    assert np.allclose(row_sums, 1.0)
    assert np.isfinite(tm).all()


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("hmmlearn"),
    reason="hmmlearn not installed",
)
def test_predict_no_longer_throws_after_clean():
    """The end-to-end contract: a model loaded with a cleaned startprob_ can
    predict without the 'startprob_ must sum to 1' ValueError."""
    from hmmlearn.hmm import GaussianHMM
    model = GaussianHMM(n_components=4, covariance_type="diag", n_iter=10, random_state=42)
    model.means_ = np.zeros((4, 3))
    model.covars_ = np.ones((4, 3))
    model.transmat_ = _clean_transmat(np.full((4, 4), np.nan))
    model.startprob_ = _clean_startprob(np.array([np.nan, np.nan, np.nan, np.nan]))
    # would have raised before the fix
    state = model.predict(np.zeros((1, 3)))
    assert state is not None
