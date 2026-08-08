"""NOBLE_TRADER_REDIS_PUBLISH_ENABLED gate tests.

Covers the settings default + the heavy-sweep publish path (the only
unit-testable seam; the light-sweep gate in orchestrator._process_symbol is
inline and covered by the full-suite run).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

from regime_platform.core.settings import settings
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
        p_pattern=0.6,
        sources_used=["hmm"],
        weights_used={"hmm": 1.0},
    )


def _minimal_best():
    return SimpleNamespace(brick_size=0.01, sl_bricks=5, tp_bricks=10, sharpe=1.2)


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
        decimals=2,
        best=_minimal_best(),
        kelly_f=0.1,
        ev_result=_minimal_ev_result(),
        p_timesfm=0.5,
        cal=None,
    )


class _Stub:
    """Minimal stand-in exposing only ``self._redis`` (what the mixin needs)."""

    def __init__(self, redis):
        self._redis = redis


def test_settings_default_enabled():
    """The toggle defaults to enabled — current production behavior preserved."""
    assert settings.NOBLE_TRADER_REDIS_PUBLISH_ENABLED is True


def test_heavy_sweep_skips_redis_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "NOBLE_TRADER_REDIS_PUBLISH_ENABLED", False)
    redis = MagicMock()
    asyncio.run(HeavySweepMixin._push_heavy_signal(_Stub(redis), **_push_args()))
    redis.pipeline.assert_not_called()


def test_heavy_sweep_publishes_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "NOBLE_TRADER_REDIS_PUBLISH_ENABLED", True)
    redis = MagicMock()
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis.pipeline.return_value = pipe
    asyncio.run(HeavySweepMixin._push_heavy_signal(_Stub(redis), **_push_args()))
    redis.pipeline.assert_called_once_with(transaction=False)
    pipe.publish.assert_called_once_with("signal.raw.noble_trader", ANY)
    pipe.xadd.assert_called_once()
    pipe.execute.assert_awaited_once()
