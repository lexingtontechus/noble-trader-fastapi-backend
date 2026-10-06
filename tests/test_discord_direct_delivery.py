"""NOBLE_TRADER_DISCORD_SIGNALS_ENABLED direct-delivery tests.

Covers direct Discord delivery from both sweep paths (light
``_process_symbol`` + heavy ``_push_heavy_signal``), gated by
NOBLE_TRADER_DISCORD_SIGNALS_ENABLED (default True). Originally built
(2026-08-10) as a Redis-feed-off fallback; now the only delivery path
since Redis was fully retired (2026-08-25) — the redis_delivery_active
param and the Redis pipeline publish it used to guard against are gone.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.discord_delivery import (
    maybe_send_discord_signal,
)
from regime_platform.sweep_orchestrator.heavy_sweep import HeavySweepMixin


def _minimal_ev_result():
    return SimpleNamespace(
        ev_scale=1.0,
        aggression=0.5,
        ev=0.2,
        ev_per_dollar=0.1,
        rr=1.5,
        p_win=0.6,
        p_regime=0.7,
        p_markov=0.55,
        p_timesfm=0.5,
        p_historical=0.6,
        sources_used=["hmm"],
        weights_used={"hmm": 1.0},
    )


def _minimal_best():
    return SimpleNamespace(brick_size=0.01, sl_bricks=5, tp_bricks=10, sharpe=1.2)


def _payload(**overrides):
    p = {
        "type": "heartbeat",
        "symbol": "XAUUSD",
        "ts": 1723300000000,
        "asset_class": "metals",
        "venue": "metaapi",
        "source": "sweep-light",
        "regime": "high_vol_strong_bull",
        "regime_conf": 0.9,
        "signal": "buy",
        "entry_price": 4000.0,
        "stop_loss": 3990.0,
        "take_profit": 4020.0,
        "aggression": 0.5,
        "brick_size": 0.01,
        "sl_bricks": 5,
        "tp_bricks": 10,
        "kelly_f": 0.1,
        "effective_kelly": 0.1,
        "ev": 0.2,
        "rr": 1.5,
        "p_win": 0.6,
        "p_regime": 0.7,
        "p_markov": 0.55,
        "p_timesfm": 0.5,
        "p_historical": 0.6,
        "timesfm_available": True,
        "prev_regime": "high_vol_weak_bull",
        "regime_shift": "true",
        "plan_ids": ["11111111-1111-1111-1111-111111111111"],
        "qualified": True,
    }
    p.update(overrides)
    return p


def _push_args():
    return dict(
        sym={"asset_class": "metals"},
        symbol="XAUUSD",
        regime_label="high_vol_strong_bull",
        regime_conf=0.9,
        trade_signal={
            "signal": "BUY",
            "entry_price": 4000.0,
            "stop_loss": 3990.0,
            "take_profit": 4020.0,
        },
        best=_minimal_best(),
        kelly_f=0.1,
        ev_result=_minimal_ev_result(),
        p_timesfm=0.5,
        cal=None,
    )


class _Stub:
    """Minimal stand-in for HeavySweepMixin's host object."""


def test_settings_default_enabled():
    """The toggle defaults to enabled — Discord keeps flowing out of the box."""
    assert settings.NOBLE_TRADER_DISCORD_SIGNALS_ENABLED is True


def test_helper_sends_when_enabled(monkeypatch):
    """Flag on → direct Discord send with aggregator-identical kwargs."""
    monkeypatch.setattr(settings, "NOBLE_TRADER_DISCORD_SIGNALS_ENABLED", True)
    send = AsyncMock(return_value=True)
    with patch("notifications.discord.discord_notifier.send_signal", send):
        ok = asyncio.run(
            maybe_send_discord_signal(_payload(), source="sweep-light")
        )
    assert ok is True
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["symbol"] == "XAUUSD"
    assert kwargs["direction"] == "BUY"
    assert kwargs["pattern"] == "Regime Signal"
    assert kwargs["price"] == 4000.0
    assert kwargs["sl_price"] == 3990.0
    assert kwargs["tp_price"] == 4020.0
    assert kwargs["kelly_fraction"] == 0.1
    assert kwargs["confidence"] == 0.6
    assert kwargs["regime"] == "high_vol_strong_bull"
    assert kwargs["sl_bricks"] == 5
    assert kwargs["tp_bricks"] == 10
    assert kwargs["prev_regime"] == "high_vol_weak_bull"
    assert kwargs["regime_shift"] == "true"
    assert kwargs["asset_class"] == "metals"
    assert kwargs["venue"] == "metaapi"
    assert kwargs["plan_ids"] == ["11111111-1111-1111-1111-111111111111"]


def test_helper_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "NOBLE_TRADER_DISCORD_SIGNALS_ENABLED", False)
    send = AsyncMock()
    with patch("notifications.discord.discord_notifier.send_signal", send):
        ok = asyncio.run(
            maybe_send_discord_signal(_payload(), source="sweep-light")
        )
    assert ok is False
    send.assert_not_awaited()


def test_helper_skips_neutral():
    send = AsyncMock()
    with patch("notifications.discord.discord_notifier.send_signal", send):
        ok = asyncio.run(
            maybe_send_discord_signal(_payload(signal="neutral"), source="sweep-light")
        )
    assert ok is False
    send.assert_not_awaited()


def test_helper_never_raises(monkeypatch):
    """A Discord failure must never propagate into the sweep."""
    monkeypatch.setattr(settings, "NOBLE_TRADER_DISCORD_SIGNALS_ENABLED", True)
    with patch(
        "notifications.discord.discord_notifier.send_signal",
        AsyncMock(side_effect=RuntimeError("webhook exploded")),
    ):
        ok = asyncio.run(
            maybe_send_discord_signal(_payload(), source="sweep-light")
        )
    assert ok is False


def test_heavy_sweep_dispatches_direct_discord(monkeypatch):
    """Heavy path always dispatches the direct Discord helper — no Redis
    branch remains to skip it."""
    monkeypatch.setattr(settings, "NOBLE_TRADER_DISCORD_SIGNALS_ENABLED", True)
    helper = AsyncMock(return_value=True)
    with patch(
        "regime_platform.sweep_orchestrator.discord_delivery.maybe_send_discord_signal",
        helper,
    ):
        asyncio.run(HeavySweepMixin._push_heavy_signal(_Stub(), **_push_args()))
    helper.assert_called_once()
    kwargs = helper.call_args.kwargs
    assert kwargs["source"] == "sweep-heavy"
    assert "redis_delivery_active" not in kwargs
    payload = helper.call_args.args[0]
    assert payload["symbol"] == "XAUUSD"
    assert payload["qualified"] is True
