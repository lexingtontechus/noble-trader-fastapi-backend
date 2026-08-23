"""Tests for the p_historical EV-blend source (R3, 2026-08-23 —
worklog/20260823_AAPL_US500_EXCEPTION_ROOT_CAUSE.md).

Covers:
  1. ``calibration_enforcement.fetch_historical_p_win`` — reads
     ``v_eod_calibration_bias_latest``, applies the Bayesian shrink, and
     respects the view's own INSUFFICIENT_DATA gate.
  2. ``EVEngine.compute_p_historical`` — the pass-through/clamp contract.
  3. ``EVEngine.compute_ev`` — p_historical participates in the blend and
     renormalizes out cleanly when unavailable (replacing the old
     always-None p_pattern source).
"""
import asyncio
from unittest.mock import patch

import pytest

from regime_platform.services.statistics.ev_engine import EVEngine, P_WIN_WEIGHTS_FULL
from regime_platform.sweep_orchestrator.calibration_enforcement import (
    fetch_historical_p_win,
)


# ─── 1. fetch_historical_p_win ──────────────────────────────────────────


class FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        return FakeResp(self.rows)


def _run_fetch(symbol="AAPL", rows=None):
    client = FakeClient(rows if rows is not None else [])
    with (
        patch(
            "regime_platform.sweep_orchestrator.calibration_enforcement._url",
            return_value="https://x.supabase.co/rest/v1/v_eod_calibration_bias_latest",
        ),
        patch(
            "regime_platform.sweep_orchestrator.calibration_enforcement._headers",
            return_value={"apikey": "k", "Authorization": "Bearer k"},
        ),
        patch("httpx.AsyncClient", return_value=client),
        patch("regime_platform.core.settings.settings.SUPABASE_URL", "https://x.supabase.co"),
        patch("regime_platform.core.settings.settings.SUPABASE_KEY", "k"),
    ):
        return asyncio.run(fetch_historical_p_win(symbol))


def test_shrinks_realized_win_rate_toward_half():
    """n=90, win_rate=0.10 (like US500's real numbers) -> Bayesian-shrunk
    toward 0.5, not the raw (extreme) realized rate."""
    result = _run_fetch(rows=[{"n": 90, "realized_win_rate": 0.10, "status_raw": "OVERCONFIDENT"}])
    assert result is not None
    # (0.10*90 + 0.5*10) / 100 = 0.14 with the default prior_count=10.
    assert abs(result - 0.14) < 1e-6
    assert result > 0.10  # shrunk toward 0.5, not the raw extreme


def test_insufficient_data_returns_none():
    """The view's own min-sample gate (status_raw=INSUFFICIENT_DATA) is
    respected, not re-derived — a new/thin-history symbol must drop out
    of the blend rather than contribute a noisy small-n estimate."""
    result = _run_fetch(rows=[{"n": 4, "realized_win_rate": 0.25, "status_raw": "INSUFFICIENT_DATA"}])
    assert result is None


def test_no_row_returns_none():
    """A symbol with no calibration history at all (empty result set)."""
    assert _run_fetch(rows=[]) is None


def test_unconfigured_supabase_returns_none():
    with (
        patch("regime_platform.core.settings.settings.SUPABASE_URL", ""),
        patch("regime_platform.core.settings.settings.SUPABASE_KEY", ""),
    ):
        assert asyncio.run(fetch_historical_p_win("AAPL")) is None


def test_network_error_returns_none_not_raise():
    class ExplodingClient:
        async def __aenter__(self):
            raise ConnectionError("boom")

        async def __aexit__(self, *exc):
            return False

    with (
        patch(
            "regime_platform.sweep_orchestrator.calibration_enforcement._url",
            return_value="https://x.supabase.co/rest/v1/v_eod_calibration_bias_latest",
        ),
        patch(
            "regime_platform.sweep_orchestrator.calibration_enforcement._headers",
            return_value={},
        ),
        patch("httpx.AsyncClient", return_value=ExplodingClient()),
        patch("regime_platform.core.settings.settings.SUPABASE_URL", "https://x.supabase.co"),
        patch("regime_platform.core.settings.settings.SUPABASE_KEY", "k"),
    ):
        assert asyncio.run(fetch_historical_p_win("AAPL")) is None


# ─── 2. EVEngine.compute_p_historical (pass-through contract) ──────────


def test_compute_p_historical_passthrough_and_clamp():
    assert EVEngine.compute_p_historical(None, "long") is None
    assert EVEngine.compute_p_historical(0.6, "long") == 0.6
    assert EVEngine.compute_p_historical(1.5, "long") == 1.0  # clamped
    assert EVEngine.compute_p_historical(-0.2, "long") == 0.0  # clamped


# ─── 3. compute_ev() blend integration ─────────────────────────────────


def test_weights_full_has_historical_not_pattern():
    """R2: p_pattern is gone; R3: p_historical replaces it at the same
    30% base weight."""
    assert "p_pattern" not in P_WIN_WEIGHTS_FULL
    assert P_WIN_WEIGHTS_FULL["p_historical"] == 0.30
    assert abs(sum(P_WIN_WEIGHTS_FULL.values()) - 1.0) < 1e-9


def test_p_historical_none_renormalizes_like_old_p_pattern():
    """Omitting p_historical (None, e.g. a brand-new symbol) must drop it
    from the blend and renormalize over the remaining 3 sources — the
    exact contract p_pattern always relied on, since the server always
    passed it as None."""
    engine = EVEngine()
    result = engine.compute_ev(
        trend_probs={"bull": 0.6, "bear": 0.2, "strong_bull": 0.1, "strong_bear": 0.1},
        direction="long",
        p_historical=None,
        p_timesfm=0.55,
        tp_bricks=5, sl_bricks=3, brick_size=0.5,
    )
    assert "p_historical" not in result.sources_used
    assert abs(sum(result.weights_used.values()) - 1.0) < 1e-9


def test_p_historical_present_participates_in_blend():
    """A high p_historical (symbol has a strong realized track record)
    pulls p_win up relative to the same call without it."""
    engine = EVEngine()
    common = dict(
        trend_probs={"bull": 0.5, "bear": 0.5},
        direction="long",
        p_timesfm=0.5,
        tp_bricks=5, sl_bricks=3, brick_size=0.5,
    )
    without = engine.compute_ev(p_historical=None, **common)
    with_high = engine.compute_ev(p_historical=0.9, **common)
    assert "p_historical" in with_high.sources_used
    assert with_high.p_win > without.p_win


def test_unknown_weight_key_error_mentions_p_historical():
    """set_weights() must reject the old p_pattern key with a message
    pointing at its replacement, not silently accept it."""
    engine = EVEngine()
    with pytest.raises(ValueError, match="p_historical"):
        engine.set_weights({"p_pattern": 0.5})
