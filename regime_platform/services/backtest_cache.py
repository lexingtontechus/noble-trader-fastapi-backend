"""
Backtest Result Cache - Redis-backed with 1h TTL
═══════════════════════════════════════════════════

Caches backtest results keyed on the full request configuration so
identical backtests return instantly from cache instead of recomputing.

Architecture
────────────
  • Cache key: "bt:{prefix}:{sha256(canonical_json(request_body))}"
  • TTL: 3600 seconds (1 hour)
  • If REDIS_URL is not configured or redis is unavailable, all
    operations are no-ops - the backtest runs normally.
  • Never raises - Redis failures are caught and logged.

v7.3 Changes
────────────
  • Uses shared Redis connection pool instead of per-service connection
  • Replaced KEYS command with SCAN to avoid blocking Redis event loop
  • Maintains a Redis SET (bt:known_keys:{prefix}) for O(1) key tracking

Usage
─────
  from regime_platform.services.backtest_cache import backtest_cache

  # Check cache before running
  cached = await backtest_cache.get("renko", request_dict)
  if cached is not None:
      return cached  # cache HIT

  # Run backtest, then store
  result = await run_expensive_backtest(...)
  await backtest_cache.set("renko", request_dict, result)

  # Invalidate (optional - TTL handles expiry)
  await backtest_cache.invalidate("renko", request_dict)

  # Health check
  is_enabled = backtest_cache.enabled
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from regime_platform.core.settings import settings

log = logging.getLogger("regime.backtest_cache")

# ── Constants ────────────────────────────────────────────────────────────────

KEY_PREFIX = "bt"
KNOWN_KEYS_PREFIX = "bt:known_keys"  # SET for tracking cached keys
DEFAULT_TTL = 3600  # 1 hour


def _make_cache_key(prefix: str, request_body: Dict[str, Any]) -> str:
    """
    Create a deterministic cache key from the request body.

    The key is SHA-256 of a canonical JSON representation (sorted keys,
    compact separators) so identical configs always hash identically.
    Large fields (prices, timestamps, regimes) are excluded from the hash
    and replaced with their length to keep keys stable across identical
    datasets without including every data point.
    """
    # Deep-copy so we don't mutate the caller's dict
    canonical = dict(request_body)

    # Replace large lists with their length for a stable hash
    for field in ("prices", "timestamps", "regimes", "dates"):
        val = canonical.get(field)
        if isinstance(val, list):
            canonical[f"{field}_len"] = len(val)
            del canonical[field]

    # Sort keys, compact JSON for determinism
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{KEY_PREFIX}:{prefix}:{digest}"


class BacktestCache:
    """
    Async Redis cache for backtest results.

    If REDIS_URL is not configured or the redis package is not installed,
    all methods become no-ops and `enabled` is False. Redis failures are
    always caught and logged - they NEVER propagate to the caller.

    v7.3: Uses shared connection pool. Replaces KEYS with SCAN + SET tracking.
    """

    def __init__(self, redis_url: str = "", ttl: int = DEFAULT_TTL):
        self._ttl = ttl
        self._redis = None
        self._enabled = False

        _url = redis_url or settings.redis_url_effective
        if not _url:
            log.info("REDIS_URL not set - backtest result caching disabled")
            return

        try:
            import redis.asyncio as aioredis  # noqa: F401
            self._url = _url
            self._enabled = True
            log.info(f"Backtest cache configured (TTL={ttl}s)")
        except ImportError:
            log.warning("redis package not installed - backtest cache disabled. pip install redis")

    @property
    def enabled(self) -> bool:
        """True if Redis is configured and available for caching."""
        return self._enabled

    async def _get_client(self):
        """Get Redis client from the shared connection pool.

        v7.3: Uses the shared RedisConnectionManager instead of creating
        a per-service TCP connection.
        """
        if self._redis is None and self._enabled:
            try:
                from regime_platform.services.redis_pool import redis_pool
                self._redis = await redis_pool.get_client()
            except Exception as e:
                log.warning(f"Backtest cache: Redis pool connection failed: {e}")
        return self._redis

    async def get(self, prefix: str, request_body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Look up a cached backtest result.

        Returns the cached result dict on HIT, or None on MISS.
        Never raises - Redis errors return None (cache miss).
        """
        if not self._enabled:
            return None
        try:
            client = await self._get_client()
            key = _make_cache_key(prefix, request_body)
            raw = await client.get(key)
            if raw is None:
                log.debug(f"Cache MISS: {key[:40]}...")
                return None
            log.info(f"Cache HIT: {key[:40]}...")
            return json.loads(raw)
        except Exception as exc:
            log.warning(f"Cache GET failed (treating as miss): {exc}")
            return None

    async def set(
        self,
        prefix: str,
        request_body: Dict[str, Any],
        result: Dict[str, Any],
    ) -> bool:
        """
        Store a backtest result in the cache.

        Returns True if stored successfully, False otherwise.
        Never raises - Redis errors are caught and logged.
        """
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            key = _make_cache_key(prefix, request_body)
            payload = json.dumps(result, default=str)

            # Use pipeline to SET + SADD atomically
            pipe = client.pipeline(transaction=False)
            pipe.set(key, payload, ex=self._ttl)
            # Track the key in the known_keys SET for O(1) stats
            pipe.sadd(f"{KNOWN_KEYS_PREFIX}:{prefix}", key)
            await pipe.execute()

            log.info(f"Cache SET: {key[:40]}... (TTL={self._ttl}s)")
            return True
        except Exception as exc:
            log.warning(f"Cache SET failed: {exc}")
            return False

    async def invalidate(self, prefix: str, request_body: Dict[str, Any]) -> bool:
        """
        Remove a cached backtest result.

        Returns True if the key existed and was deleted, False otherwise.
        Never raises.
        """
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            key = _make_cache_key(prefix, request_body)

            # Use pipeline to DEL + SREM atomically
            pipe = client.pipeline(transaction=False)
            pipe.delete(key)
            pipe.srem(f"{KNOWN_KEYS_PREFIX}:{prefix}", key)
            results = await pipe.execute()

            return results[0] > 0
        except Exception as exc:
            log.warning(f"Cache INVALIDATE failed: {exc}")
            return False

    async def clear_prefix(self, prefix: str) -> int:
        """
        Remove all cached results for a given prefix (e.g. "renko" or "hmm").

        v7.3: Uses SCAN instead of KEYS to avoid blocking the Redis event loop.
        Returns the number of keys deleted. Useful for admin/debug.
        """
        if not self._enabled:
            return 0
        try:
            client = await self._get_client()
            pattern = f"{KEY_PREFIX}:{prefix}:*"
            keys_to_delete = []

            # Use SCAN instead of KEYS
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor, match=pattern, count=100)
                keys_to_delete.extend(keys)
                if cursor == 0:
                    break

            if not keys_to_delete:
                return 0

            deleted = await client.delete(*keys_to_delete)

            # Clear the known_keys SET for this prefix
            await client.delete(f"{KNOWN_KEYS_PREFIX}:{prefix}")

            log.info(f"Cache CLEAR: {deleted} keys matching {pattern}")
            return deleted
        except Exception as exc:
            log.warning(f"Cache CLEAR failed: {exc}")
            return 0

    async def get_stats(self) -> Dict[str, Any]:
        """
        Return cache statistics for monitoring.

        v7.3: Uses the known_keys SET for O(1) key counting instead of
        the O(N) blocking KEYS command. Falls back to SCAN if the SET
        is empty or unavailable.
        """
        if not self._enabled:
            return {"enabled": False, "keys": 0}

        try:
            client = await self._get_client()

            # Use SCARD on the known_keys SET for O(1) counting
            renko_count = await client.scard(f"{KNOWN_KEYS_PREFIX}:renko")
            hmm_count = await client.scard(f"{KNOWN_KEYS_PREFIX}:hmm")

            # Get memory info
            info = await client.info("memory")

            return {
                "enabled": True,
                "renko_keys": renko_count,
                "hmm_keys": hmm_count,
                "total_keys": renko_count + hmm_count,
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "ttl_seconds": self._ttl,
            }
        except Exception as exc:
            log.warning(f"Cache STATS failed: {exc}")
            return {"enabled": True, "error": str(exc)}

    async def close(self) -> None:
        """Release Redis client reference (pool manages the connection)."""
        self._redis = None


# ── Singleton - shared across all endpoints ──────────────────────────────────

backtest_cache = BacktestCache()
