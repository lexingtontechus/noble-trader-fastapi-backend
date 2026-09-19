"""P0/P1 tests — evidence promote, deadline pool, heavy publish gate math."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from regime_platform.core.feature_flags import (
    get_atr_anchored_evidence_mode,
    get_heavy_publish_geometry_gate,
)
from regime_platform.sweep_orchestrator.tiered_atr import (
    compose_deadline_candidate_pool,
    should_promote_atr_candidates,
)


class TestEvidencePromote:
    def test_promote_when_4plus_to_tighter_low_lag(self):
        assert should_promote_atr_candidates(
            selection_changed=True,
            baseline_bucket="4+",
            widened_bucket="0.5-1",
            pct_expired_by_bucket={"4+": 0.8, "0.5-1": 0.05},
        ) is True

    def test_no_promote_unchanged(self):
        assert should_promote_atr_candidates(
            selection_changed=False,
            baseline_bucket="4+",
            widened_bucket="0.5-1",
            pct_expired_by_bucket={"4+": 0.8, "0.5-1": 0.05},
        ) is False

    def test_no_promote_if_widened_worse(self):
        assert should_promote_atr_candidates(
            selection_changed=True,
            baseline_bucket="2-4",
            widened_bucket="4+",
            pct_expired_by_bucket={"2-4": 0.1, "4+": 0.9},
        ) is False

    def test_missing_metrics_fail_closed(self):
        assert should_promote_atr_candidates(
            selection_changed=True,
            baseline_bucket="4+",
            widened_bucket="2-4",
            pct_expired_by_bucket={},
        ) is False


class TestDeadlinePool:
    def _c(self, brick):
        return SimpleNamespace(brick_size=brick, sl_bricks=3, tp_bricks=6)

    def test_live_mode_full_grid(self):
        grid = [self._c(1.0), self._c(0.1)]
        out = compose_deadline_candidate_pool(grid, {1.0}, "live", promote=False)
        assert len(out) == 2

    def test_shadow_no_promote_original_only(self):
        grid = [self._c(1.0), self._c(0.1)]
        out = compose_deadline_candidate_pool(grid, {1.0}, "shadow", promote=False)
        assert len(out) == 1
        assert out[0].brick_size == 1.0

    def test_shadow_with_promote_full_grid(self):
        grid = [self._c(1.0), self._c(0.1)]
        out = compose_deadline_candidate_pool(grid, {1.0}, "shadow", promote=True)
        assert len(out) == 2


class TestFlags:
    def test_defaults(self):
        assert get_atr_anchored_evidence_mode() in ("off", "shadow", "live")
        assert get_heavy_publish_geometry_gate() in ("off", "shadow", "live")
        assert get_heavy_publish_geometry_gate() == "live"


class TestHeavyPayloadRaw:
    def test_build_payload_accepts_raw_and_geometry(self):
        from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
        p = _build_signal_sim_payload(
            symbol="US30USD", venue="OANDA", ts="2026-09-19T00:00:00Z",
            signal="buy", entry_price=50000.0, stop_loss=49800.0, take_profit=50400.0,
            brick_size=50.0, sl_bricks=4, tp_bricks=8,
            p_win=0.55, ev=1.0, regime="med_high_vol_bull", regime_conf=0.8,
            source="sweep-heavy", qualified=True,
            predicted_p_win_raw=0.72,
            p_win_dir=0.72,
            tp_distance_atr=3.2,
            geometry_risk_hat=0.2,
            long_lag_risk=False,
        )
        assert p["predicted_p_win_raw"] == 0.72
        assert p["p_win_dir"] == 0.72
        assert p["tp_distance_atr"] == 3.2
        assert p["source"] == "sweep-heavy"
