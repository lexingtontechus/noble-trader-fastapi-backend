"""TimesFM rank features (P1.2)."""

from __future__ import annotations

from regime_platform.sweep_orchestrator import kairos_rank as R


class TestTimesfmRankFeatures:
    def test_side_agreement(self):
        import pytest

        f = R.timesfm_rank_features(0.70, "buy")
        assert f["timesfm_dir"] == pytest.approx(0.20)
        assert f["timesfm_conf"] == pytest.approx(0.20)
        assert f["timesfm_side"] == pytest.approx(0.20)
        g = R.timesfm_rank_features(0.70, "sell")
        assert g["timesfm_side"] == pytest.approx(-0.20)

    def test_gap_and_quantiles(self):
        import pytest

        f = R.timesfm_rank_features(
            0.60, "buy", p_win=0.55, q_lo=98.0, q_mid=100.0, q_hi=102.0, last_price=100.0
        )
        assert f["timesfm_gap"] == pytest.approx(0.05)
        assert f["q_spread"] == pytest.approx(0.04)
        assert f["q_pos"] == pytest.approx(0.5)

    def test_missing_p(self):
        f = R.timesfm_rank_features(None, "buy")
        assert f["timesfm_dir"] is None
        assert f["timesfm_side"] is None

    def test_no_last_price_skips_q(self):
        import pytest

        f = R.timesfm_rank_features(0.55, None, q_lo=1, q_hi=2)
        assert f["q_spread"] is None
        assert f["timesfm_conf"] == pytest.approx(0.05)
