"""
Redis L1 Cache for Renko Pipeline Snapshots
════════════════════════════════════════════

Provides a fast Redis-backed caching layer for Renko pipeline snapshots,
sitting above the Supabase L2 (durable) store.

Architecture:
    L1: Redis   - sub-5ms reads, 4h TTL, compressed JSON
    L2: Supabase - ~100ms reads, durable, full history

Restore hierarchy (MCP agent perspective):
    1. Redis L1 hit → <5ms, instant hydration
    2. Supabase L2 hit → ~100ms, still fast hydration
    3. Yahoo fetch → 5-30s, full warmup cycle

Write path (dual-write):
    - On save: write Redis first (fast, non-blocking), then Supabase
    - On confirmed save: await both writes
    - Redis TTL matches Supabase freshness threshold (4h default)

v7.3 Changes:
    - Uses shared Redis pool for binary client (gzip snapshots)
    - Replaced KEYS with SCAN to avoid blocking Redis event loop
    - Consolidated snapshot save paths to eliminate duplicate writes

Key format:
    renko:snapshot:{symbol}:{brick_size}  → compressed snapshot JSON
    renko:snapshot:meta:{symbol}          → lightweight metadata (brick_count, has_position, etc.)

Error policy:
    Redis failures NEVER block the pipeline. If Redis is down, we fall
    back to Supabase directly. Redis is a performance optimization, not
    a dependency. All operations are wrapped in try/except.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from regime_platform.core.settings import settings

log = logging.getLogger(__name__)

# Redis key prefixes
SNAPSHOT_PREFIX = "renko:snapshot:"
META_PREFIX = "renko:snapshot:meta:"

# Default TTL: 4 hours (matches Supabase freshness threshold)
DEFAULT_TTL_SECONDS = 14400

# Max compressed snapshot size (10 MB - safety limit)
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024


class RenkoRedisCache:
    """
    Redis L1 cache for Renko pipeline snapshots.

    If REDIS_URL is not configured or the redis package is unavailable,
    all methods become no-ops and `enabled` is False. This ensures the
    pipeline works perfectly without Redis - just slower on cold starts.

    v7.3: Uses shared Redis pool for binary client. Replaced KEYS with SCAN.
    """

    def __init__(self, redis_url: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._redis = None
        self._enabled = False

        _url = redis_url or settings.redis_url_effective
        if not _url:
            log.info(
                "REDIS_URL not set - Renko L1 cache disabled "
                "(Supabase L2 only, ~100ms restore)"
            )
            return

        try:
            import redis.asyncio as aioredis
            self._client_cls = aioredis.from_url
            self._url = _url
            self._enabled = True
            log.info(f"Renko L1 cache configured (TTL={ttl_seconds}s)")
        except ImportError:
            log.warning("redis package not installed - Renko L1 cache disabled")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _get_client(self):
        """Lazy connection - created on first use.

        v7.3: Uses the shared Redis pool for the binary client.
        """
        if self._redis is None and self._enabled:
            try:
                from regime_platform.services.redis_pool import redis_pool
                self._redis = await redis_pool.get_binary_client()
                if self._redis is None:
                    # Fallback: create standalone binary client
                    import redis.asyncio as aioredis
                    self._redis = aioredis.from_url(
                        self._url,
                        decode_responses=False,  # Binary mode for gzip
                        socket_keepalive=True,
                        retry_on_timeout=True,
                        socket_connect_timeout=2,
                        socket_timeout=3,
                    )
            except Exception as e:
                log.warning(f"Renko L1: Failed to get binary client from pool: {e}")
                # Fallback: create standalone binary client
                try:
                    import redis.asyncio as aioredis
                    self._redis = aioredis.from_url(
                        self._url,
                        decode_responses=False,
                        socket_keepalive=True,
                        retry_on_timeout=True,
                        socket_connect_timeout=2,
                        socket_timeout=3,
                    )
                except Exception as e2:
                    log.error(f"Renko L1: Binary client creation failed: {e2}")
        return self._redis

    # ── Snapshot Operations ────────────────────────────────────────────

    async def save(
        self,
        symbol: str,
        brick_size: float,
        snapshot: dict,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """
        Save a pipeline snapshot to Redis L1 cache.

        The snapshot is gzip-compressed to reduce Redis memory usage.
        A lightweight metadata entry is also stored for quick lookups
        without deserializing the full snapshot.

        Returns True if saved successfully, False otherwise.
        """
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            key = f"{SNAPSHOT_PREFIX}{symbol}:{brick_size}"
            meta_key = f"{META_PREFIX}{symbol}"

            # Serialize and compress
            raw_json = json.dumps(snapshot, default=str)
            compressed = gzip.compress(raw_json.encode("utf-8"))

            # Safety: reject oversized snapshots
            if len(compressed) > MAX_SNAPSHOT_BYTES:
                log.warning(
                    f"Renko snapshot for {symbol} too large for Redis L1 "
                    f"({len(compressed)} bytes, max {MAX_SNAPSHOT_BYTES}) - skipping"
                )
                return False

            ttl = ttl_seconds or self.ttl_seconds

            # Write full snapshot + metadata in a pipeline (atomic)
            pipe = client.pipeline(transaction=False)
            pipe.set(key, compressed, ex=ttl)

            # Lightweight metadata for quick readiness checks
            meta = {
                "symbol": symbol,
                "brick_size": str(brick_size),
                "brick_count": str(snapshot.get("brick_count", 0)),
                "snapshot_version": str(snapshot.get("snapshot_version", 3)),
                "has_active_position": str(snapshot.get("active_position") is not None),
                "snapshot_ts": str(snapshot.get("snapshot_ts", 0)),
                "total_trades": str(snapshot.get("journal_stats", {}).get("total_trades", 0)),
            }
            pipe.hset(meta_key, mapping=meta)
            pipe.expire(meta_key, ttl)

            await pipe.execute()
            log.debug(
                f"Renko L1 save: {symbol} ({len(compressed)} bytes, "
                f"brick_count={snapshot.get('brick_count', 0)})"
            )
            return True

        except Exception as exc:
            log.debug(f"Renko L1 save failed for {symbol}: {exc}")
            return False

    async def load(
        self,
        symbol: str,
        brick_size: float = 0.50,
    ) -> Optional[dict]:
        """
        Load a pipeline snapshot from Redis L1 cache.

        Returns the deserialized snapshot dict, or None if not found.
        """
        if not self._enabled:
            return None
        try:
            client = await self._get_client()
            key = f"{SNAPSHOT_PREFIX}{symbol}:{brick_size}"

            compressed = await client.get(key)
            if compressed is None:
                return None

            # Decompress and deserialize
            raw_json = gzip.decompress(compressed).decode("utf-8")
            snapshot = json.loads(raw_json)

            log.debug(
                f"Renko L1 load: {symbol} ({len(compressed)} bytes, "
                f"brick_count={snapshot.get('brick_count', 0)})"
            )
            return snapshot

        except Exception as exc:
            log.debug(f"Renko L1 load failed for {symbol}: {exc}")
            return None

    async def load_meta(self, symbol: str) -> Optional[dict]:
        """
        Load lightweight metadata for a symbol from Redis L1.

        Much faster than loading the full snapshot - useful for
        readiness checks and status displays.
        """
        if not self._enabled:
            return None
        try:
            client = await self._get_client()
            meta_key = f"{META_PREFIX}{symbol}"
            raw = await client.hgetall(meta_key)
            if not raw:
                return None
            # Redis returns bytes in non-decode mode
            meta = {}
            for k, v in raw.items():
                key = k.decode("utf-8") if isinstance(k, bytes) else k
                val = v.decode("utf-8") if isinstance(v, bytes) else v
                meta[key] = val
            return meta

        except Exception as exc:
            log.debug(f"Renko L1 meta load failed for {symbol}: {exc}")
            return None

    async def delete(self, symbol: str, brick_size: float = 0.50) -> bool:
        """Delete a snapshot from Redis L1 cache."""
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            key = f"{SNAPSHOT_PREFIX}{symbol}:{brick_size}"
            meta_key = f"{META_PREFIX}{symbol}"
            pipe = client.pipeline(transaction=False)
            pipe.delete(key)
            pipe.delete(meta_key)
            await pipe.execute()
            return True
        except Exception as exc:
            log.debug(f"Renko L1 delete failed for {symbol}: {exc}")
            return False

    async def list_symbols(self) -> List[str]:
        """Return all symbols that have cached snapshots in Redis L1.

        v7.3: Uses SCAN instead of KEYS to avoid blocking the Redis event loop.
        """
        if not self._enabled:
            return []
        try:
            client = await self._get_client()
            pattern = f"{SNAPSHOT_PREFIX}*"
            symbols = set()
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor, match=pattern, count=100)
                for k in keys:
                    key = k.decode("utf-8") if isinstance(k, bytes) else k
                    # Key format: renko:snapshot:{symbol}:{brick_size}
                    parts = key.replace(SNAPSHOT_PREFIX, "").split(":")
                    if parts:
                        symbols.add(parts[0])
                if cursor == 0:
                    break
            return sorted(symbols)
        except Exception as exc:
            log.debug(f"Renko L1 list failed: {exc}")
            return []

    async def get_age_seconds(self, symbol: str) -> Optional[float]:
        """
        Get the age of a cached snapshot in seconds.

        Returns None if not cached or age cannot be determined.
        """
        meta = await self.load_meta(symbol)
        if meta and meta.get("snapshot_ts"):
            try:
                ts = float(meta["snapshot_ts"])
                return time.time() - ts
            except (ValueError, TypeError):
                return None
        return None

    async def close(self) -> None:
        """Close the Redis connection gracefully."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None


# Singleton - shared across all Renko endpoints
renko_cache = RenkoRedisCache()
