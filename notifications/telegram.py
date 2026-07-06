"""
Telegram Bot Notifier - sends messages to Telegram chats via Bot API.

Design:
  - Uses a single bot token + chat ID for P&L and system alerts
  - HTML-formatted messages with emoji indicators by severity
  - Rate-limited: max 20 messages per minute (Telegram's limit is ~30/sec)
  - Non-blocking: all sends are fire-and-forget; failures are logged, never crash
  - Graceful degradation: if bot token / chat ID not configured, silently skips

Environment variables:
  TELEGRAM_BOT_TOKEN  -> Bot token from @BotFather
  TELEGRAM_CHAT_ID    -> Target chat ID (group or private)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from regime_platform.core.resilience import retry_with_backoff
from regime_platform.core.settings import settings

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────


class AlertLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Severity emoji and indicators
SEVERITY_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.SUCCESS: "✅",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.ERROR: "🚨",
    AlertLevel.CRITICAL: "🔴",
}

SEVERITY_LABEL = {
    AlertLevel.INFO: "INFO",
    AlertLevel.SUCCESS: "SUCCESS",
    AlertLevel.WARNING: "WARNING",
    AlertLevel.ERROR: "ERROR",
    AlertLevel.CRITICAL: "CRITICAL",
}


# ── Rate Limiter ─────────────────────────────────────────────────────────


@dataclass
class TelegramRateLimiter:
    """Simple sliding-window rate limiter for Telegram API.

    Telegram allows ~30 messages per second per bot, but we throttle
    more conservatively to avoid hitting group-specific limits.
    """
    max_per_window: int = 20
    window_seconds: float = 60.0
    _timestamps: List[float] = field(default_factory=list)

    def can_send(self) -> bool:
        """Check if we can send without exceeding rate limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
        return len(self._timestamps) < self.max_per_window

    def record_send(self) -> None:
        """Record that a send occurred."""
        now = time.monotonic()
        self._timestamps.append(now)


# ── Telegram Notifier ────────────────────────────────────────────────────


class TelegramNotifier:
    """
    Sends HTML-formatted notifications to Telegram via Bot API.

    All methods are async and non-blocking. Failures are logged but never
    raise exceptions or crash the calling code.

    Usage::

        notifier = TelegramNotifier()

        # Send a P&L alert
        await notifier.send_pnl_alert(
            metric="drawdown_pct",
            operator="lte",
            threshold_value=-5.0,
            current_value=-6.2,
            severity="critical",
        )

        # Send a system status
        await notifier.send_status(
            message="Pipeline warmup complete",
            level=AlertLevel.INFO,
        )

        # Check if configured
        if notifier.is_configured:
            ...
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self):
        self._bot_token: Optional[str] = settings.TELEGRAM_BOT_TOKEN or None
        self._chat_id: Optional[str] = settings.TELEGRAM_CHAT_ID or None
        self._rate_limiter = TelegramRateLimiter()
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """Check if bot token and chat ID are configured."""
        return bool(self._bot_token and self._chat_id)

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def close(self) -> None:
        """Close the HTTP client. Call on shutdown."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    # ── Core send method ──────────────────────────────────────────────

    async def _send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> bool:
        """
        Send a message via Telegram Bot API.

        Returns True if sent successfully, False otherwise.
        Never raises - all errors are caught and logged.
        """
        if not self.is_configured:
            return False

        # Rate limit check
        if not self._rate_limiter.can_send():
            logger.warning("Telegram rate limit reached, dropping message")
            return False

        url = self.BASE_URL.format(token=self._bot_token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        try:
            client = self._get_client()

            async def _do_post():
                return await client.post(url, json=payload)

            try:
                response = await retry_with_backoff(
                    _do_post,
                    max_retries=2,
                    base_delay=1.0,
                    retryable_exceptions=(
                        httpx.ConnectError,
                        httpx.TimeoutException,
                        ConnectionError,
                        TimeoutError,
                        OSError,
                    ),
                )
            except (httpx.ConnectError, httpx.TimeoutException, ConnectionError, TimeoutError, OSError) as e:
                logger.error(f"Telegram send error after retries: {e}")
                return False

            if response.status_code == 200:
                result = response.json()
                if result.get("ok"):
                    self._rate_limiter.record_send()
                    return True
                else:
                    logger.error(f"Telegram API error: {result.get('description', 'unknown')}")
                    return False
            elif response.status_code == 429:
                # Rate limited by Telegram
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                logger.warning(f"Telegram 429 rate limited, retry after {retry_after}s")
                return False
            else:
                logger.error(f"Telegram send failed: {response.status_code}: {response.text[:200]}")
                return False

        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    # ── Message Builders ──────────────────────────────────────────────

    @staticmethod
    def _build_pnl_alert_html(
        metric: str,
        operator: str,
        threshold_value: float,
        current_value: float,
        severity: str = "warning",
        message: str = "",
    ) -> str:
        """Build an HTML-formatted P&L alert message for Telegram."""
        emoji = SEVERITY_EMOJI.get(AlertLevel(severity), "⚠️")
        label = SEVERITY_LABEL.get(AlertLevel(severity), severity.upper())

        # Format metric name for display
        metric_display = metric.replace("_", " ").title()

        # Format values with appropriate precision
        if metric in ("drawdown_pct", "equity_change_pct", "var_breach"):
            threshold_str = f"{threshold_value:+.2f}%"
            current_str = f"{current_value:+.2f}%"
        else:
            threshold_str = f"${threshold_value:+,.2f}"
            current_str = f"${current_value:+,.2f}"

        text = (
            f"{emoji} <b>P&amp;L Alert - {label}</b>\n\n"
            f"<b>Metric:</b> {metric_display}\n"
            f"<b>Condition:</b> {operator.replace('_', ' ')} {threshold_str}\n"
            f"<b>Current Value:</b> {current_str}\n"
        )

        if message:
            text += f"\n📝 {message}\n"

        text += f"\n🕐 <i>{_utc_now_iso()}</i>"

        return text

    @staticmethod
    def _build_status_html(
        message: str,
        level: AlertLevel = AlertLevel.INFO,
        data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Build an HTML-formatted system status message for Telegram."""
        emoji = SEVERITY_EMOJI.get(level, "ℹ️")
        label = SEVERITY_LABEL.get(level, level.value.upper())

        text = f"{emoji} <b>System - {label}</b>\n\n{message}\n"

        if data:
            text += "\n"
            for key, value in list(data.items())[:8]:
                display_key = key.replace("_", " ").title()
                text += f"<b>{display_key}:</b> {value}\n"

        text += f"\n🕐 <i>{_utc_now_iso()}</i>"
        return text

    # ── Public API ────────────────────────────────────────────────────

    async def send_pnl_alert(
        self,
        metric: str,
        operator: str,
        threshold_value: float,
        current_value: float,
        severity: str = "warning",
        message: str = "",
    ) -> bool:
        """
        Send a P&L alert notification to Telegram.

        Parameters
        ----------
        metric : str
            The metric that triggered (day_pnl, unrealized_pnl, drawdown_pct, etc.)
        operator : str
            The comparison operator (lt, gt, lte, gte, crosses_below, crosses_above)
        threshold_value : float
            The threshold value
        current_value : float
            The current metric value
        severity : str
            Alert severity: info, warning, critical
        message : str
            Human-readable alert message
        """
        # Critical alerts send with sound; others silently
        disable_notification = severity != "critical"

        text = self._build_pnl_alert_html(
            metric=metric,
            operator=operator,
            threshold_value=threshold_value,
            current_value=current_value,
            severity=severity,
            message=message,
        )

        return await self._send_message(
            text=text,
            disable_notification=disable_notification,
        )

    async def send_status(
        self,
        message: str,
        level: AlertLevel = AlertLevel.INFO,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a system status notification to Telegram.

        Parameters
        ----------
        message : str
            Human-readable status message
        level : AlertLevel
            Severity level
        data : dict, optional
            Additional structured data
        """
        disable_notification = level not in (AlertLevel.ERROR, AlertLevel.CRITICAL)

        text = self._build_status_html(
            message=message,
            level=level,
            data=data,
        )

        return await self._send_message(
            text=text,
            disable_notification=disable_notification,
        )

    async def send_signal(
        self,
        symbol: str,
        direction: str,
        pattern: str,
        price: float,
        confidence: float,
        brick_size: Optional[float] = None,
        regime: Optional[str] = None,
        position_size_usd: Optional[float] = None,
        kelly_fraction: Optional[float] = None,
        masaniello_sizing: bool = False,
        **kwargs,
    ) -> bool:
        """
        Send a trading signal notification to Telegram.
        
        Args:
            symbol: Trading symbol (e.g., "SPY")
            direction: Signal direction ("LONG", "SHORT", "BUY", "SELL")
            pattern: Pattern type ("bull_run", "bear_run", "reversal", etc.)
            price: Current price
            confidence: Signal confidence (0.0 to 1.0)
            brick_size: Renko brick size (optional)
            regime: Current regime (optional)
            position_size_usd: Position size in USD (optional)
            kelly_fraction: Kelly fraction (optional)
            masaniello_sizing: Whether using Masaniello sizing (optional)
            **kwargs: Additional data
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_configured:
            return False
            
        # Rate limit check
        if not self._rate_limiter.can_send():
            logger.warning("Telegram rate limit reached, dropping signal")
            return False
            
        # Direction emoji
        direction_emoji = {
            "LONG": "📈", "SHORT": "📉", "BUY": "📈", "SELL": "📉", "HOLD": "⏸️"
        }.get(direction.upper(), "🔔")
        
        # Pattern emoji and descriptive text
        pattern_info = {
            "bull_run": ("🐂", "Bull Run"),
            "bear_run": ("🐻", "Bear Run"), 
            "reversal": ("🔄", "Reversal"),
            "consolidation": ("⚖️", "Consolidation"),
            "breakout": ("🚀", "Breakout"),
            "double_top": ("⬆️", "Double Top"),
            "double_bottom": ("⬇️", "Double Bottom"),
            "no_pattern": ("❓", "No Pattern")
        }
        
        pattern_emoji, pattern_name = pattern_info.get(pattern.lower(), ("❓", "Unknown Pattern"))
        
        # Confidence indicator
        confidence_color = "🟢" if confidence >= 0.8 else "🟡" if confidence >= 0.6 else "🔴"
        confidence_pct = confidence * 100
        
        # Build enhanced HTML message
        message_lines = [
            f"{direction_emoji} <b>{symbol} Trading Signal</b>",
            f"{pattern_emoji} <i>{pattern_name}</i>",
            "",
            f"💰 <b>Price:</b> ${price:.2f}",
            f"📊 <b>Confidence:</b> {confidence_color} {confidence_pct:.0f}%",
            f"🎯 <b>Direction:</b> {direction}"
        ]
        
        # Add optional information
        if regime:
            regime_color = "🟢" if "bull" in regime.lower() else "🔴" if "bear" in regime.lower() else "🟡"
            message_lines.append(f"🏛️ <b>Regime:</b> {regime_color} {regime}")
            
        if brick_size:
            message_lines.append(f"🧱 <b>Brick Size:</b> ${brick_size:.2f}")
            
        if position_size_usd and position_size_usd > 0:
            message_lines.append(f"💸 <b>Position:</b> ${position_size_usd:,.0f}")
            
        if kelly_fraction:
            kelly_color = "🟢" if kelly_fraction >= 0.2 else "🟡" if kelly_fraction >= 0.1 else "🔴"
            message_lines.append(f"🎲 <b>Kelly F:</b> {kelly_color} {kelly_fraction:.3f}")
            
        if masaniello_sizing:
            message_lines.append("⚡ <b>Sizing:</b> Dynamic Masaniello")
            
        # Add technical indicators if available
        if kwargs.get("atr"):
            message_lines.append(f"📈 <b>ATR:</b> ${kwargs['atr']:.2f}")
            
        if kwargs.get("support"):
            message_lines.append(f"🛡️ <b>Support:</b> ${kwargs['support']:.2f}")
            
        if kwargs.get("resistance"):
            message_lines.append(f"🎯 <b>Resistance:</b> ${kwargs['resistance']:.2f}")
            
        # Add timeframe if available
        if kwargs.get("timeframe"):
            tf_emoji = {
                "1m": "⏱️", "5m": "⏱️", "15m": "⏱️", "30m": "⏱️",
                "1h": "🕐", "4h": "🕐", "1d": "📅", "1w": "📅"
            }.get(kwargs["timeframe"], "📊")
            message_lines.append(f"{tf_emoji} <b>Timeframe:</b> {kwargs['timeframe']}")
        
        # Add risk information
        if kwargs.get("risk_level"):
            risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(kwargs["risk_level"], "⚠️")
            message_lines.append(f"⚠️ <b>Risk:</b> {risk_emoji} {kwargs['risk_level'].title()}")
            
        message_lines.append("")
        message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        message_lines.append("<i>Noble Trader Dynamic Regime Platform</i>")
        
        message = "\n".join(message_lines)
        
        try:
            await self._send_html_message(message, parse_mode="HTML")
            self._rate_limiter.record_send()
            logger.info(f"Signal sent to Telegram for {symbol}")
            return True
        except Exception as e:
            logger.error(f"Failed to send signal to Telegram: {e}")
            return False

    async def send_risk_alert(
        self,
        risk_type: str,
        message: str,
        level: AlertLevel = AlertLevel.WARNING,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a risk management alert to Telegram.

        Parameters
        ----------
        risk_type : str
            Type of risk alert (daily_loss_limit, consecutive_losses, etc.)
        message : str
            Human-readable risk alert message
        level : AlertLevel
            Severity
        data : dict, optional
            Additional risk metrics
        """
        disable_notification = level not in (AlertLevel.ERROR, AlertLevel.CRITICAL)

        emoji = SEVERITY_EMOJI.get(level, "⚠️")
        label = SEVERITY_LABEL.get(level, level.value.upper())

        text = (
            f"{emoji} <b>Risk Alert - {label}</b>\n\n"
            f"<b>Type:</b> {risk_type.replace('_', ' ').title()}\n"
            f"{message}\n"
        )

        if data:
            text += "\n"
            for key, value in list(data.items())[:6]:
                display_key = key.replace("_", " ").title()
                text += f"<b>{display_key}:</b> {value}\n"

        text += f"\n🕐 <i>{_utc_now_iso()}</i>"

        return await self._send_message(
            text=text,
            disable_notification=disable_notification,
        )


# ── Helper ───────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ── Singleton ────────────────────────────────────────────────────────────

# Global singleton instance - import and use across the backend
telegram_notifier = TelegramNotifier()
