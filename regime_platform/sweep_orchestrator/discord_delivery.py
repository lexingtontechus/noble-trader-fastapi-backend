"""
Direct Discord signal delivery for the sweep paths.

This module sends qualified sweep signals (light ``_process_symbol`` +
heavy ``_push_heavy_signal``) straight to the Discord notifier, gated by
``NOBLE_TRADER_DISCORD_SIGNALS_ENABLED``. Originally built (2026-08-10) as
a fallback for when the Redis/proxy signal feed was disabled — now the
only delivery path, since Redis has been fully retired (2026-08-25).

The mapping mirrors the legacy SignalAggregator's HEARTBEAT buy/sell
branch (pattern="regime_trade", direction upper-cased, full-price
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
) -> bool:
    """Send a qualified buy/sell signal directly to Discord (best-effort).

    Parameters
    ----------
    payload : dict
        The enriched signal payload dict (``signal``, ``entry_price``,
        ``stop_loss``, ``take_profit``, ``p_win``, ``kelly_f``,
        ``sl_bricks``, ``tp_bricks``, ``prev_regime``, ``regime_shift``,
        ``asset_class``, ``venue``, ``plan_ids``, ...).
    source : str
        Sweep source label for logging ("sweep-light" / "sweep-heavy").

    Returns
    -------
    bool
        True if the Discord notifier was invoked (or would have been),
        False if gated off or the send failed. Never raises.
    """
    if not settings.NOBLE_TRADER_DISCORD_SIGNALS_ENABLED:
        return False

    signal = str(payload.get("signal", "")).lower()
    if signal not in ("buy", "sell"):
        return False

    try:
        from notifications.discord import discord_notifier

        await discord_notifier.send_signal(
            symbol=payload.get("symbol", "UNKNOWN"),
            direction=signal.upper(),
            pattern="Regime Signal",
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
            # RANK board (20260928) — display only, never a gate
            rank=payload.get("kairos_rank_score"),
            state=payload.get("kairos_event_window") or payload.get("state"),
            ev_r=payload.get("ev"),
            # Dynamic deadline tier (20261005_REACHABILITY_REDESIGN)
            expected_resolution_hours=payload.get("expected_resolution_hours"),
            expected_resolution_days=payload.get("expected_resolution_days"),
            resolution_confidence=payload.get("resolution_confidence"),
            soft_deadline_hours=payload.get("soft_deadline_hours"),
            hard_deadline_hours=payload.get("hard_deadline_hours"),
            # Clef-Flash abstention (20261004)
            p_clef_wait=payload.get("p_clef_wait"),
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
