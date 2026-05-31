"""
Trading Heartbeat — O(1) data state detection for Renko pipelines
════════════════════════════════════════════════════════════════════

Provides instant pipeline state checks via Redis hashes, replacing
the multi-layer staleness waterfall (Redis L1 → Supabase L2 → age calc).

Architecture:
    trading:heartbeat:{symbol}  → Redis hash (24h TTL, updated every tick)
    trading:config:{symbol}     → Redis hash (1h TTL, updated on config change)

Heartbeat fields:
    status          — cold | warming | warm | hot | live_ready
    brick_count     — current brick count
    last_brick_ts   — unix timestamp of last brick
    last_price      — last processed tick price
    last_tick_ts    — unix timestamp of last tick
    snapshot_ts     — when snapshot was last saved
    has_position    — whether pipeline has an open position
    warmup_mode     — incremental | full | restore (last warmup mode)
    source          — yahoo_fetch | snapshot_restore | up_to_date | stale_snapshot_restore
    regime          — current HMM regime
    direction       — last brick direction (up|down)
    sizing_mode     — kelly | masaniello
    equity          — current account equity

Config cache fields:
    All RenkoConfig fields as string values + config_ts timestamp

Error policy:
    Redis failures NEVER block the pipeline. All operations are
    wrapped in try/except and return gracefully.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from regime_platform.core.settings import settings

log = logging.getLogger(__name__)

# Redis key prefixes
HEARTBEAT_PREFIX = "trading:heartbeat:"
CONFIG_PREFIX = "trading:config:"

# TTLs
HEARTBEAT_TTL = 86400     # 24 hours
CONFIG_TTL = 3600          # 1 hour

# Heartbeat field defaults
HEARTBEAT_DEFAULTS = {
    "status": "cold",
    "brick_count": "0",
    "last_brick_ts": "0",
    "last_price": "0",
    "last_tick_ts": "0",
    "snapshot_ts": "0",
    "has_position": "false",
    "warmup_mode": "",
    "source": "",
    "regime": "",
    "direction": "",
    "sizing_mode": "kelly",
    "equity": "100000",
}


class TradingHeartbeat:
    """
    Redis-backed heartbeat for instant pipeline state detection.

    If REDIS_URL is not configured, all methods become no-ops and
    `enabled` is False. This ensures the pipeline works perfectly
    without Redis — just with slower cold starts.
    """

    def __init__(self, redis_url: str = ""):
        self._redis = None
        self._enabled = False

        _url = redis_url or settings.redis_url_effective
        if not _url:
            log.info(
                "TradingHeartbeat: REDIS_URL not set — heartbeat disabled "
                "(fallback to snapshot-based state detection)"
            )
            return

        self._url = _url
        self._enabled = True
        log.info("TradingHeartbeat: configured (TTL=%ds)", HEARTBEAT_TTL)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _get_client(self):
        """Lazy connection — uses the shared Redis pool."""
        if self._redis is None and self._enabled:
            try:
                from regime_platform.services.redis_pool import redis_pool
                client = await redis_pool.get_client()
                if client is not None:
                    self._redis = client
                    return self._redis
                # Fallback: create standalone client
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(
                    self._url,
                    decode_responses=True,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                    socket_connect_timeout=2,
                    socket_timeout=3,
                )
            except Exception as e:
                log.warning("TradingHeartbeat: failed to get client from pool: %s", e)
                try:
                    import redis.asyncio as aioredis
                    self._redis = aioredis.from_url(
                        self._url,
                        decode_responses=True,
                        socket_keepalive=True,
                        retry_on_timeout=True,
                        socket_connect_timeout=2,
                        socket_timeout=3,
                    )
                except Exception as e2:
                    log.error("TradingHeartbeat: client creation failed: %s", e2)
        return self._redis

    # ── Heartbeat Operations ──────────────────────────────────────────

    async def update(
        self,
        symbol: str,
        *,
        status: Optional[str] = None,
        brick_count: Optional[int] = None,
        last_brick_ts: Optional[float] = None,
        last_price: Optional[float] = None,
        last_tick_ts: Optional[float] = None,
        snapshot_ts: Optional[float] = None,
        has_position: Optional[bool] = None,
        warmup_mode: Optional[str] = None,
        source: Optional[str] = None,
        regime: Optional[str] = None,
        direction: Optional[str] = None,
        sizing_mode: Optional[str] = None,
        equity: Optional[float] = None,
    ) -> bool:
        """
        Update heartbeat fields for a symbol.

        Only provided fields are updated; others are left unchanged.
        This allows partial updates (e.g., tick updates only last_price
        and last_tick_ts without touching status or brick_count).
        """
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            if client is None:
                return False

            key = f"{HEARTBEAT_PREFIX}{symbol}"
            mapping = {}

            if status is not None:
                mapping["status"] = status
            if brick_count is not None:
                mapping["brick_count"] = str(brick_count)
            if last_brick_ts is not None:
                mapping["last_brick_ts"] = str(last_brick_ts)
            if last_price is not None:
                mapping["last_price"] = str(last_price)
            if last_tick_ts is not None:
                mapping["last_tick_ts"] = str(last_tick_ts)
            if snapshot_ts is not None:
                mapping["snapshot_ts"] = str(snapshot_ts)
            if has_position is not None:
                mapping["has_position"] = str(has_position).lower()
            if warmup_mode is not None:
                mapping["warmup_mode"] = warmup_mode
            if source is not None:
                mapping["source"] = source
            if regime is not None:
                mapping["regime"] = regime
            if direction is not None:
                mapping["direction"] = direction
            if sizing_mode is not None:
                mapping["sizing_mode"] = sizing_mode
            if equity is not None:
                mapping["equity"] = str(equity)

            if not mapping:
                return True  # Nothing to update

            pipe = client.pipeline(transaction=False)
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, HEARTBEAT_TTL)
            await pipe.execute()

            return True
        except Exception as exc:
            log.debug("TradingHeartbeat update failed for %s: %s", symbol, exc)
            return False

    async def update_from_pipeline(self, symbol: str, pipeline) -> bool:
        """
        Full heartbeat update from a RenkoPipeline instance.

        Convenience method that extracts all fields from the pipeline
        in a single call. Used after warmup, tick processing, and
        snapshot saves.
        """
        readiness = pipeline.readiness
        last_brick = pipeline.brick_engine.last_brick

        return await self.update(
            symbol,
            status=readiness["level"],
            brick_count=pipeline.brick_engine.brick_count,
            last_brick_ts=last_brick.timestamp if last_brick else None,
            last_price=last_brick.close_price if last_brick else None,
            last_tick_ts=time.time(),
            has_position=pipeline.risk_manager.has_position,
            regime=pipeline._current_regime,
            direction=(
                pipeline.brick_engine.current_direction.value
                if pipeline.brick_engine.current_direction else None
            ),
            sizing_mode=pipeline.config.sizing_mode.value,
            equity=pipeline._current_equity,
        )

    async def get(self, symbol: str) -> Optional[Dict[str, str]]:
        """
        Get the heartbeat for a symbol.

        Returns a dict of string fields, or None if not found.
        """
        if not self._enabled:
            return None
        try:
            client = await self._get_client()
            if client is None:
                return None
            key = f"{HEARTBEAT_PREFIX}{symbol}"
            data = await client.hgetall(key)
            if not data:
                return None
            return data
        except Exception as exc:
            log.debug("TradingHeartbeat get failed for %s: %s", symbol, exc)
            return None

    async def get_status(self, symbol: str) -> Optional[str]:
        """Quick check: just get the status field."""
        if not self._enabled:
            return None
        try:
            client = await self._get_client()
            if client is None:
                return None
            key = f"{HEARTBEAT_PREFIX}{symbol}"
            return await client.hget(key, "status")
        except Exception as exc:
            log.debug("TradingHeartbeat get_status failed for %s: %s", symbol, exc)
            return None

    async def is_fresh(self, symbol: str, max_age_seconds: float = 14400) -> bool:
        """
        Check if a symbol's data is fresh (snapshot_ts within max_age_seconds).

        Returns True if the heartbeat exists and snapshot_ts is recent.
        Returns False if no heartbeat, no snapshot_ts, or stale.
        """
        heartbeat = await self.get(symbol)
        if not heartbeat:
            return False
        try:
            snapshot_ts = float(heartbeat.get("snapshot_ts", "0"))
            if snapshot_ts <= 0:
                return False
            return (time.time() - snapshot_ts) < max_age_seconds
        except (ValueError, TypeError):
            return False

    async def list_symbols(self) -> List[str]:
        """List all symbols with heartbeats (SCAN, not KEYS)."""
        if not self._enabled:
            return []
        try:
            client = await self._get_client()
            if client is None:
                return []
            symbols = set()
            cursor = 0
            pattern = f"{HEARTBEAT_PREFIX}*"
            while True:
                cursor, keys = await client.scan(cursor, match=pattern, count=100)
                for k in keys:
                    symbol = k.replace(HEARTBEAT_PREFIX, "")
                    symbols.add(symbol)
                if cursor == 0:
                    break
            return sorted(symbols)
        except Exception as exc:
            log.debug("TradingHeartbeat list_symbols failed: %s", exc)
            return []

    async def delete(self, symbol: str) -> bool:
        """Delete a heartbeat."""
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            if client is None:
                return False
            key = f"{HEARTBEAT_PREFIX}{symbol}"
            await client.delete(key)
            return True
        except Exception as exc:
            log.debug("TradingHeartbeat delete failed for %s: %s", symbol, exc)
            return False

    # ── Config Cache Operations ───────────────────────────────────────

    async def cache_config(self, symbol: str, config_dict: Dict[str, Any]) -> bool:
        """
        Cache a RenkoConfig in Redis for fast retrieval.

        The config is stored as a flat hash of string values.
        A config_ts timestamp is added for staleness detection.
        """
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            if client is None:
                return False

            key = f"{CONFIG_PREFIX}{symbol}"
            mapping = {}
            for k, v in config_dict.items():
                # Skip non-serializable values (enums, nested dicts)
                if isinstance(v, (str, int, float, bool)):
                    mapping[k] = str(v)
                elif hasattr(v, "value"):  # Enum
                    mapping[k] = str(v.value)
                # Skip complex types (lists, dicts, None)

            mapping["config_ts"] = str(time.time())

            pipe = client.pipeline(transaction=False)
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, CONFIG_TTL)
            await pipe.execute()

            return True
        except Exception as exc:
            log.debug("TradingHeartbeat cache_config failed for %s: %s", symbol, exc)
            return False

    async def get_config(self, symbol: str) -> Optional[Dict[str, str]]:
        """
        Get cached config for a symbol.

        Returns a dict of string values, or None if not cached or stale.
        """
        if not self._enabled:
            return None
        try:
            client = await self._get_client()
            if client is None:
                return None
            key = f"{CONFIG_PREFIX}{symbol}"
            data = await client.hgetall(key)
            if not data:
                return None
            # Check staleness
            config_ts = float(data.get("config_ts", "0"))
            if config_ts > 0 and (time.time() - config_ts) > CONFIG_TTL:
                return None  # Stale
            return data
        except Exception as exc:
            log.debug("TradingHeartbeat get_config failed for %s: %s", symbol, exc)
            return None

    async def invalidate_config(self, symbol: str) -> bool:
        """Invalidate cached config for a symbol."""
        if not self._enabled:
            return False
        try:
            client = await self._get_client()
            if client is None:
                return False
            key = f"{CONFIG_PREFIX}{symbol}"
            await client.delete(key)
            return True
        except Exception as exc:
            log.debug("TradingHeartbeat invalidate_config failed for %s: %s", symbol, exc)
            return False

    async def close(self) -> None:
        """Close connections gracefully. No-op for shared pool."""
        # Don't close shared pool connections
        self._redis = None


# Singleton — shared across all Renko endpoints
trading_heartbeat = TradingHeartbeat()
