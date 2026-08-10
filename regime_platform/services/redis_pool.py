"""
Shared Redis Connection Manager
══════════════════════════════

Centralized Redis connection pool for all services. Replaces the previous
pattern where each service created its own `redis.asyncio.from_url()` 
connection (6 separate TCP connections to the same Upstash instance).

v7.3: Single shared connection pool with max_connections=10.
All services acquire clients from this pool instead of creating their own.

Usage:
    from regime_platform.services.redis_pool import redis_pool

    client = await redis_pool.get_client()
    if client:
        await client.get("some_key")

Benefits:
    - Reduces TCP connection overhead (1 pool vs 6 connections)
    - Simplifies connection lifecycle management
    - Ensures consistent timeout/retry configuration
    - Reduces Upstash connection billing (some plans charge per connection)
"""

from __future__ import annotations

import logging
from typing import Optional

from regime_platform.core.settings import settings

logger = logging.getLogger("regime.redis_pool")


class RedisConnectionManager:
    """Shared Redis connection pool manager - singleton.

    All services should use `await redis_pool.get_client()` instead of
    creating their own `redis.asyncio.from_url()` connections.
    """

    def __init__(self):
        self._pool: Optional[object] = None
        self._enabled = False

    @property
    def enabled(self) -> bool:
        """True if Redis is configured and available."""
        return self._enabled

    async def get_client(self):
        """Get a Redis client from the shared connection pool.

        Returns None if Redis is not configured. The returned client
        is backed by the connection pool and should NOT be closed by
        callers - the pool manages the connection lifecycle.
        """
        if self._pool is None:
            await self._initialize()

        return self._pool if self._enabled else None

    async def _initialize(self):
        """Initialize the shared connection pool."""
        url = settings.redis_url_effective
        if not url:
            self._enabled = False
            return

        try:
            import redis.asyncio as aioredis
            self._pool = aioredis.from_url(
                url,
                decode_responses=True,
                # 2026-08-09: raised 10 → 16 after heavy-sweep concurrency
                # exhausted the pool (MaxConnectionsError). The heavy sweep
                # is now capped at HEAVY_SWEEP_MAX_CONCURRENCY=4 concurrent
                # symbols, and the light sweep runs at the same minute, so
                # 16 gives headroom for both + all other subsystems.
                max_connections=16,
                socket_timeout=5,
                socket_connect_timeout=5,
                socket_keepalive=True,
                retry_on_timeout=True,
            )
            # Verify the connection works
            await self._pool.ping()
            self._enabled = True
            logger.info("Redis pool: Shared connection pool initialized")
        except Exception as e:
            logger.error(f"Redis pool: Failed to initialize: {e}")
            self._enabled = False
            self._pool = None

    async def get_binary_client(self):
        """Get a Redis client configured for binary mode (gzip snapshots).

        Some services (RenkoRedisCache) need binary mode for gzip-compressed
        data. This returns a separate pool with decode_responses=False.
        """
        url = settings.redis_url_effective
        if not url:
            return None

        try:
            import redis.asyncio as aioredis
            return aioredis.from_url(
                url,
                decode_responses=False,
                max_connections=5,
                socket_keepalive=True,
                retry_on_timeout=True,
                socket_connect_timeout=2,
                socket_timeout=3,
            )
        except Exception as e:
            logger.error(f"Redis pool: Failed to create binary client: {e}")
            return None

    async def close(self):
        """Close the shared connection pool."""
        if self._pool is not None:
            try:
                await self._pool.aclose()
            except Exception:
                pass
            self._pool = None
            logger.info("Redis pool: Connection pool closed")


# Module-level singleton
redis_pool = RedisConnectionManager()
