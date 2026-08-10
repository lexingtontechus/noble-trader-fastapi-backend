"""
Direct Discord signal delivery for the sweep paths (Option 2 — decoupled
from the Redis/proxy feed).

Incident (2026-08-10): qualified signals stopped reaching Discord when
``NOBLE_TRADER_REDIS_PUBLISH_ENABLED=false`` (proxy retirement). The ONLY
Discord path for sweep signals was Redis ``signal.raw.noble_trader`` →
in-process SignalAggregator (subscribed to that channel) → Discord. When
the Redis feed is disabled the orchestrator writes nothing to Redis, the
aggregator receives nothing, and Discord goes silent — even though the
webhooks themselves are healthy.

This module adds a DIRECT Discord delivery path used by both sweep paths
(light ``_process_symbol`` + heavy ``_push_heavy_signal``), gated by its
OWN toggle ``NOBLE_TRADER_DISCORD_SIGNALS_ENABLED`` so Discord no longer
depends on the Redis feed:

  - Redis feed OFF (proxy retired) + flag ON  → direct Discord send. ✅
  - Redis feed ON                              → aggregator already delivers
    Discord (original path); the direct send is skipped to avoid
    duplicate alerts.
  - Flag OFF                                   → no direct Discord sends.

The mapping mirrors ``SignalAggregator._route_to_discord``'s HEARTBEAT
buy/sell branch (pattern="regime_trade", direction upper-cased, full-price
entry/stop/tp, plan routing) so Discord embeds are byte-for-byte the same
format users already receive.

All errors are caught and logged — a Discord hiccup must NEVER break the
sweep.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from regime_platform.core.settings import settings

logger = logging.getLogger(__name__)


async def maybe_send_discord_signal(
    payload: dict[str, Any],
    *,
    source: str,
    redis_delivery_active: bool,
) -> bool:
    """Send a qualified buy/sell signal directly to Discord (best-effort).

    Parameters
    ----------
    payload : dict
        The heartbeat/agent payload dict (same shape the orchestrator
        publishes to Redis: ``signal``, ``entry_price``, ``stop_loss``,
        ``take_profit``, ``p_win``, ``kelly_f``, ``sl_bricks``,
        ``tp_bricks``, ``prev_regime``, ``regime_shift``,
        ``asset_class``, ``venue``, ``plan_ids``, ...).
    source : str
        Sweep source label for logging ("sweep-light" / "sweep-heavy").
    redis_delivery_active : bool
        True when the Redis feed is live (``NOBLE_TRADER_REDIS_PUBLISH_ENABLED``
        AND a Redis client is configured). In that state the in-process
        SignalAggregator already delivers Discord via the original path —
        this direct send is skipped to avoid duplicate alerts.

    Returns
    -------
    bool
        True if the Discord notifier was invoked (or would have been),
        False if gated off or the send failed. Never raises.
    """
    if not settings.NOBLE_TRADER_DISCORD_SIGNALS_ENABLED:
        return False
    if redis_delivery_active:
        # Original path alive — the aggregator delivers Discord. Direct
        # send here would duplicate the alert.
        return False

    signal = str(payload.get("signal", "")).lower()
    if signal not in ("buy", "sell"):
        return False

    try:
        from notifications.discord import discord_notifier

        await discord_notifier.send_signal(
            symbol=payload.get("symbol", "UNKNOWN"),
            direction=signal.upper(),
            pattern="regime_trade",
            price=payload.get("entry_price", 0.0),
            confidence=payload.get("p_win"),
            regime=payload.get("regime"),
            kelly_fraction=payload.get("kelly_f"),
            sl_price=payload.get("stop_loss"),
            tp_price=payload.get("take_profit"),
            sl_bricks=payload.get("sl_bricks"),
            tp_bricks=payload.get("tp_bricks"),
            prev_regime=payload.get("prev_regime") or None,
            regime_shift=(
                payload.get("regime_shift")
                if payload.get("regime_shift") == "true"
                else None
            ),
            asset_class=payload.get("asset_class"),
            venue=payload.get("venue"),
            plan_ids=payload.get("plan_ids") or [],
        )
        logger.info(
            "[discord:direct] %s %s signal alert sent (%s)",
            payload.get("symbol"), signal.upper(), source,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[discord:direct] %s signal alert send failed (%s): %s",
            payload.get("symbol"), source, exc,
        )
        return False
