"""
Backtest Result Cache — Redis-backed with 1h TTL
═══════════════════════════════════════════════════

Caches backtest results keyed on the full request configuration so
identical backtests return instantly from cache instead of recomputing.

Architecture
────────────
  • Cache key: "bt:{prefix}:{sha256(canonical_json(request_body))}"
  • TTL: 3600 seconds (1 hour)
  • If REDIS_URL is not configured or redis is unavailable, all
    operations are no-ops — the backtest runs normally.
  • Never raises — Redis failures are caught and logged.

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

  # Invalidate (optional — TTL handles expiry)
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
    always caught and logged — they NEVER propagate to the caller.
    """

    def __init__(self, redis_url: str = "", ttl: int = DEFAULT_TTL):
        self._ttl = ttl
        self._redis = None
        self._enabled = False

        _url = redis_url or settings.REDIS_URL
        if not _url:
            log.info("REDIS_URL not set — backtest result caching disabled")
            return

        try:
            import redis.asyncio as aioredis  # noqa: F401
            self._url = _url
            self._enabled = True
            log.info(f"Backtest cache configured: {_url} (TTL={ttl}s)")
        except ImportError:
            log.warning("redis package not installed — backtest cache disabled. pip install redis")

    @property
    def enabled(self) -> bool:
        """True if Redis is configured and available for caching."""
        return self._enabled

    async def _get_client(self):
        """Lazy Redis connection — created on first use."""
        if self._redis is None and self._enabled:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self._url, decode_responses=True)
        return self._redis

    async def get(self, prefix: str, request_body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Look up a cached backtest result.

        Returns the cached result dict on HIT, or None on MISS.
        Never raises — Redis errors return None (cache miss).
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
        Never raises — Redis errors are caught and logged.
        """
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            key = _make_cache_key(prefix, request_body)
            payload = json.dumps(result, default=str)
            await client.set(key, payload, ex=self._ttl)
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
            deleted = await client.delete(key)
            return deleted > 0
        except Exception as exc:
            log.warning(f"Cache INVALIDATE failed: {exc}")
            return False

    async def clear_prefix(self, prefix: str) -> int:
        """
        Remove all cached results for a given prefix (e.g. "renko" or "hmm").

        Returns the number of keys deleted. Useful for admin/debug.
        """
        if not self._enabled:
            return 0
        try:
            client = await self._get_client()
            pattern = f"{KEY_PREFIX}:{prefix}:*"
            keys = await client.keys(pattern)
            if not keys:
                return 0
            deleted = await client.delete(*keys)
            log.info(f"Cache CLEAR: {deleted} keys matching {pattern}")
            return deleted
        except Exception as exc:
            log.warning(f"Cache CLEAR failed: {exc}")
            return 0

    async def get_stats(self) -> Dict[str, Any]:
        """
        Return cache statistics for monitoring.

        Includes key count, memory usage, and enabled status.
        """
        if not self._enabled:
            return {"enabled": False, "keys": 0}

        try:
            client = await self._get_client()
            # Count all backtest cache keys
            renko_keys = await client.keys(f"{KEY_PREFIX}:renko:*")
            hmm_keys = await client.keys(f"{KEY_PREFIX}:hmm:*")
            info = await client.info("memory")

            return {
                "enabled": True,
                "renko_keys": len(renko_keys),
                "hmm_keys": len(hmm_keys),
                "total_keys": len(renko_keys) + len(hmm_keys),
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "ttl_seconds": self._ttl,
            }
        except Exception as exc:
            log.warning(f"Cache STATS failed: {exc}")
            return {"enabled": True, "error": str(exc)}

    async def close(self) -> None:
        """Close the Redis connection gracefully."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass


# ── Singleton — shared across all endpoints ──────────────────────────────────

backtest_cache = BacktestCache()
