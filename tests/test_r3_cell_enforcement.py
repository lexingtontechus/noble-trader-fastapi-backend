"""R3 cell-level calibration enforcement (migration 162).

Hierarchy: cell (n ≥ CELL_MIN_N) → parent symbol → system (no-op).
Underconfidence never Kelly-cuts; overconfidence does.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.calibration_enforcement import (
    CELL_MIN_N,
    apply_calibration_adjustment,
    normalize_side,
    resolve_bias_evidence,
)


def test_normalize_side_maps_long_short():
    assert normalize_side("long") == "buy"
    assert normalize_side("BUY") == "buy"
    assert normalize_side("short") == "sell"
    assert normalize_side("Sell") == "sell"
    assert normalize_side(None) is None
    assert normalize_side("neutral") is None


def test_cell_min_n_is_ten():
    assert CELL_MIN_N == 10


def test_resolve_prefers_sufficient_cell():
    cell = {
        "symbol": "EURUSD", "side": "buy", "vol_tier": "low_vol",
        "distance_atr_bucket": "2-4", "n": 25, "wins": 5, "losses": 20,
        "avg_p_win": 0.70, "win_rate": 0.20, "bias": 0.50, "bias_shrunk": 0.45,
    }
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=cell),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value={"action": "bayesian_penalty", "bias": 0.3, "status": "OVERCONFIDENT", "n_records": 40}),
        ),
    ):
        ev = asyncio.run(resolve_bias_evidence(
            "EURUSD", side="long", vol_tier="low_vol", distance_atr_bucket="2-4",
        ))
    assert ev["source"] == "cell"
    assert ev["status"] == "OVERCONFIDENT"
    assert ev["bias"] == 0.45


def test_resolve_falls_back_to_symbol_when_cell_thin():
    thin = {"n": 3, "bias_shrunk": 0.4, "bias": 0.5}
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=thin),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value={
                "action": "bayesian_penalty", "bias": 0.2, "status": "OVERCONFIDENT",
                "n_records": 30, "computed_at": "2026-09-22T12:00:00+00:00",
            }),
        ),
    ):
        ev = asyncio.run(resolve_bias_evidence(
            "EURUSD", side="buy", vol_tier="low_vol", distance_atr_bucket="2-4",
        ))
    assert ev["source"] == "symbol"


def test_resolve_system_when_no_evidence():
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value=None),
        ),
    ):
        ev = asyncio.run(resolve_bias_evidence("ZZZ", side="buy", vol_tier="low_vol", distance_atr_bucket="2-4"))
    assert ev["source"] == "system"
    assert ev["reason"] == "no_eod_action"


def test_cell_calibrated_does_not_apply_symbol_penalty():
    cell = {
        "symbol": "EURUSD", "side": "buy", "n": 20,
        "bias": 0.02, "bias_shrunk": 0.01,
    }
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=cell),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value={
                "action": "bayesian_penalty", "bias": 0.35, "status": "OVERCONFIDENT",
                "n_records": 50,
            }),
        ),
    ):
        out = asyncio.run(apply_calibration_adjustment(
            "EURUSD", p_win=0.8, kelly_f=0.1, side="buy",
            vol_tier="low_vol", distance_atr_bucket="2-4",
        ))
    assert out["applied"] is False
    assert out["reason"] == "cell_calibrated"
    assert out["source"] == "cell"
    assert out["p_win"] == 0.8
    assert out["kelly_f"] == 0.1


def test_cell_overconfident_cuts_kelly():
    cell = {
        "symbol": "EURUSD", "side": "sell", "n": 20,
        "bias": 0.35, "bias_shrunk": 0.30,
    }
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=cell),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value={"action": "none"}),
        ),
    ):
        out = asyncio.run(apply_calibration_adjustment(
            "EURUSD", p_win=0.85, kelly_f=0.2, side="short",
            vol_tier="high_vol", distance_atr_bucket="4+",
        ))
    assert out["applied"] is True
    assert out["source"] == "cell"
    assert out["status"] == "OVERCONFIDENT"
    assert out["kelly_f"] == pytest.approx(0.1)  # ×0.5
    assert out["p_win"] < 0.85  # bayesian shrink toward 0.5


def test_cell_underconfident_keeps_kelly():
    cell = {
        "symbol": "EURUSD", "side": "buy", "n": 20,
        "bias": -0.35, "bias_shrunk": -0.30,
    }
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=cell),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value={"action": "none"}),
        ),
    ):
        out = asyncio.run(apply_calibration_adjustment(
            "EURUSD", p_win=0.4, kelly_f=0.2, side="buy",
            vol_tier="low_vol", distance_atr_bucket="1-2",
        ))
    assert out["applied"] is True
    assert out["status"] == "UNDERCONFIDENT"
    assert out["kelly_f"] == pytest.approx(0.2)
    assert out["p_win"] > 0.4  # shrink toward 0.5


def test_stagnation_kill_switch_overrides_cell():
    from datetime import datetime, timezone

    cell = {
        "symbol": "EURUSD", "side": "buy", "n": 25,
        "bias": 0.02, "bias_shrunk": 0.01,
    }
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_bias_cell",
            new=AsyncMock(return_value=cell),
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value={
                "action": "stagnation_kill_switch", "bias": 0.4,
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "detail": {"stagnation_streak": 3},
            }),
        ),
    ):
        out = asyncio.run(apply_calibration_adjustment(
            "EURUSD", p_win=0.8, kelly_f=0.1, side="buy",
            vol_tier="low_vol", distance_atr_bucket="2-4",
        ))
    assert out["applied"] is True
    assert out["status"] == "STAGNATION_KILL_SWITCH"
    assert out["kelly_f"] == 0.0
    assert out["p_win"] == 0.5
