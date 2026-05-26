"""
Kill Switch Service — Three-tier emergency halt system.

Tiers:
1. GLOBAL_HALT — Stops ALL trading across all users, all symbols
2. USER_HALT   — Stops trading for a specific user  
3. SYMBOL_HALT — Stops trading for a specific symbol

State is stored in Redis for cross-process persistence:
- noble:halt:global         → JSON HaltRecord or None
- noble:halt:user:{user_id} → JSON HaltRecord or None
- noble:halt:symbol:{sym}   → JSON HaltRecord or None

In-memory cache for zero-latency checks in the hot path.
v7.3: Replaced 5-second polling with Redis pub/sub + 60s fallback.
      Reduces Redis commands from ~69K/day to ~1.4K/day (98% reduction).
"""

from __future__ import annotations
import json
import logging
import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel

from regime_platform.core.tenant import tenant_key

logger = logging.getLogger(__name__)


class HaltLevel(str, Enum):
    GLOBAL_HALT = "global_halt"
    USER_HALT = "user_halt"
    SYMBOL_HALT = "symbol_halt"


class HaltReason(str, Enum):
    MANUAL = "manual"
    CIRCUIT_BREAKER = "circuit_breaker"
    MAX_DRAWDOWN = "max_drawdown"
    DATA_FEED_ERROR = "data_feed_error"
    COMPLIANCE = "compliance"
    RECONCILIATION_FAILURE = "reconciliation_failure"


class HaltRecord(BaseModel):
    level: HaltLevel
    reason: HaltReason
    triggered_by: str           # user_id or "system"
    triggered_at: datetime
    scope: Optional[str] = None  # user_id or symbol
    notes: Optional[str] = None


class KillSwitchService:
    """Redis-backed kill switch with in-memory cache for hot-path checks.

    v7.3: Uses Redis pub/sub for event-driven cache updates instead of
    5-second polling. A 60-second fallback polling interval is retained
    as a health check to detect missed pub/sub messages.
    """

    GLOBAL_BASE_KEY = "noble:halt:global"
    USER_BASE_KEY = "noble:halt:user"
    SYMBOL_BASE_KEY = "noble:halt:symbol"
    PUBSUB_CHANNEL = "noble:halt:updates"
    FALLBACK_POLL_INTERVAL = 60  # seconds (was 5)

    def __init__(self):
        self._redis = None
        self._pubsub_client = None  # Separate client for pub/sub (required by redis-py)
        self._pubsub = None
        self._cache: dict[str, Optional[HaltRecord]] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._pubsub_task: Optional[asyncio.Task] = None
        self._running = False

    async def _get_client(self):
        """Get Redis client from the shared connection pool.

        v7.3: Uses the shared RedisConnectionManager instead of creating
        a per-service TCP connection. Reduces from 6 connections to 1 pool.
        """
        if self._redis is None:
            try:
                from regime_platform.services.redis_pool import redis_pool
                self._redis = await redis_pool.get_client()
                if self._redis:
                    logger.info("KillSwitch: Using shared Redis pool")
                else:
                    logger.warning("KillSwitch: No Redis available, using in-memory only")
            except Exception as e:
                logger.error(f"KillSwitch: Redis pool connection failed: {e}")
        return self._redis

    async def _get_pubsub_client(self, *, force_new: bool = False):
        """Get or create a separate Redis client for pub/sub.

        redis-py requires a dedicated client for pub/sub because the
        client enters a subscription mode where it can only send
        SUBSCRIBE/UNSUBSCRIBE/PING commands. This client is NOT from the
        shared pool — it's a standalone connection.

        Args:
            force_new: If True, close and discard the existing client
                and create a fresh one. Used during reconnection after
                a connection drop.
        """
        if force_new and self._pubsub_client is not None:
            try:
                await self._pubsub_client.aclose()
            except Exception:
                pass
            self._pubsub_client = None
            self._pubsub = None

        if self._pubsub_client is not None:
            return self._pubsub_client
        try:
            import redis.asyncio as aioredis
            from regime_platform.core.settings import settings
            url = settings.redis_url_effective
            if url:
                self._pubsub_client = aioredis.from_url(
                    url,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                )
                return self._pubsub_client
        except Exception as e:
            logger.error(f"KillSwitch: Pub/sub client connection failed: {e}")
        return None

    async def start_refresh(self):
        """Start event-driven cache refresh with pub/sub + fallback polling."""
        if self._running:
            return
        self._running = True

        # Initial cache load on startup
        try:
            await self._refresh_cache()
            logger.info("KillSwitch: Initial cache loaded from Redis")
        except Exception as e:
            logger.warning(f"KillSwitch: Initial cache load failed: {e}")

        # Start pub/sub listener (primary update mechanism)
        self._pubsub_task = asyncio.create_task(self._pubsub_listener())
        logger.info("KillSwitch: Pub/sub listener started")

        # Start fallback polling (60s interval — health check for missed messages)
        self._refresh_task = asyncio.create_task(self._fallback_poll_loop())
        logger.info(f"KillSwitch: Fallback polling started ({self.FALLBACK_POLL_INTERVAL}s interval)")

    async def stop_refresh(self):
        """Stop background tasks and close Redis connections."""
        self._running = False

        # Cancel pub/sub listener
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass

        # Cancel fallback polling
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

        # Unsubscribe and close pub/sub
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

        if self._pubsub_client:
            try:
                await self._pubsub_client.aclose()
            except Exception:
                pass
            self._pubsub_client = None

        # Note: self._redis is from the shared pool — don't close it here.
        # The pool is closed during app shutdown in main.py.
        self._redis = None

        logger.info("KillSwitch: Stopped")

    async def _pubsub_listener(self):
        """Listen for halt state change notifications via Redis pub/sub.

        When activate_halt() or deactivate_halt() is called (in any process),
        it PUBLISHes to the noble:halt:updates channel. This listener receives
        the notification and refreshes the in-memory cache instantly, instead
        of waiting for the next polling cycle.

        v7.4: Robust reconnection — properly cleans up stale pub/sub objects
        and creates fresh connections on error instead of reusing dead ones.
        Exponential backoff prevents crash loops on persistent failures.
        """
        consecutive_errors = 0

        while self._running:
            try:
                # Force a fresh client on reconnect (after first attempt)
                pubsub_client = await self._get_pubsub_client(
                    force_new=(consecutive_errors > 0)
                )
                if not pubsub_client:
                    await self._backoff(consecutive_errors)
                    consecutive_errors += 1
                    continue

                # Clean up previous pubsub object if any
                if self._pubsub is not None:
                    try:
                        await self._pubsub.unsubscribe()
                        await self._pubsub.close()
                    except Exception:
                        pass
                    self._pubsub = None

                self._pubsub = pubsub_client.pubsub()
                await self._pubsub.subscribe(self.PUBSUB_CHANNEL)
                logger.info(f"KillSwitch: Subscribed to {self.PUBSUB_CHANNEL}")

                # Reset error counter on successful subscribe
                consecutive_errors = 0

                # Listen for messages
                while self._running:
                    message = await self._pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message and message.get("type") == "message":
                        data = message.get("data")
                        if data:
                            try:
                                payload = json.loads(data)
                                action = payload.get("action")
                                key = payload.get("key")
                                logger.info(
                                    f"KillSwitch: Pub/sub notification — "
                                    f"action={action}, key={key}"
                                )
                                # Refresh cache on any state change
                                await self._refresh_cache()
                            except (json.JSONDecodeError, KeyError) as e:
                                logger.warning(f"KillSwitch: Invalid pub/sub message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    f"KillSwitch: Pub/sub listener error: {e} "
                    f"(reconnect attempt #{consecutive_errors})"
                )
                # Clean up the broken pubsub before reconnecting
                if self._pubsub is not None:
                    try:
                        await self._pubsub.unsubscribe()
                        await self._pubsub.close()
                    except Exception:
                        pass
                    self._pubsub = None

                if self._running:
                    await self._backoff(consecutive_errors)

        # Final cleanup on exit
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

    async def _backoff(self, error_count: int):
        """Exponential backoff with cap — prevents crash loops.

        5s → 10s → 20s → 40s → 60s (max)
        """
        delay = min(5 * (2 ** min(error_count, 4)), 60)
        logger.info(f"KillSwitch: Reconnecting in {delay}s")
        await asyncio.sleep(delay)

    async def _fallback_poll_loop(self):
        """Fallback polling loop — refreshes cache every 60 seconds.

        This is a safety net in case pub/sub messages are missed due to
        network partitions or Redis reconnections. At 60s intervals, this
        generates only ~1,440 commands/day vs the previous ~69,120/day.
        """
        while self._running:
            try:
                await asyncio.sleep(self.FALLBACK_POLL_INTERVAL)
                await self._refresh_cache()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"KillSwitch: Fallback poll error: {e}")

    async def _refresh_cache(
        self,
        org_id: Optional[str] = None,
    ):
        """Read all halt states from Redis into in-memory cache."""
        client = await self._get_client()
        if not client:
            return
        try:
            # Global halt
            global_key = tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
            global_data = await client.get(global_key)
            self._cache[global_key] = (
                HaltRecord.model_validate_json(global_data) if global_data else None
            )

            # User halts - scan for noble:halt:user*
            user_prefix = tenant_key(self.USER_BASE_KEY, org_id=org_id)
            cursor = 0
            while True:
                cursor, keys = await client.scan(
                    cursor, match=f"{user_prefix}*", count=100
                )
                for key in keys:
                    data = await client.get(key)
                    self._cache[key] = (
                        HaltRecord.model_validate_json(data) if data else None
                    )
                if cursor == 0:
                    break

            # Symbol halts - scan for noble:halt:symbol*
            symbol_prefix = tenant_key(self.SYMBOL_BASE_KEY, org_id=org_id)
            cursor = 0
            while True:
                cursor, keys = await client.scan(
                    cursor, match=f"{symbol_prefix}*", count=100
                )
                for key in keys:
                    data = await client.get(key)
                    self._cache[key] = (
                        HaltRecord.model_validate_json(data) if data else None
                    )
                if cursor == 0:
                    break

        except Exception as e:
            logger.error(f"KillSwitch: Cache refresh failed: {e}")

    async def _publish_update(self, action: str, key: str):
        """Publish a halt state change notification to the pub/sub channel.

        This is called after activate_halt() or deactivate_halt() so that
        all subscribed processes (including other uvicorn workers) can
        immediately update their in-memory cache without waiting for polling.
        """
        try:
            client = await self._get_client()
            if client:
                payload = json.dumps({"action": action, "key": key})
                await client.publish(self.PUBSUB_CHANNEL, payload)
        except Exception as e:
            logger.debug(f"KillSwitch: Pub/sub publish failed (non-critical): {e}")

    # ── Hot-path check (uses in-memory cache, sub-millisecond) ──

    async def is_halted(
        self,
        user_id: Optional[str] = None,
        symbol: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> bool:
        """
        Check if trading is halted. O(1) from in-memory cache.
        
        Checks in order: global > user > symbol.
        If ANY tier is halted, returns True.
        """
        # Global halt
        global_key = tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
        if self._cache.get(global_key) is not None:
            return True

        # User halt
        if user_id:
            user_key = tenant_key(f"{self.USER_BASE_KEY}:{user_id}", org_id=org_id, user_id=user_id if not org_id else None)
            if self._cache.get(user_key) is not None:
                return True

        # Symbol halt
        if symbol:
            sym_key = tenant_key(f"{self.SYMBOL_BASE_KEY}:{symbol.upper()}", org_id=org_id)
            if self._cache.get(sym_key) is not None:
                return True

        return False

    async def get_active_halt(
        self,
        user_id: Optional[str] = None,
        symbol: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> Optional[HaltRecord]:
        """Get the most specific active halt record (for error messages)."""
        # Check in priority order: global > user > symbol
        global_key = tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
        global_halt = self._cache.get(global_key)
        if global_halt:
            return global_halt

        if user_id:
            user_key = tenant_key(f"{self.USER_BASE_KEY}:{user_id}", org_id=org_id, user_id=user_id if not org_id else None)
            user_halt = self._cache.get(user_key)
            if user_halt:
                return user_halt

        if symbol:
            sym_key = tenant_key(f"{self.SYMBOL_BASE_KEY}:{symbol.upper()}", org_id=org_id)
            sym_halt = self._cache.get(sym_key)
            if sym_halt:
                return sym_halt

        return None

    # ── Activate / Deactivate (writes to Redis + cache + pub/sub) ──

    async def activate_halt(
        self,
        level: HaltLevel,
        reason: HaltReason,
        triggered_by: str,
        scope: Optional[str] = None,
        notes: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> HaltRecord:
        """Activate a halt at the specified level."""
        record = HaltRecord(
            level=level,
            reason=reason,
            triggered_by=triggered_by,
            triggered_at=datetime.now(timezone.utc),
            scope=scope,
            notes=notes,
        )

        key = self._get_key(level, scope, org_id=org_id)
        record_json = record.model_dump_json()

        # Write to Redis
        client = await self._get_client()
        if client:
            try:
                await client.set(key, record_json)
                logger.warning(f"KillSwitch: HALT ACTIVATED — {level.value} scope={scope} reason={reason.value} by={triggered_by} org={org_id}")
            except Exception as e:
                logger.error(f"KillSwitch: Failed to write halt to Redis: {e}")

        # Update in-memory cache immediately
        self._cache[key] = record

        # Notify other processes via pub/sub
        await self._publish_update("activate", key)

        return record

    async def deactivate_halt(
        self,
        level: HaltLevel,
        scope: Optional[str] = None,
        deactivated_by: str = "unknown",
        org_id: Optional[str] = None,
    ) -> bool:
        """Deactivate a halt at the specified level."""
        key = self._get_key(level, scope, org_id=org_id)

        # Remove from Redis
        client = await self._get_client()
        if client:
            try:
                await client.delete(key)
                logger.info(f"KillSwitch: HALT DEACTIVATED — {level.value} scope={scope} by={deactivated_by} org={org_id}")
            except Exception as e:
                logger.error(f"KillSwitch: Failed to remove halt from Redis: {e}")

        # Update in-memory cache
        self._cache[key] = None

        # Notify other processes via pub/sub
        await self._publish_update("deactivate", key)

        return True

    async def get_all_active_halts(self) -> list[HaltRecord]:
        """Get all active halt records."""
        return [v for v in self._cache.values() if v is not None]

    # ── Emergency actions ──

    async def cancel_all_orders(self, user_id: str) -> dict:
        """
        Cancel all open orders for a user via ExecutorFactory.
        Returns dict with count of cancelled orders.
        """
        cancelled = []
        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()
            if api:
                orders = api.list_orders(status="open")
                for order in orders:
                    try:
                        api.cancel_order(order.id)
                        cancelled.append(order.id)
                        logger.info(f"KillSwitch: Cancelled order {order.id}")
                    except Exception as e:
                        logger.error(f"KillSwitch: Failed to cancel order {order.id}: {e}")
            else:
                logger.warning("KillSwitch: No Alpaca API client — cancel_all_orders is no-op (simulation mode)")
        except Exception as e:
            logger.error(f"KillSwitch: cancel_all_orders failed: {e}")

        return {"cancelled_count": len(cancelled), "order_ids": cancelled}

    async def close_all_positions(self, user_id: str) -> dict:
        """
        Close all open positions for a user via ExecutorFactory.
        Returns dict with count of closed positions.
        """
        closed = []
        try:
            from regime_platform.services.operational.executor_factory import executor_factory
            api = executor_factory.get_alpaca_rest()
            if api:
                positions = api.list_positions()
                for pos in positions:
                    try:
                        api.close_position(pos.symbol)
                        closed.append(pos.symbol)
                        logger.info(f"KillSwitch: Closed position {pos.symbol}")
                    except Exception as e:
                        logger.error(f"KillSwitch: Failed to close position {pos.symbol}: {e}")
            else:
                logger.warning("KillSwitch: No Alpaca API client — close_all_positions is no-op (simulation mode)")
        except Exception as e:
            logger.error(f"KillSwitch: close_all_positions failed: {e}")

        return {"closed_count": len(closed), "symbols": closed}

    # ── Helpers ──

    def _get_key(self, level: HaltLevel, scope: Optional[str] = None, org_id: Optional[str] = None) -> str:
        if level == HaltLevel.GLOBAL_HALT:
            return tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)
        elif level == HaltLevel.USER_HALT:
            return tenant_key(f"{self.USER_BASE_KEY}:{scope}", org_id=org_id, user_id=scope if not org_id else None)
        elif level == HaltLevel.SYMBOL_HALT:
            return tenant_key(f"{self.SYMBOL_BASE_KEY}:{scope.upper()}", org_id=org_id)
        return tenant_key(self.GLOBAL_BASE_KEY, org_id=org_id)


# Module-level singleton
kill_switch = KillSwitchService()
