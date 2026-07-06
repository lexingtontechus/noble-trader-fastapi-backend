"""
Unified Signal Aggregator Service
═══════════════════════════════════════════════════════════════════════════

Aggregates signals from multiple sources (Renko pipeline, Sweep Orchestrator)
and distributes them to appropriate notification channels (Discord, Telegram, Webhooks).

Architecture:
  - Redis Pub/Sub: Subscribe to "signal.raw.noble_trader" channel
  - Signal Processing: Normalize and route signals based on type and source
  - Multi-channel Distribution: Discord, Telegram, and external webhooks
  - Rate Limiting: Respect rate limits of each channel
  - Error Handling: Non-blocking with graceful degradation

Signal Types Supported:
  - renko_signal: Qualified signals from Renko HFT pipeline
  - heartbeat: Regime and trade signals from Sweep Orchestrator
  - pnl_alert: P&L alerts from risk management
  - system_status: System health and operational alerts

Usage::

    # Start the aggregator service
    aggregator = SignalAggregator()
    await aggregator.start()

    # The service will automatically:
    # 1. Subscribe to Redis pub/sub channel
    # 2. Process incoming signals
    # 3. Route to appropriate channels
    # 4. Handle rate limiting and errors
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

import redis.asyncio as redis
from regime_platform.core.settings import settings
from regime_platform.core.resilience import retry_with_backoff

# Import notification services (graceful import)
try:
    from notifications.discord import discord_notifier, AlertLevel as DiscordAlertLevel
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    discord_notifier = None
    DiscordAlertLevel = None

try:
    from notifications.telegram import telegram_notifier, AlertLevel as TelegramAlertLevel
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    telegram_notifier = None
    TelegramAlertLevel = None

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

log = logging.getLogger(__name__)


class SignalType(str, Enum):
    RENKO_SIGNAL = "renko_signal"
    HEARTBEAT = "heartbeat"
    PNL_ALERT = "pnl_alert"
    SYSTEM_STATUS = "system_status"


class SignalPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class SignalData:
    """Normalized signal data for processing."""
    type: SignalType
    source: str
    symbol: str
    timestamp: float
    priority: SignalPriority
    data: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "source": self.source,
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "priority": self.priority.value,
            **self.data
        }


@dataclass
class SignalRouterConfig:
    """Configuration for signal routing."""
    # Discord routing
    discord_enabled: bool = True
    discord_webhook_signals: Optional[str] = None
    discord_webhook_executions: Optional[str] = None
    discord_webhook_status: Optional[str] = None
    
    # Telegram routing
    telegram_enabled: bool = True
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    
    # External webhook routing
    external_webhooks_enabled: bool = True
    external_webhook_urls: list[str] = field(default_factory=list)
    
    # Rate limiting
    discord_rate_limit_per_second: int = 5
    telegram_rate_limit_per_minute: int = 20
    webhook_rate_limit_per_minute: int = 60
    
    # Signal type routing
    route_renko_to_discord: bool = True
    route_renko_to_telegram: bool = True
    route_heartbeat_to_discord: bool = True
    route_pnl_to_telegram: bool = True
    route_system_status_to_all: bool = True


class RateLimiter:
    """Simple sliding window rate limiter for each channel."""
    
    def __init__(self, max_per_window: int, window_seconds: float):
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self._timestamps: list[float] = field(default_factory=list)
    
    def can_send(self) -> bool:
        """Check if we can send without exceeding rate limit."""
        now = time.monotonic()
        # Remove timestamps outside the window
        cutoff = now - self.window_seconds
        self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
        return len(self._timestamps) < self.max_per_window
    
    def record_send(self) -> None:
        """Record that a send occurred."""
        now = time.monotonic()
        self._timestamps.append(now)


class SignalAggregator:
    """Unified signal aggregator and router."""
    
    def __init__(self, config: Optional[SignalRouterConfig] = None):
        self.config = config or self._load_config()
        self.redis_client: Optional[redis.Redis] = None
        self.redis_channel = "signal.raw.noble_trader"
        self._running = False
        
        # Rate limiters for each channel
        self._discord_rate_limiter = RateLimiter(
            self.config.discord_rate_limit_per_second,
            1.0  # 1 second window
        )
        self._telegram_rate_limiter = RateLimiter(
            self.config.telegram_rate_limit_per_minute,
            60.0  # 1 minute window
        )
        self._webhook_rate_limiter = RateLimiter(
            self.config.webhook_rate_limit_per_minute,
            60.0  # 1 minute window
        )
    
    def _load_config(self) -> SignalRouterConfig:
        """Load configuration from settings."""
        return SignalRouterConfig(
            discord_enabled=settings.DISCORD_WEBHOOK_SIGNALS is not None,
            discord_webhook_signals=settings.DISCORD_WEBHOOK_SIGNALS,
            discord_webhook_executions=settings.DISCORD_WEBHOOK_EXECUTIONS,
            discord_webhook_status=settings.DISCORD_WEBHOOK_STATUS,
            telegram_enabled=settings.TELEGRAM_BOT_TOKEN is not None and settings.TELEGRAM_CHAT_ID is not None,
            telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
            telegram_chat_id=settings.TELEGRAM_CHAT_ID,
            external_webhooks_enabled=settings.EXTERNAL_SIGNAL_WEBHOOK_URL is not None,
            external_webhook_urls=[settings.EXTERNAL_SIGNAL_WEBHOOK_URL] if settings.EXTERNAL_SIGNAL_WEBHOOK_URL else [],
        )
    
    async def start(self) -> None:
        """Start the signal aggregator service."""
        if self._running:
            return
        
        log.info("Starting Signal Aggregator Service")
        
        # Initialize Redis client
        if settings.REDIS_URL or settings.UPSTASH_REDIS_REST_URL:
            self.redis_client = redis.from_url(
                settings.REDIS_URL or settings.UPSTASH_REDIS_REST_URL,
                decode_responses=False
            )
        else:
            raise RuntimeError("Redis URL is required for Signal Aggregator")
        
        # Start Redis subscription
        await self._start_redis_subscription()
        
        self._running = True
        log.info("Signal Aggregator Service started successfully")
    
    async def stop(self) -> None:
        """Stop the signal aggregator service."""
        if not self._running:
            return
        
        log.info("Stopping Signal Aggregator Service")
        
        if self.redis_client:
            await self.redis_client.aclose()
        
        self._running = False
        log.info("Signal Aggregator Service stopped")
    
    async def _start_redis_subscription(self) -> None:
        """Subscribe to Redis pub/sub channel."""
        if not self.redis_client:
            return
        
        pubsub = self.redis_client.pubsub()
        await pubsub.subscribe(self.redis_channel)
        
        log.info(f"Subscribed to Redis channel: {self.redis_channel}")
        
        # Start listening in background task
        asyncio.create_task(self._process_redis_messages(pubsub))
    
    async def _process_redis_messages(self, pubsub) -> None:
        """Process incoming Redis messages."""
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        signal_data = json.loads(message["data"])
                        await self._process_signal(signal_data)
                    except Exception as e:
                        log.error(f"Error processing Redis message: {e}")
                elif message["type"] == "subscribe":
                    log.info(f"Subscribed to channel: {message['channel']}")
        except Exception as e:
            log.error(f"Redis pub/sub error: {e}")
    
    async def _process_signal(self, raw_data: Dict[str, Any]) -> None:
        """Process incoming raw signal data and route it appropriately."""
        try:
            # Normalize signal data
            signal = self._normalize_signal(raw_data)
            log.debug(f"Processing signal: {signal.type.value} from {signal.source}")
            
            # Route based on signal type
            if signal.type == SignalType.RENKO_SIGNAL and self.config.route_renko_to_discord:
                await self._route_to_discord(signal)
                if self.config.route_renko_to_telegram:
                    await self._route_to_telegram(signal)
            
            elif signal.type == SignalType.HEARTBEAT and self.config.route_heartbeat_to_discord:
                await self._route_to_discord(signal)
            
            elif signal.type == SignalType.PNL_ALERT and self.config.route_pnl_to_telegram:
                await self._route_to_telegram(signal)
            
            elif signal.type == SignalType.SYSTEM_STATUS and self.config.route_system_status_to_all:
                await self._route_to_discord(signal)
                await self._route_to_telegram(signal)
            
            # Route to external webhooks
            if self.config.external_webhooks_enabled:
                await self._route_to_webhooks(signal)
            
        except Exception as e:
            log.error(f"Error processing signal: {e}")
    
    def _normalize_signal(self, raw_data: Dict[str, Any]) -> SignalData:
        """Normalize raw signal data into SignalData format."""
        signal_type = raw_data.get("type", "unknown")
        
        # Determine signal priority based on type and content
        if signal_type == "renko_signal":
            priority = SignalPriority.HIGH
            source = "renko_hft"
        elif signal_type == "heartbeat":
            priority = SignalPriority.MEDIUM
            source = "sweep_orchestrator"
        elif signal_type == "pnl_alert":
            priority = SignalPriority.CRITICAL if raw_data.get("severity") == "critical" else SignalPriority.HIGH
            source = "risk_management"
        else:
            priority = SignalPriority.LOW
            source = "unknown"
        
        return SignalData(
            type=SignalType(signal_type),
            source=source,
            symbol=raw_data.get("symbol", "UNKNOWN"),
            timestamp=raw_data.get("timestamp", time.time()),
            priority=priority,
            data=raw_data
        )
    
    async def _route_to_discord(self, signal: SignalData) -> None:
        """Route signal to Discord notifications."""
        if not DISCORD_AVAILABLE or not self.config.discord_enabled:
            return
        
        # Rate limit check
        if not self._discord_rate_limiter.can_send():
            log.warning("Discord rate limit reached, dropping message")
            return
        
        try:
            if signal.type == SignalType.RENKO_SIGNAL:
                await discord_notifier.send_signal(
                    symbol=signal.symbol,
                    direction=signal.data.get("direction", ""),
                    pattern=signal.data.get("pattern", ""),
                    price=signal.data.get("price", 0.0),
                    confidence=signal.data.get("confidence", 0.0),
                    brick_size=signal.data.get("brick_size", 0.0),
                    regime=signal.data.get("regime", "")
                )
            elif signal.type == SignalType.HEARTBEAT:
                level = DiscordAlertLevel.INFO
                if signal.data.get("regime_shift") == "true":
                    level = DiscordAlertLevel.WARNING
                
                await discord_notifier.send_status(
                    message=f"Regime update: {signal.symbol} → {signal.data.get('regime', 'unknown')}",
                    symbol=signal.symbol,
                    level=level,
                    data=signal.data
                )
            elif signal.type == SignalType.SYSTEM_STATUS:
                level = DiscordAlertLevel.INFO
                if signal.priority == SignalPriority.CRITICAL:
                    level = DiscordAlertLevel.CRITICAL
                elif signal.priority == SignalPriority.HIGH:
                    level = DiscordAlertLevel.ERROR
                
                await discord_notifier.send_status(
                    message=signal.data.get("message", ""),
                    symbol=signal.symbol,
                    level=level,
                    data=signal.data
                )
            
            self._discord_rate_limiter.record_send()
            
        except Exception as e:
            log.error(f"Error sending to Discord: {e}")
    
    async def _route_to_telegram(self, signal: SignalData) -> None:
        """Route signal to Telegram notifications."""
        if not TELEGRAM_AVAILABLE or not self.config.telegram_enabled:
            return
        
        # Rate limit check
        if not self._telegram_rate_limiter.can_send():
            log.warning("Telegram rate limit reached, dropping message")
            return
        
        try:
            if signal.type == SignalType.RENKO_SIGNAL:
                # Extended Telegram signal notification
                await self._send_telegram_signal(signal)
            elif signal.type == SignalType.PNL_ALERT:
                await telegram_notifier.send_pnl_alert(
                    metric=signal.data.get("metric", ""),
                    operator=signal.data.get("operator", ""),
                    threshold_value=signal.data.get("threshold_value", 0.0),
                    current_value=signal.data.get("current_value", 0.0),
                    severity=signal.data.get("severity", "")
                )
            elif signal.type == SignalType.SYSTEM_STATUS:
                level = TelegramAlertLevel.INFO
                if signal.priority == SignalPriority.CRITICAL:
                    level = TelegramAlertLevel.CRITICAL
                elif signal.priority == SignalPriority.HIGH:
                    level = TelegramAlertLevel.ERROR
                
                await telegram_notifier.send_status(
                    message=signal.data.get("message", ""),
                    level=level,
                    data=signal.data
                )
            
            self._telegram_rate_limiter.record_send()
            
        except Exception as e:
            log.error(f"Error sending to Telegram: {e}")
    
    async def _send_telegram_signal(self, signal: SignalData) -> None:
        """Send formatted signal notification to Telegram."""
        direction_emoji = {
            "LONG": "📈", "SHORT": "📉", "BUY": "📈", "SELL": "📉", "HOLD": "⏸️"
        }.get(signal.data.get("direction", "").upper(), "🔔")
        
        pattern_emoji = {
            "bull_run": "🐂", "bear_run": "🐻", "reversal": "🔄", 
            "consolidation": "⚖️", "breakout": "🚀", "no_pattern": "❓"
        }.get(signal.data.get("pattern", ""), "🔔")
        
        confidence_pct = signal.data.get("confidence", 0.0) * 100
        
        # Build HTML message
        message = f"""
{direction_emoji} <b>{signal.symbol} Signal</b>
{pattern_emoji} <i>{signal.data.get('pattern', 'Unknown Pattern')}</i>
💰 <b>Price:</b> ${signal.data.get('price', 0):.2f}
📊 <b>Confidence:</b> {confidence_pct:.0f}%
🎯 <b>Direction:</b> {signal.data.get('direction', 'Unknown')}
{'✅ Regime Aligned' if signal.data.get('regime_aligned', False) else '❌ Regime Not Aligned'}
{'💰 Size: $' + f'{signal.data.get("position_size_usd", 0):.0f}' if signal.data.get("position_size_usd", 0) > 0 else ''}
        """.strip()
        
        await telegram_notifier._send_html_message(message, parse_mode="HTML")
    
    async def _route_to_webhooks(self, signal: SignalData) -> None:
        """Route signal to external webhooks."""
        if not self.config.external_webhooks_enabled or not self.config.external_webhook_urls:
            return
        
        # Rate limit check
        if not self._webhook_rate_limiter.can_send():
            log.warning("Webhook rate limit reached, dropping message")
            return
        
        payload = {
            "source": "noble_trader",
            "signal_type": signal.type.value,
            "symbol": signal.symbol,
            "timestamp": signal.timestamp,
            "priority": signal.priority.value,
            "data": signal.data
        }
        
        # Send to all configured webhooks
        for url in self.config.external_webhook_urls:
            asyncio.create_task(self._send_webhook(url, payload))
        
        self._webhook_rate_limiter.record_send()
    
    async def _send_webhook(self, url: str, payload: Dict[str, Any]) -> None:
        """Send payload to external webhook."""
        if not HTTPX_AVAILABLE:
            log.warning("HTTPX not available, skipping webhook")
            return
        
        async def _do_send():
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return response
        
        try:
            await _do_send()
            log.debug(f"Webhook sent successfully to {url}")
        except Exception as e:
            log.error(f"Failed to send webhook to {url}: {e}")
            raise


# Global instance for use in the application
signal_aggregator: Optional[SignalAggregator] = None


async def get_signal_aggregator() -> SignalAggregator:
    """Get or create the global signal aggregator instance."""
    global signal_aggregator
    
    if signal_aggregator is None:
        signal_aggregator = SignalAggregator()
        await signal_aggregator.start()
    
    return signal_aggregator


def create_signal_aggregator(config: Optional[SignalRouterConfig] = None) -> SignalAggregator:
    """Create a new signal aggregator instance."""
    return SignalAggregator(config)