"""
External Webhook Service
═══════════════════════════════════════════════════════════════════════════

Dedicated service for forwarding Redis signals to external webhooks with advanced features:

Features:
  - Multi-URL support: Forward to multiple webhook endpoints
  - Payload transformation: Customize payload format per destination
  - Authentication support: Basic Auth, Bearer tokens, custom headers
  - Retry logic with exponential backoff
  - Webhook health monitoring and status tracking
  - Payload validation and error handling
  - Webhook queuing for failed deliveries

Usage::

    # Start the external webhook service
    webhook_service = ExternalWebhookService()
    await webhook_service.start()

    # The service will:
    # 1. Subscribe to Redis pub/sub channel "signal.raw.noble_trader"
    # 2. Process signals through configured transformers
    # 3. Forward to configured webhook endpoints
    # 4. Handle retries and error recovery
    # 5. Monitor webhook health and performance
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable

import redis.asyncio as redis
from regime_platform.core.settings import settings
from regime_platform.core.resilience import retry_with_backoff

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None

try:
    from pydantic import BaseModel, Field
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    BaseModel = None
    Field = None

log = logging.getLogger(__name__)


class WebhookStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    RETRYING = "retrying"


class WebhookAuthentication(str, Enum):
    NONE = "none"
    BASIC = "basic"
    BEARER = "bearer"
    CUSTOM = "custom"


@dataclass
class WebhookEndpoint:
    """Configuration for a webhook endpoint."""
    url: str
    name: str
    enabled: bool = True
    authentication: WebhookAuthentication = WebhookAuthentication.NONE
    auth_credentials: Optional[Dict[str, str]] = None
    custom_headers: Optional[Dict[str, str]] = None
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_delay_seconds: int = 5
    payload_transformer: Optional[str] = None  # Function name to transform payload
    health_check_enabled: bool = True
    health_check_interval_hours: int = 24
    
    # Status tracking
    last_status: WebhookStatus = WebhookStatus.PENDING
    last_sent_at: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_sent: int = 0
    total_failed: int = 0


@dataclass
class WebhookDelivery:
    """Track individual webhook delivery attempts."""
    endpoint_name: str
    payload: Dict[str, Any]
    timestamp: float
    status: WebhookStatus
    attempt_number: int
    response_code: Optional[int] = None
    error_message: Optional[str] = None
    delivery_id: str = field(default_factory=lambda: str(int(time.time() * 1000000)))


class PayloadTransformers:
    """Collection of payload transformation functions."""
    
    @staticmethod
    def default_transformer(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Default transformer - pass through with minimal changes."""
        return {
            "timestamp": payload.get("timestamp", time.time()),
            "source": "noble_trader",
            "signal": payload
        }
    
    @staticmethod
    def slack_transformer(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Transform payload for Slack compatibility."""
        signal_type = payload.get("type", "unknown")
        symbol = payload.get("symbol", "UNKNOWN")
        
        if signal_type == "renko_signal":
            return {
                "text": f"🔔 {symbol} Trading Signal",
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f"🔔 {symbol} Trading Signal"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Pattern:* {payload.get('pattern', 'Unknown')}"},
                            {"type": "mrkdwn", "text": f"*Direction:* {payload.get('direction', 'Unknown')}"},
                            {"type": "mrkdwn", "text": f"*Price:* ${payload.get('price', 0):.2f}"},
                            {"type": "mrkdwn", "text": f"*Confidence:* {payload.get('confidence', 0) * 100:.0f}%"},
                        ]
                    }
                ]
            }
        elif signal_type == "heartbeat":
            return {
                "text": f"📊 {symbol} Regime Update",
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f"📊 {symbol} Regime Update"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Regime:* {payload.get('regime', 'Unknown')}"},
                            {"type": "mrkdwn", "text": f"*Kelly F:* {payload.get('kelly_f', 0):.3f}"},
                            {"type": "mrkdwn", "text": f"*EV:* {payload.get('ev', 0):.3f}"},
                        ]
                    }
                ]
            }
        else:
            return {
                "text": f"📡 {symbol} Signal from Noble Trader",
                "blocks": [
                    {
                        "type": "header",
                        "text": {"type": "plain_text", "text": f"📡 {symbol} Signal from Noble Trader"}
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Type:* {signal_type}"}
                    }
                ]
            }
    
    @staticmethod
    def teams_transformer(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Transform payload for Microsoft Teams compatibility."""
        signal_type = payload.get("type", "unknown")
        symbol = payload.get("symbol", "UNKNOWN")
        
        if signal_type == "renko_signal":
            return {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": f"{symbol} Trading Signal",
                "themeColor": "0076D7",
                "sections": [
                    {
                        "activityTitle": f"🔔 {symbol} Trading Signal",
                        "activitySubtitle": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "facts": [
                            {"name": "Pattern", "value": payload.get('pattern', 'Unknown')},
                            {"name": "Direction", "value": payload.get('direction', 'Unknown')},
                            {"name": "Price", "value": f"${payload.get('price', 0):.2f}"},
                            {"name": "Confidence", "value": f"{payload.get('confidence', 0) * 100:.0f}%"},
                        ]
                    }
                ]
            }
        elif signal_type == "heartbeat":
            return {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": f"{symbol} Regime Update",
                "themeColor": "0076D7",
                "sections": [
                    {
                        "activityTitle": f"📊 {symbol} Regime Update",
                        "activitySubtitle": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "facts": [
                            {"name": "Regime", "value": payload.get('regime', 'Unknown')},
                            {"name": "Kelly Fraction", "value": f"{payload.get('kelly_f', 0):.3f}"},
                            {"name": "Expected Value", "value": f"{payload.get('ev', 0):.3f}"},
                        ]
                    }
                ]
            }
        else:
            return {
                "@type": "MessageCard",
                "@context": "https://schema.org/extensions",
                "summary": f"{symbol} Signal from Noble Trader",
                "themeColor": "0076D7",
                "sections": [
                    {
                        "activityTitle": f"📡 {symbol} Signal from Noble Trader",
                        "activitySubtitle": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "facts": [
                            {"name": "Type", "value": signal_type},
                        ]
                    }
                ]
            }


class ExternalWebhookService:
    """Service for forwarding Redis signals to external webhooks."""
    
    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self.redis_channel = "signal.raw.noble_trader"
        self._running = False
        
        # Configuration
        self.endpoints: List[WebhookEndpoint] = []
        self.delivery_queue: List[WebhookDelivery] = []
        self._transformers = {
            "default": PayloadTransformers.default_transformer,
            "slack": PayloadTransformers.slack_transformer,
            "teams": PayloadTransformers.teams_transformer,
        }
        
        # Load configuration from settings
        self._load_config()
        
        # Background tasks
        self._health_check_task: Optional[asyncio.Task] = None
        self._retry_task: Optional[asyncio.Task] = None
    
    def _load_config(self) -> None:
        """Load configuration from environment settings."""
        # Load webhook endpoints from settings
        webhook_urls = getattr(settings, 'EXTERNAL_WEBHOOK_URLS', '')
        if webhook_urls:
            urls = [url.strip() for url in webhook_urls.split(',') if url.strip()]
            for i, url in enumerate(urls):
                self.endpoints.append(WebhookEndpoint(
                    url=url,
                    name=f"webhook_{i+1}",
                    enabled=True
                ))
        
        # Load individual endpoint configurations from settings
        for attr_name in dir(settings):
            if attr_name.startswith('EXTERNAL_WEBHOOK_'):
                if attr_name == 'EXTERNAL_WEBHOOK_URLS':
                    continue
                
                # Parse endpoint-specific config
                # Example: EXTERNAL_WEBHOOK_1_URL, EXTERNAL_WEBHOOK_1_AUTH, etc.
                endpoint_num = attr_name.split('_')[2] if '_' in attr_name else 'default'
                
                # Find or create endpoint
                endpoint = None
                for ep in self.endpoints:
                    if ep.name == f"webhook_{endpoint_num}":
                        endpoint = ep
                        break
                
                if not endpoint:
                    endpoint = WebhookEndpoint(
                        url="",
                        name=f"webhook_{endpoint_num}"
                    )
                    self.endpoints.append(endpoint)
                
                # Apply configuration
                if hasattr(settings, attr_name):
                    value = getattr(settings, attr_name)
                    if 'URL' in attr_name and value:
                        endpoint.url = value
                    elif 'ENABLED' in attr_name and isinstance(value, bool):
                        endpoint.enabled = value
                    elif 'AUTH' in attr_name and value:
                        if value.lower() == 'basic':
                            endpoint.authentication = WebhookAuthentication.BASIC
                        elif value.lower() == 'bearer':
                            endpoint.authentication = WebhookAuthentication.BEARER
                        elif value.lower() == 'custom':
                            endpoint.authentication = WebhookAuthentication.CUSTOM
                    elif 'HEADERS' in attr_name and value:
                        # Custom headers in format "Header1: Value1, Header2: Value2"
                        headers = {}
                        for header_pair in value.split(','):
                            if ':' in header_pair:
                                key, val = header_pair.split(':', 1)
                                headers[key.strip()] = val.strip()
                        endpoint.custom_headers = headers
                    elif 'RETRIES' in attr_name and isinstance(value, int):
                        endpoint.max_retries = value
                    elif 'TIMEOUT' in attr_name and isinstance(value, int):
                        endpoint.timeout_seconds = value
        
        # Filter out endpoints without URLs
        self.endpoints = [ep for ep in self.endpoints if ep.url]
    
    async def start(self) -> None:
        """Start the external webhook service."""
        if self._running:
            return
        
        log.info("Starting External Webhook Service")
        
        # Initialize Redis client
        if settings.REDIS_URL or settings.UPSTASH_REDIS_REST_URL:
            self.redis_client = redis.from_url(
                settings.REDIS_URL or settings.UPSTASH_REDIS_REST_URL,
                decode_responses=False
            )
        else:
            raise RuntimeError("Redis URL is required for External Webhook Service")
        
        # Start Redis subscription
        await self._start_redis_subscription()
        
        # Start background tasks
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        self._retry_task = asyncio.create_task(self._retry_loop())
        
        self._running = True
        log.info(f"External Webhook Service started with {len(self.endpoints)} endpoints")
    
    async def stop(self) -> None:
        """Stop the external webhook service."""
        if not self._running:
            return
        
        log.info("Stopping External Webhook Service")
        
        # Cancel background tasks
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        
        if self._retry_task:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
        
        # Close Redis client
        if self.redis_client:
            await self.redis_client.aclose()
        
        self._running = False
        log.info("External Webhook Service stopped")
    
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
    
    async def _process_signal(self, signal_data: Dict[str, Any]) -> None:
        """Process incoming signal and forward to enabled endpoints."""
        for endpoint in self.endpoints:
            if not endpoint.enabled:
                continue
            
            # Create delivery record
            delivery = WebhookDelivery(
                endpoint_name=endpoint.name,
                payload=signal_data,
                timestamp=time.time(),
                status=WebhookStatus.PENDING,
                attempt_number=1
            )
            
            self.delivery_queue.append(delivery)
            
            # Send immediately (non-blocking)
            asyncio.create_task(self._send_webhook(endpoint, delivery))
    
    @retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=10.0)
    async def _send_webhook(self, endpoint: WebhookEndpoint, delivery: WebhookDelivery) -> None:
        """Send webhook to the specified endpoint."""
        if not HTTPX_AVAILABLE:
            delivery.status = WebhookStatus.FAILED
            delivery.error_message = "HTTPX not available"
            return
        
        # Get transformer function
        transformer_func = self._transformers.get(endpoint.payload_transformer, 
                                                PayloadTransformers.default_transformer)
        try:
            transformed_payload = transformer_func(delivery.payload)
        except Exception as e:
            log.error(f"Payload transformation failed for {endpoint.name}: {e}")
            transformed_payload = delivery.payload
        
        # Prepare headers
        headers = {"Content-Type": "application/json"}
        if endpoint.custom_headers:
            headers.update(endpoint.custom_headers)
        
        # Add authentication
        if endpoint.authentication == WebhookAuthentication.BASIC and endpoint.auth_credentials:
            import base64
            credentials = f"{endpoint.auth_credentials['username']}:{endpoint.auth_credentials['password']}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        elif endpoint.authentication == WebhookAuthentication.BEARER and endpoint.auth_credentials:
            headers["Authorization"] = f"Bearer {endpoint.auth_credentials['token']}"
        elif endpoint.authentication == WebhookAuthentication.CUSTOM and endpoint.auth_credentials:
            headers.update(endpoint.auth_credentials)
        
        try:
            # Send webhook
            async with httpx.AsyncClient(timeout=endpoint.timeout_seconds) as client:
                response = await client.post(endpoint.url, json=transformed_payload, headers=headers)
                response.raise_for_status()
                
                # Update delivery status
                delivery.status = WebhookStatus.SENT
                delivery.response_code = response.status_code
                delivery.last_sent_at = time.time()
                endpoint.last_status = WebhookStatus.SENT
                endpoint.last_error = None
                endpoint.consecutive_failures = 0
                endpoint.total_sent += 1
                
                log.info(f"Webhook sent successfully to {endpoint.name}: {response.status_code}")
                
        except Exception as e:
            # Update delivery status
            delivery.status = WebhookStatus.FAILED
            delivery.error_message = str(e)
            
            endpoint.last_status = WebhookStatus.FAILED
            endpoint.last_error = str(e)
            endpoint.consecutive_failures += 1
            endpoint.total_failed += 1
            
            log.error(f"Webhook failed for {endpoint.name}: {e}")
            
            # Add to retry queue if retries available
            if delivery.attempt_number < endpoint.max_retries:
                delivery.status = WebhookStatus.RETRYING
                delivery.attempt_number += 1
                # Don't add to queue here, retry loop will pick it up
            else:
                log.error(f"Max retries reached for {endpoint.name}, giving up")
    
    async def _retry_loop(self) -> None:
        """Process retry queue for failed webhook deliveries."""
        while self._running:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                # Find pending and retrying deliveries
                retry_candidates = [
                    d for d in self.delivery_queue 
                    if d.status in (WebhookStatus.PENDING, WebhookStatus.RETRYING)
                ]
                
                for delivery in retry_candidates:
                    # Find the endpoint
                    endpoint = next((ep for ep in self.endpoints if ep.name == delivery.endpoint_name), None)
                    if not endpoint or not endpoint.enabled:
                        continue
                    
                    # Check if enough time has passed since last attempt
                    retry_delay = endpoint.retry_delay_seconds * (2 ** (delivery.attempt_number - 1))
                    if delivery.timestamp + retry_delay > time.time():
                        continue
                    
                    # Send webhook
                    asyncio.create_task(self._send_webhook(endpoint, delivery))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in retry loop: {e}")
    
    async def _health_check_loop(self) -> None:
        """Periodically check webhook endpoint health."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Check every hour
                
                for endpoint in self.endpoints:
                    if not endpoint.health_check_enabled:
                        continue
                    
                    # Don't check if recently failed
                    if (endpoint.last_status == WebhookStatus.FAILED and 
                        endpoint.last_error and 
                        time.time() - endpoint.last_error_timestamp < 300):  # 5 minutes
                        continue
                    
                    asyncio.create_task(self._perform_health_check(endpoint))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Error in health check loop: {e}")
    
    async def _perform_health_check(self, endpoint: WebhookEndpoint) -> None:
        """Perform health check on a specific endpoint."""
        if not HTTPX_AVAILABLE:
            return
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.head(endpoint.url, timeout=10.0)
                response.raise_for_status()
                
                log.debug(f"Health check passed for {endpoint.name}")
                
        except Exception as e:
            log.warning(f"Health check failed for {endpoint.name}: {e}")
            endpoint.last_status = WebhookStatus.FAILED
            endpoint.last_error = str(e)
    
    def get_endpoint_status(self) -> Dict[str, Any]:
        """Get status of all webhook endpoints."""
        return {
            endpoint.name: {
                "url": endpoint.url,
                "enabled": endpoint.enabled,
                "status": endpoint.last_status.value,
                "last_sent_at": endpoint.last_sent_at,
                "last_error": endpoint.last_error,
                "consecutive_failures": endpoint.consecutive_failures,
                "total_sent": endpoint.total_sent,
                "total_failed": endpoint.total_failed,
                "success_rate": (endpoint.total_sent / max(endpoint.total_sent + endpoint.total_failed, 1)) * 100
            }
            for endpoint in self.endpoints
        }


# Global instance for use in the application
external_webhook_service: Optional[ExternalWebhookService] = None


async def get_external_webhook_service() -> ExternalWebhookService:
    """Get or create the global external webhook service instance."""
    global external_webhook_service
    
    if external_webhook_service is None:
        external_webhook_service = ExternalWebhookService()
        await external_webhook_service.start()
    
    return external_webhook_service


def create_external_webhook_service() -> ExternalWebhookService:
    """Create a new external webhook service instance."""
    return ExternalWebhookService()