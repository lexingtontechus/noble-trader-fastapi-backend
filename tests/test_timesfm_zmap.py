"""TimesFM z-map unit tests (worklog 20260917_TIMESFM_ZMAP_FIX.md)."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from regime_platform.sweep_orchestrator import timesfm_zmap as zm


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    zm.reset_connection()
    yield tmp_path / "tf.sqlite"
    zm.reset_connection()


class TestNormalCdf:
    def test_zero_is_half(self):
        assert zm.normal_cdf(0.0) == pytest.approx(0.5)

    def test_symmetric(self):
        assert zm.normal_cdf(1.645) + zm.normal_cdf(-1.645) == pytest.approx(1.0, abs=1e-9)

    def test_one_sigma(self):
        assert zm.normal_cdf(1.0) == pytest.approx(0.8413, abs=1e-3)


class TestZmap:
    def test_insufficient_samples_returns_none(self, _fresh_db):
        db = _fresh_db
        for i in range(10):
            zm.record_rel_change("EURUSD", 0.001, db_path=db)
        assert zm.zmap_probability("EURUSD", 0.01, db_path=db) is None

    def test_zmap_moves_with_std(self, _fresh_db):
        db = _fresh_db
        # mixed historical noise so σ is non-trivial
        for i in range(40):
            zm.record_rel_change("XAGUSD", 0.0002 if i % 2 == 0 else -0.0002, db_path=db)
        p_small = zm.zmap_probability("XAGUSD", 0.0000, db_path=db)
        p_large = zm.zmap_probability("XAGUSD", 0.005, db_path=db)
        assert p_small is not None and p_large is not None
        assert p_large > 0.9
        assert 0.4 < p_small < 0.7

    def test_negative_rel_change_below_half(self, _fresh_db):
        db = _fresh_db
        for _ in range(40):
            zm.record_rel_change("AAPL", 0.0002, db_path=db)
        p = zm.zmap_probability("AAPL", -0.01, db_path=db)
        assert p is not None
        assert p < 0.1

    def test_sigma_floor(self, _fresh_db):
        db = _fresh_db
        for _ in range(40):
            zm.record_rel_change("SPY", 0.0, db_path=db)
        p = zm.zmap_probability("SPY", 0.001, db_path=db, sigma_floor=1e-4)
        assert p is not None
        # z = 0.001/1e-4 = 10 → capped at 5 → Φ(5) ≈ 1
        assert p > 0.99


class TestFeatureFlag:
    def test_default_mode(self):
        from regime_platform.core.feature_flags import get_timesfm_map_mode
        assert get_timesfm_map_mode() in ("sigmoid", "zmap_shadow", "zmap_live")
