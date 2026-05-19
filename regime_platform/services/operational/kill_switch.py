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

In-memory cache for zero-latency checks in the hot path
(auto-refreshed from Redis every 5 seconds).
"""

from __future__ import annotations
import json
import logging
import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel

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
    """Redis-backed kill switch with in-memory cache for hot-path checks."""

    GLOBAL_KEY = "noble:halt:global"
    USER_KEY_PREFIX = "noble:halt:user:"
    SYMBOL_KEY_PREFIX = "noble:halt:symbol:"

    def __init__(self):
        self._redis = None
        self._cache: dict[str, Optional[HaltRecord]] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False

    async def _get_client(self):
        """Lazy Redis connection (same pattern as redis_persistence.py)."""
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                from regime_platform.core.settings import settings
                if settings.REDIS_URL:
                    self._redis = aioredis.from_url(
                        settings.REDIS_URL,
                        decode_responses=True,
                        socket_timeout=5,
                        socket_connect_timeout=5,
                    )
                    logger.info("KillSwitch: Redis client connected")
                else:
                    logger.warning("KillSwitch: No REDIS_URL, using in-memory only")
            except Exception as e:
                logger.error(f"KillSwitch: Redis connection failed: {e}")
        return self._redis

    async def start_refresh(self):
        """Start background cache refresh loop (called during app lifespan startup)."""
        if self._running:
            return
        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info("KillSwitch: Background refresh started")

    async def stop_refresh(self):
        """Stop background refresh and close Redis (called during app lifespan shutdown)."""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass
            self._redis = None
        logger.info("KillSwitch: Stopped")

    async def _refresh_loop(self):
        """Refresh in-memory cache from Redis every 5 seconds."""
        while self._running:
            try:
                await self._refresh_cache()
            except Exception as e:
                logger.error(f"KillSwitch: Cache refresh error: {e}")
            await asyncio.sleep(5)

    async def _refresh_cache(self):
        """Read all halt states from Redis into in-memory cache."""
        client = await self._get_client()
        if not client:
            return
        try:
            # Global halt
            global_data = await client.get(self.GLOBAL_KEY)
            self._cache[self.GLOBAL_KEY] = (
                HaltRecord.model_validate_json(global_data) if global_data else None
            )

            # User halts - scan for noble:halt:user:*
            cursor = 0
            while True:
                cursor, keys = await client.scan(
                    cursor, match=f"{self.USER_KEY_PREFIX}*", count=100
                )
                for key in keys:
                    data = await client.get(key)
                    self._cache[key] = (
                        HaltRecord.model_validate_json(data) if data else None
                    )
                if cursor == 0:
                    break

            # Symbol halts - scan for noble:halt:symbol:*
            cursor = 0
            while True:
                cursor, keys = await client.scan(
                    cursor, match=f"{self.SYMBOL_KEY_PREFIX}*", count=100
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

    # ── Hot-path check (uses in-memory cache, sub-millisecond) ──

    async def is_halted(
        self,
        user_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> bool:
        """
        Check if trading is halted. O(1) from in-memory cache.
        
        Checks in order: global > user > symbol.
        If ANY tier is halted, returns True.
        """
        # Global halt
        if self._cache.get(self.GLOBAL_KEY) is not None:
            return True

        # User halt
        if user_id:
            user_key = f"{self.USER_KEY_PREFIX}{user_id}"
            if self._cache.get(user_key) is not None:
                return True

        # Symbol halt
        if symbol:
            sym_key = f"{self.SYMBOL_KEY_PREFIX}{symbol.upper()}"
            if self._cache.get(sym_key) is not None:
                return True

        return False

    async def get_active_halt(
        self,
        user_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Optional[HaltRecord]:
        """Get the most specific active halt record (for error messages)."""
        # Check in priority order: global > user > symbol
        global_halt = self._cache.get(self.GLOBAL_KEY)
        if global_halt:
            return global_halt

        if user_id:
            user_halt = self._cache.get(f"{self.USER_KEY_PREFIX}{user_id}")
            if user_halt:
                return user_halt

        if symbol:
            sym_halt = self._cache.get(f"{self.SYMBOL_KEY_PREFIX}{symbol.upper()}")
            if sym_halt:
                return sym_halt

        return None

    # ── Activate / Deactivate (writes to Redis + cache) ──

    async def activate_halt(
        self,
        level: HaltLevel,
        reason: HaltReason,
        triggered_by: str,
        scope: Optional[str] = None,
        notes: Optional[str] = None,
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

        key = self._get_key(level, scope)
        record_json = record.model_dump_json()

        # Write to Redis
        client = await self._get_client()
        if client:
            try:
                await client.set(key, record_json)
                logger.warning(f"KillSwitch: HALT ACTIVATED — {level.value} scope={scope} reason={reason.value} by={triggered_by}")
            except Exception as e:
                logger.error(f"KillSwitch: Failed to write halt to Redis: {e}")

        # Update in-memory cache immediately
        self._cache[key] = record

        return record

    async def deactivate_halt(
        self,
        level: HaltLevel,
        scope: Optional[str] = None,
        deactivated_by: str = "unknown",
    ) -> bool:
        """Deactivate a halt at the specified level."""
        key = self._get_key(level, scope)

        # Remove from Redis
        client = await self._get_client()
        if client:
            try:
                await client.delete(key)
                logger.info(f"KillSwitch: HALT DEACTIVATED — {level.value} scope={scope} by={deactivated_by}")
            except Exception as e:
                logger.error(f"KillSwitch: Failed to remove halt from Redis: {e}")

        # Update in-memory cache
        self._cache[key] = None
        return True

    async def get_all_active_halts(self) -> list[HaltRecord]:
        """Get all active halt records."""
        return [v for v in self._cache.values() if v is not None]

    # ── Emergency actions ──

    async def cancel_all_orders(self, user_id: str) -> dict:
        """
        Cancel all open orders for a user via AlpacaExecutor.
        Returns dict with count of cancelled orders.
        """
        cancelled = []
        try:
            from renko.executor import AlpacaExecutor
            executor = AlpacaExecutor()
            # Get open orders from Alpaca
            if executor.api:
                orders = executor.api.list_orders(status="open")
                for order in orders:
                    try:
                        executor.api.cancel_order(order.id)
                        cancelled.append(order.id)
                    except Exception as e:
                        logger.error(f"KillSwitch: Failed to cancel order {order.id}: {e}")
        except Exception as e:
            logger.error(f"KillSwitch: cancel_all_orders failed: {e}")

        return {"cancelled_count": len(cancelled), "order_ids": cancelled}

    async def close_all_positions(self, user_id: str) -> dict:
        """
        Close all open positions for a user via AlpacaExecutor.
        Returns dict with count of closed positions.
        """
        closed = []
        try:
            from renko.executor import AlpacaExecutor
            executor = AlpacaExecutor()
            if executor.api:
                positions = executor.api.list_positions()
                for pos in positions:
                    try:
                        executor.api.close_position(pos.symbol)
                        closed.append(pos.symbol)
                    except Exception as e:
                        logger.error(f"KillSwitch: Failed to close position {pos.symbol}: {e}")
        except Exception as e:
            logger.error(f"KillSwitch: close_all_positions failed: {e}")

        return {"closed_count": len(closed), "symbols": closed}

    # ── Helpers ──

    def _get_key(self, level: HaltLevel, scope: Optional[str] = None) -> str:
        if level == HaltLevel.GLOBAL_HALT:
            return self.GLOBAL_KEY
        elif level == HaltLevel.USER_HALT:
            return f"{self.USER_KEY_PREFIX}{scope}"
        elif level == HaltLevel.SYMBOL_HALT:
            return f"{self.SYMBOL_KEY_PREFIX}{scope.upper()}"
        return self.GLOBAL_KEY


# Module-level singleton
kill_switch = KillSwitchService()
