"""E2E + replay: reachability-conditioned probability SHADOW STAMP.

Verifies the shadow-stamp wiring the sweep uses: the mode gate, the
lookup_reach_prob fetch+compute, the compare_to_blend delta, and the payload
fields the sweep stamps on nt_signal_sim. Offline + deterministic — the fetch
is mocked, the payload builder is the real orchestrator function.

The point of shadow mode: p_reach + p_reach_delta accumulate on nt_signal_sim
so the walk-forward validation can measure whether replacing the direction-only
p_win with P_reach reduces bias toward 0 and improves expectancy — WITHOUT any
live EV/Kelly change.
"""

from __future__ import annotations

import pytest

from regime_platform.core import feature_flags as ff
from regime_platform.sweep_orchestrator.reachability_prob import (
    compare_to_blend,
    lookup_reach_prob,
    reachability_conditioned_probability,
)


# ── Mode gate ships OFF (fail-safe) ─────────────────────────────────────────

def test_p_win_reachability_mode_defaults_off():
    """Ships 'off' — no stamp until an operator flips to shadow. A bad value
    also falls back to off (never stamp unless asked)."""
    assert ff.get_p_win_reachability_mode() == "off"


# ── lookup_reach_prob: fetch + compute, fail-open ───────────────────────────

@pytest.mark.asyncio
async def test_lookup_reach_prob_computes_from_fetched_row(monkeypatch):
    """A mocked Supabase reachability row -> P_reach computed and matched."""
    import httpx

    row = {"symbol": "XAUUSD", "vol_tier": "med_low_vol",
           "distance_atr_bucket": "4+", "signal": "all", "n": 214,
           "pct_hit_tp": 0.192, "pct_hit_sl": 0.511, "pct_expired": 0.297,
           "p90_hours": 56.0}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return [row]

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    r = await lookup_reach_prob(
        "XAUUSD", vol_tier="med_low_vol", distance_bucket="4+",
        expiry_hours=56.0, signal="all")
    assert r is not None and r.matched is True
    assert abs(r.p_reach - 0.192) < 0.01


@pytest.mark.asyncio
async def test_lookup_reach_prob_fails_open_on_empty(monkeypatch):
    """No row -> None (stamp omitted), never a fabricated probability."""
    import httpx

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return []

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await lookup_reach_prob(
        "EURUSD", vol_tier="low_vol", distance_bucket="4+") is None


@pytest.mark.asyncio
async def test_lookup_reach_prob_fails_open_on_error(monkeypatch):
    """A fetch exception -> None (fail-open into the sweep tick)."""
    import httpx

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    assert await lookup_reach_prob(
        "XAUUSD", vol_tier="low_vol", distance_bucket="4+") is None


# ── The delta the sweep stamps (blend vs reachability) ──────────────────────

def test_shadow_delta_matches_live_replay_shape():
    """For an overpriced cell (blend 0.36 vs a wide gold cell 0.19), the
    stamped delta is positive; for the underpriced BTCUSD cell it is negative.
    Same shape as the 2026-10-11 live replay (15/17 overpriced, 2 under)."""
    over = compare_to_blend(0.192, 0.36)   # gold 4+ cell
    under = compare_to_blend(0.417, 0.36)  # BTCUSD 2-4 cell
    assert over["delta"] > 0 and over["direction"] == "overpriced"
    assert under["delta"] < 0 and under["direction"] == "underpriced"


# ── Payload builder carries the shadow fields ───────────────────────────────

def test_payload_builder_stamps_p_reach_when_present():
    """The orchestrator's _build_signal_sim_payload includes p_reach /
    p_reach_delta only when a P_reach read exists (shadow fail-open)."""
    from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
    base = dict(
        symbol="XAUUSD", venue="OANDA", ts="2026-10-11T00:00:00Z", signal="buy",
        entry_price=2000.0, stop_loss=1990.0, take_profit=2020.0,
        brick_size=1.0, sl_bricks=3, tp_bricks=5, p_win=0.36, ev=0.1,
        regime="low_vol_bull", regime_conf=0.8, source="sweep-light",
        qualified=True,
    )
    # No reachability read -> columns absent (fail-open).
    without = _build_signal_sim_payload(**base)
    assert "p_reach" not in without
    # With a read -> both stamped.
    with_reach = _build_signal_sim_payload(**base, p_reach=0.192, p_reach_delta=0.168)
    assert with_reach["p_reach"] == 0.192
    assert with_reach["p_reach_delta"] == 0.168


def test_payload_p_reach_delta_none_when_only_p_reach():
    """p_reach present but delta None (edge case) -> p_reach stamped, delta
    None (not dropped)."""
    from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
    p = _build_signal_sim_payload(
        symbol="XAUUSD", venue="OANDA", ts="2026-10-11T00:00:00Z", signal="buy",
        entry_price=2000.0, stop_loss=1990.0, take_profit=2020.0,
        brick_size=1.0, sl_bricks=3, tp_bricks=5, p_win=0.36, ev=0.1,
        regime="low_vol_bull", regime_conf=0.8, source="sweep-light",
        qualified=True, p_reach=0.192, p_reach_delta=None,
    )
    assert p["p_reach"] == 0.192
    assert p["p_reach_delta"] is None
