"""
Discord Webhook Notifier — sends rich embed notifications to Discord channels.

Design:
  - Three webhooks: SIGNALS, EXECUTIONS, STATUS (one per Discord channel)
  - Rich Discord embeds with color coding by alert type and severity
  - Rate-limited: respects Discord's 5 requests / 2 seconds per webhook
  - Non-blocking: all sends are fire-and-forget; failures are logged, never crash
  - Graceful degradation: if webhook URLs are not configured, silently skips

Rate limiting:
  Discord allows ~5 webhook messages per 2 seconds per channel.
  We use a simple sliding-window counter per webhook URL.

Environment variables:
  DISCORD_WEBHOOK_SIGNALS     → #trade-signals channel
  DISCORD_WEBHOOK_EXECUTIONS  → #trade-executions channel
  DISCORD_WEBHOOK_STATUS      → #system-status channel
"""

from __future__ import annotations

import asyncio
import json
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


class AlertChannel(str, Enum):
    SIGNALS = "signals"
    EXECUTIONS = "executions"
    STATUS = "status"


class AlertLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Discord embed color palette (decimal integers for Discord API)
EMBED_COLORS = {
    AlertLevel.INFO: 0x3498DB,       # Blue
    AlertLevel.SUCCESS: 0x2ECC71,    # Green
    AlertLevel.WARNING: 0xF39C12,    # Orange/Amber
    AlertLevel.ERROR: 0xE74C3C,      # Red
    AlertLevel.CRITICAL: 0x9B59B6,   # Purple
}

# Direction colors for signal embeds
DIRECTION_COLORS = {
    "LONG": 0x2ECC71,    # Green
    "BUY": 0x2ECC71,     # Green
    "SHORT": 0xE74C3C,   # Red
    "SELL": 0xE74C3C,    # Red
}

# Alert type emojis
ALERT_EMOJI = {
    AlertChannel.SIGNALS: "📊",
    AlertChannel.EXECUTIONS: "💰",
    AlertChannel.STATUS: "🔧",
}

LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.SUCCESS: "✅",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.ERROR: "🚨",
    AlertLevel.CRITICAL: "🔴",
}


# ── Rate Limiter ─────────────────────────────────────────────────────────


@dataclass
class RateLimiter:
    """Simple sliding-window rate limiter per webhook URL.

    Discord allows ~5 messages per 2 seconds per webhook.
    We track timestamps of recent sends and block if over limit.
    """
    max_per_window: int = 5
    window_seconds: float = 2.0
    _timestamps: Dict[str, List[float]] = field(default_factory=dict)

    def can_send(self, url: str) -> bool:
        """Check if we can send to this URL without exceeding rate limit."""
        now = time.monotonic()
        timestamps = self._timestamps.get(url, [])

        # Remove timestamps outside the window
        cutoff = now - self.window_seconds
        timestamps = [ts for ts in timestamps if ts > cutoff]
        self._timestamps[url] = timestamps

        return len(timestamps) < self.max_per_window

    def record_send(self, url: str) -> None:
        """Record that a send occurred for rate limiting."""
        now = time.monotonic()
        if url not in self._timestamps:
            self._timestamps[url] = []
        self._timestamps[url].append(now)


# ── Discord Notifier ─────────────────────────────────────────────────────


class DiscordNotifier:
    """
    Sends rich embed notifications to Discord via webhooks.

    All methods are async and non-blocking. Failures are logged but never
    raise exceptions or crash the calling code.

    Usage::

        notifier = DiscordNotifier()

        # Auto-loaded from env vars
        await notifier.send_signal(symbol="SPY", direction="LONG", ...)

        # Check if configured
        if notifier.is_configured:
            ...
    """

    def __init__(self):
        # Load webhook URLs from centralized settings
        self._webhooks: Dict[AlertChannel, Optional[str]] = {
            AlertChannel.SIGNALS: settings.DISCORD_WEBHOOK_SIGNALS or None,
            AlertChannel.EXECUTIONS: settings.DISCORD_WEBHOOK_EXECUTIONS or None,
            AlertChannel.STATUS: settings.DISCORD_WEBHOOK_STATUS or None,
        }
        self._rate_limiter = RateLimiter()
        self._http_client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """Check if at least one webhook is configured."""
        return any(self._webhooks.values())

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

    async def _send_webhook(
        self,
        channel: AlertChannel,
        embed: Dict[str, Any],
        content: Optional[str] = None,
    ) -> bool:
        """
        Send a Discord webhook message with a rich embed.

        Returns True if sent successfully, False otherwise.
        Never raises — all errors are caught and logged.
        """
        url = self._webhooks.get(channel)
        if not url:
            return False

        # Rate limit check
        if not self._rate_limiter.can_send(url):
            logger.warning(f"Discord rate limit reached for {channel.value}, dropping message")
            return False

        payload: Dict[str, Any] = {}
        if content:
            payload["content"] = content
        if embed:
            payload["embeds"] = [embed]

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
                logger.error(f"Discord send error for {channel.value} after retries: {e}")
                return False

            if response.status_code == 204 or response.status_code == 200:
                self._rate_limiter.record_send(url)
                return True
            elif response.status_code == 429:
                # Rate limited by Discord — extract retry-after
                retry_after = response.headers.get("Retry-After", "2")
                logger.warning(
                    f"Discord 429 rate limited on {channel.value}, "
                    f"retry after {retry_after}s"
                )
                return False
            else:
                logger.error(
                    f"Discord webhook failed: {response.status_code} "
                    f"for {channel.value}: {response.text[:200]}"
                )
                return False

        except Exception as e:
            logger.error(f"Discord send error for {channel.value}: {e}")
            return False

    # ── Build Discord embed ───────────────────────────────────────────

    @staticmethod
    def _build_embed(
        title: str,
        description: str,
        color: int = 0x3498DB,
        fields: Optional[List[Dict[str, str]]] = None,
        footer: Optional[str] = None,
        emoji: str = "",
    ) -> Dict[str, Any]:
        """Build a Discord embed payload."""
        embed: Dict[str, Any] = {
            "title": f"{emoji} {title}" if emoji else title,
            "description": description,
            "color": color,
            "timestamp": _utc_now_iso(),
        }

        if fields:
            embed["fields"] = [
                {
                    "name": f["name"],
                    "value": str(f["value"]),
                    "inline": f.get("inline", True),
                }
                for f in fields
            ]

        if footer:
            embed["footer"] = {"text": footer}

        return embed

    # ── Public API: Signal Alerts ─────────────────────────────────────

    async def send_signal(
        self,
        symbol: str,
        direction: str,
        pattern: str,
        price: float,
        confidence: Optional[float] = None,
        velocity: Optional[float] = None,
        brick_size: Optional[float] = None,
        brick_count: Optional[int] = None,
        regime: Optional[str] = None,
        kelly_fraction: Optional[float] = None,
    ) -> bool:
        """
        Send a trading signal alert to #trade-signals.

        This is called when the Renko pattern detector produces a signal
        that passes all filter gates. The embed is color-coded by direction
        (green for LONG, red for SHORT).

        Parameters
        ----------
        symbol : str
            Trading symbol (e.g., "SPY")
        direction : str
            Signal direction ("LONG" or "SHORT")
        pattern : str
            Pattern type (bull_run, bear_run, reversal, double_top, etc.)
        price : float
            Signal price
        confidence : float, optional
            Signal confidence 0.0-1.0
        velocity : float, optional
            Brick velocity (bricks/minute)
        brick_size : float, optional
            Current brick size in dollars
        brick_count : int, optional
            Current brick count in the pipeline
        regime : str, optional
            Current HMM regime label
        kelly_fraction : float, optional
            Kelly criterion fraction for position sizing
        """
        dir_emoji = "🟢" if direction.upper() in ("LONG", "BUY") else "🔴"
        color = DIRECTION_COLORS.get(direction.upper(), 0x3498DB)

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Direction", "value": f"{dir_emoji} {direction}", "inline": True},
            {"name": "Price", "value": f"${price:.2f}", "inline": True},
            {"name": "Pattern", "value": pattern, "inline": True},
        ]

        if confidence is not None:
            fields.append({
                "name": "Confidence",
                "value": f"{confidence:.0%}",
                "inline": True,
            })
        if velocity is not None:
            fields.append({
                "name": "Velocity",
                "value": f"{velocity:.2f} bricks/min",
                "inline": True,
            })
        if brick_size is not None:
            fields.append({
                "name": "Brick Size",
                "value": f"${brick_size:.2f}",
                "inline": True,
            })
        if regime is not None:
            fields.append({
                "name": "Regime",
                "value": regime,
                "inline": True,
            })
        if kelly_fraction is not None:
            fields.append({
                "name": "Kelly %",
                "value": f"{kelly_fraction:.1%}",
                "inline": True,
            })

        embed = self._build_embed(
            title=f"Signal: {symbol} {direction}",
            description=f"**{pattern}** pattern detected at ${price:.2f}",
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Renko HFT Pipeline",
            emoji=ALERT_EMOJI[AlertChannel.SIGNALS],
        )

        return await self._send_webhook(AlertChannel.SIGNALS, embed)

    # ── Public API: Execution Alerts ──────────────────────────────────

    async def send_execution(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        order_id: Optional[str] = None,
        order_type: str = "market",
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        pnl_bricks: Optional[float] = None,
        pnl_dollars: Optional[float] = None,
        status: str = "filled",
    ) -> bool:
        """
        Send a trade execution alert to #trade-executions.

        Called when an Alpaca order is submitted, filled, or when SL/TP
        is hit. The embed is color-coded by PnL (green for profit, red
        for loss, blue for entry).

        Parameters
        ----------
        symbol : str
            Trading symbol
        side : str
            "buy" or "sell"
        qty : float
            Number of shares
        price : float, optional
            Execution price
        order_id : str, optional
            Alpaca order ID
        order_type : str
            "market", "limit", "bracket"
        sl_price : float, optional
            Stop-loss price
        tp_price : float, optional
            Take-profit price
        pnl_bricks : float, optional
            P&L in brick units (for close alerts)
        pnl_dollars : float, optional
            P&L in dollars (for close alerts)
        status : str
            "filled", "submitted", "rejected", "cancelled"
        """
        # Determine color based on context
        if pnl_bricks is not None:
            color = 0x2ECC71 if pnl_bricks >= 0 else 0xE74C3C
        elif status == "rejected":
            color = 0xE74C3C
        else:
            color = DIRECTION_COLORS.get(side.upper(), 0x3498DB)

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Side", "value": side.upper(), "inline": True},
            {"name": "Qty", "value": str(int(qty)), "inline": True},
            {"name": "Status", "value": status.upper(), "inline": True},
        ]

        if price is not None:
            fields.append({
                "name": "Price", "value": f"${price:.2f}", "inline": True,
            })
        if order_id is not None:
            fields.append({
                "name": "Order ID", "value": f"`{order_id[:16]}…`", "inline": True,
            })
        if sl_price is not None:
            fields.append({
                "name": "Stop-Loss", "value": f"${sl_price:.2f}", "inline": True,
            })
        if tp_price is not None:
            fields.append({
                "name": "Take-Profit", "value": f"${tp_price:.2f}", "inline": True,
            })
        if pnl_bricks is not None:
            pnl_emoji = "📈" if pnl_bricks >= 0 else "📉"
            fields.append({
                "name": f"{pnl_emoji} P&L",
                "value": f"{pnl_bricks:+.2f} bricks",
                "inline": True,
            })
        if pnl_dollars is not None:
            fields.append({
                "name": "P&L $",
                "value": f"${pnl_dollars:+.2f}",
                "inline": True,
            })

        # Build title and description based on context
        if pnl_bricks is not None:
            title = f"Close: {symbol} {side.upper()}"
            result = "PROFIT" if pnl_bricks >= 0 else "LOSS"
            description = f"Position closed with **{result}**: {pnl_bricks:+.2f} bricks (${pnl_dollars:+.2f})" if pnl_dollars else f"Position closed: {pnl_bricks:+.2f} bricks"
        elif status == "rejected":
            title = f"Rejected: {symbol} {side.upper()}"
            description = f"Order for {int(qty)} shares of {symbol} was rejected"
        else:
            title = f"Entry: {symbol} {side.upper()}"
            description = f"Bracket order: {int(qty)} shares at {'${price:.2f}' if price else 'market'}"

        embed = self._build_embed(
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Alpaca Executor",
            emoji=ALERT_EMOJI[AlertChannel.EXECUTIONS],
        )

        return await self._send_webhook(AlertChannel.EXECUTIONS, embed)

    # ── Public API: System Status ─────────────────────────────────────

    async def send_status(
        self,
        message: str,
        symbol: Optional[str] = None,
        level: AlertLevel = AlertLevel.INFO,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a system status alert to #system-status.

        Used for warmup completions, pipeline resets, errors, health
        checks, and any non-trade operational notifications.

        Parameters
        ----------
        message : str
            Human-readable status message
        symbol : str, optional
            Associated symbol (if any)
        level : AlertLevel
            Severity level (determines embed color)
        data : dict, optional
            Additional structured data to show as fields
        """
        color = EMBED_COLORS.get(level, 0x3498DB)
        emoji = LEVEL_EMOJI.get(level, "ℹ️")

        fields = []
        if symbol:
            fields.append({"name": "Symbol", "value": symbol, "inline": True})

        fields.append({"name": "Level", "value": f"{emoji} {level.value}", "inline": True})

        if data:
            for key, value in list(data.items())[:8]:  # Max 8 fields for readability
                fields.append({
                    "name": key.replace("_", " ").title(),
                    "value": str(value),
                    "inline": True,
                })

        embed = self._build_embed(
            title=f"System: {level.value.upper()}",
            description=message,
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Pipeline Status",
            emoji=ALERT_EMOJI[AlertChannel.STATUS],
        )

        return await self._send_webhook(AlertChannel.STATUS, embed)

    # ── Public API: Risk Alert ────────────────────────────────────────

    async def send_risk_alert(
        self,
        symbol: str,
        risk_type: str,
        message: str,
        level: AlertLevel = AlertLevel.WARNING,
        data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a risk management alert.

        Routes to #system-status with elevated color coding.
        Used for daily loss limits, consecutive losses, cooldown triggers.

        Parameters
        ----------
        symbol : str
            Trading symbol
        risk_type : str
            "daily_loss_limit", "consecutive_losses", "cooldown", "regime_mismatch"
        message : str
            Human-readable risk alert message
        level : AlertLevel
            Severity (WARNING or ERROR typically)
        data : dict, optional
            Additional risk metrics
        """
        color = EMBED_COLORS.get(level, 0xF39C12)
        emoji = "⚠️" if level == AlertLevel.WARNING else "🚨"

        fields = [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Risk Type", "value": risk_type, "inline": True},
        ]

        if data:
            for key, value in list(data.items())[:6]:
                fields.append({
                    "name": key.replace("_", " ").title(),
                    "value": str(value),
                    "inline": True,
                })

        embed = self._build_embed(
            title=f"Risk Alert: {risk_type}",
            description=message,
            color=color,
            fields=fields,
            footer="Noble Trader Agent | Risk Manager",
            emoji=emoji,
        )

        return await self._send_webhook(AlertChannel.STATUS, embed)

    # ── Convenience: Send to all channels ─────────────────────────────

    async def send_trade_lifecycle(
        self,
        symbol: str,
        direction: str,
        pattern: str,
        price: float,
        qty: float,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
        confidence: Optional[float] = None,
        order_id: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> Dict[str, bool]:
        """
        Send both a signal alert AND an execution alert in one call.

        This is the primary method to call when a filtered signal
        leads to an actual trade execution. It sends:
          1. Signal embed to #trade-signals
          2. Execution embed to #trade-executions

        Returns dict with {channel_name: success_bool}
        """
        results = {}

        # 1. Signal alert
        results["signals"] = await self.send_signal(
            symbol=symbol,
            direction=direction,
            pattern=pattern,
            price=price,
            confidence=confidence,
            regime=regime,
        )

        # 2. Execution alert
        results["executions"] = await self.send_execution(
            symbol=symbol,
            side="buy" if direction.upper() in ("LONG", "BUY") else "sell",
            qty=qty,
            price=price,
            sl_price=sl_price,
            tp_price=tp_price,
            order_id=order_id,
            status="submitted",
        )

        return results


# ── Helper ───────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Singleton ────────────────────────────────────────────────────────────

# Global singleton instance — import and use across the backend
discord_notifier = DiscordNotifier()
