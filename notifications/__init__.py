"""
Notifications Package - Discord and Telegram integration for the Noble Trader Agent.

Discord Channels:
  - #trade-signals   -> Renko pattern signals (bull run, bear run, reversal, etc.)
  - #trade-executions -> Alpaca order fills, SL/TP hits
  - #system-status   -> Warmup completions, pipeline resets, errors, health, P&L alerts

Telegram:
  - Single chat for P&L alerts, risk alerts, and system status
  - HTML-formatted messages with severity indicators

Usage::

    from notifications import discord_notifier, telegram_notifier

    # Discord: Send a signal alert (auto-routes to #trade-signals)
    await discord_notifier.send_signal(symbol="SPY", direction="LONG",
                                       pattern="bull_run", price=450.25,
                                       confidence=0.85)

    # Discord: Send a system status / P&L alert
    await discord_notifier.send_status(message="P&L Alert: drawdown -5%",
                                       level=AlertLevel.WARNING)

    # Telegram: Send a P&L alert
    await telegram_notifier.send_pnl_alert(
        metric="drawdown_pct", operator="lte",
        threshold_value=-5.0, current_value=-6.2,
        severity="critical"
    )

    # Telegram: Send a system status
    await telegram_notifier.send_status(message="Pipeline warmup complete",
                                        level=AlertLevel.INFO)
"""

from notifications.discord import discord_notifier, AlertLevel as DiscordAlertLevel
from notifications.telegram import telegram_notifier, AlertLevel as TelegramAlertLevel

__all__ = [
    "discord_notifier",
    "telegram_notifier",
    "DiscordAlertLevel",
    "TelegramAlertLevel",
]
