"""
Batch C — p_win_bracket unit tests (Phase 3).
"""

from __future__ import annotations

import pytest

from regime_platform.sweep_orchestrator.p_win_bracket import (
    apply_bracket_to_ev,
    expire_prob,
    BETA_ALPHA,
    BETA_BETA,
    beta_shrunk_win_rate,
    compute_bracket_probs,
    would_bracket_gate_reject,
)


class TestBetaShrink:
    def test_prior_mean_is_half(self):
        assert beta_shrunk_win_rate(0, 0) == pytest.approx(0.5)

    def test_shrinks_thin_sample(self):
        # 1 win 0 losses → (1+2)/(1+0+4) = 0.6, not 1.0
        assert beta_shrunk_win_rate(1, 0) == pytest.approx(0.6)

    def test_large_n_approaches_empirical(self):
        p = beta_shrunk_win_rate(80, 20)
        assert abs(p - 0.8) < 0.02


class TestComputeBracketProbs:
    def test_full_path(self):
        r = compute_bracket_probs(
            entry_price=2500.0, take_profit=2520.0, atr=10.0,
            regime_label="med_low_vol_bull",
            n=20, n_stored=20, pct_hit_tp=0.25, pct_hit_sl=0.35, pct_expired=0.40,
        )
        assert r.bucket == "2-4"  # 20/10 = 2.0 → "2-4" (lo inclusive)
        assert r.vol_tier == "med_low_vol"
        assert r.p_resolve == pytest.approx(0.60)
        # wins=5, losses=7 → (5+2)/(5+7+4)=7/16=0.4375
        assert r.p_win_resolve == pytest.approx(0.4375)
        assert r.p_win_bracket == pytest.approx(0.60 * 0.4375)
        assert r.reason is None

    def test_missing_atr(self):
        r = compute_bracket_probs(
            entry_price=2500.0, take_profit=2520.0, atr=None,
            regime_label="med_low_vol_bull",
            n=10, n_stored=10, pct_hit_tp=0.5, pct_hit_sl=0.5, pct_expired=0.0,
        )
        assert r.p_win_bracket is None
        assert r.reason == "missing_atr_or_prices"

    def test_legacy_stored_fallback_when_n_stored_thin(self):
        """Bootstrap (2026-09-16): n_stored thin but n large + row stored → use n."""
        r = compute_bracket_probs(
            entry_price=2500.0, take_profit=2510.0, atr=10.0,
            regime_label="med_low_vol_bull",
            n=50, n_stored=1, pct_hit_tp=0.5, pct_hit_sl=0.3, pct_expired=0.2,
        )
        assert r.p_win_bracket is not None
        assert r.atr_source == "legacy_stored"

    def test_insufficient_n_without_legacy_fallback(self):
        r = compute_bracket_probs(
            entry_price=2500.0, take_profit=2510.0, atr=10.0,
            regime_label="med_low_vol_bull",
            n=2, n_stored=1, pct_hit_tp=0.5, pct_hit_sl=0.3, pct_expired=0.2,
            allow_legacy_stored=False,
        )
        assert r.p_win_bracket is None
        assert r.reason == "insufficient_stored_n"

    def test_legacy_fallback_never_for_proxy_rows(self):
        r = compute_bracket_probs(
            entry_price=2500.0, take_profit=2510.0, atr=10.0,
            regime_label="med_low_vol_bull",
            n=50, n_stored=0, pct_hit_tp=0.5, pct_hit_sl=0.3, pct_expired=0.2,
            row_atr_source="proxy_current",
        )
        assert r.p_win_bracket is None
        assert r.reason == "insufficient_stored_n"

    def test_bucket_0_5_1(self):
        r = compute_bracket_probs(
            entry_price=100.0, take_profit=100.75, atr=1.0,
            regime_label="high_vol_bull",
            n=10, n_stored=10, pct_hit_tp=0.4, pct_hit_sl=0.3, pct_expired=0.3,
        )
        assert r.bucket == "0.5-1"
        assert r.vol_tier == "high_vol"


class TestGate:
    def test_none_is_fail_open(self):
        assert would_bracket_gate_reject(None) is None


def test_expire_prob():
    assert expire_prob(None) == 0.0
    assert expire_prob(0.7) == pytest.approx(0.3)
    assert expire_prob(0.0) == pytest.approx(1.0)
    assert expire_prob(1.0) == pytest.approx(0.0)


def test_apply_bracket_to_ev_mutates_and_fails_open():
    from regime_platform.services.statistics.ev_engine import EVEngine

    class FakeEV:
        p_win = 0.70
        profit = 2.0
        stake = 1.0
        ev = 0.4
        ev_per_dollar = 0.4
        rr = 2.0
        allowed = True
        aggression = "low"
        ev_scale = 0.8

    engine = EVEngine(min_ev=0.0, min_rr=1.5)
    obj = FakeEV()
    assert apply_bracket_to_ev(
        obj, p_win_bracket=0.35, p_resolve=0.5, engine=engine,
    ) is True
    assert obj.p_win == 0.35
    # EV = 0.35*2 − (1-0.35-0.5)*1 = 0.7 − 0.15 = 0.55
    assert obj.ev == pytest.approx(0.55)

    untouched = FakeEV()
    before = untouched.p_win
    assert apply_bracket_to_ev(
        untouched, p_win_bracket=None, p_resolve=None, engine=engine,
    ) is False
    assert untouched.p_win == before


class TestGateThresholds:
    def test_below_threshold_rejects(self):
        # min_p_win_bracket is 0.20 (was 0.45 — wrong scale for p_resolve×p_win|resolve).
        assert would_bracket_gate_reject(0.15) is True
        assert would_bracket_gate_reject(0.30) is False

    def test_above_threshold_keeps(self):
        assert would_bracket_gate_reject(0.60) is False


class TestPayloadWiring:
    def test_sim_payload_includes_bracket_fields(self):
        from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload

        p = _build_signal_sim_payload(
            symbol="XAUUSD", venue="OANDA", ts="2026-09-16T12:00:00Z",
            signal="buy", entry_price=2500.0, stop_loss=2470.0, take_profit=2560.0,
            brick_size=5.0, sl_bricks=6, tp_bricks=12,
            p_win=0.62, ev=0.01, regime="med_low_vol_bull", regime_conf=0.8,
            source="light", qualified=True,
            p_win_dir=0.62, p_win_bracket=0.41, p_resolve=0.7,
            p_win_resolve=0.59, bracket_bucket="2-4",
            bracket_vol_tier="med_low_vol", bracket_gate_shadow=True,
        )
        assert p["p_win_dir"] == 0.62
        assert p["p_win_bracket"] == 0.41
        assert p["bracket_gate_shadow"] is True
        assert p["bracket_bucket"] == "2-4"
