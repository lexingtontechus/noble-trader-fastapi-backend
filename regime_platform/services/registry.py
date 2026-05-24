"""
SessionRegistry — global store of active StreamSessions.

One registry per process. Accessed by all WebSocket and SSE handlers.
Thread-safe via asyncio.Lock.

Configuration:
  New sessions are created via StreamSession.from_runtime_config()
  which resolves all params from the runtime configuration system.
  Legacy get_or_create() with explicit params is retained for backward
  compatibility but defaults to runtime config when no overrides are given.

Pipeline Invalidation:
  When a stream.* config key changes, all cached sessions are invalidated
  so the next tick() creates fresh sessions with updated config.
  Registered via runtime_config.register_on_change() in main.py startup.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Optional
from .stream_session import StreamSession

log = logging.getLogger("regime.registry")


class SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        symbol: str,
        window: int | None = None,
        kelly_fraction: float | None = None,
        target_vol: float | None = None,
        base_risk_limit: float | None = None,
        refit_every: int | None = None,
    ) -> StreamSession:
        """
        Get an existing session or create a new one.

        When no explicit params are provided (all None), creates the session
        via StreamSession.from_runtime_config() which resolves all params
        from the runtime configuration system (DB → env → hardcoded default).

        When explicit params are given, creates with those overrides (backward
        compatible for Redis restore and other callers that provide params).
        """
        async with self._lock:
            if symbol not in self._sessions:
                # If any param is explicitly provided, use the old constructor
                explicit = any(p is not None for p in (window, kelly_fraction, target_vol, base_risk_limit, refit_every))
                if explicit:
                    self._sessions[symbol] = StreamSession(
                        symbol=symbol,
                        window=window or 500,
                        kelly_fraction=kelly_fraction or 0.5,
                        target_vol=target_vol or 0.15,
                        base_risk_limit=base_risk_limit or 0.02,
                        refit_every=refit_every or 50,
                    )
                else:
                    # Resolve all params from runtime config
                    self._sessions[symbol] = await StreamSession.from_runtime_config(symbol)
            return self._sessions[symbol]

    async def get(self, symbol: str) -> Optional[StreamSession]:
        async with self._lock:
            return self._sessions.get(symbol)

    async def remove(self, symbol: str) -> None:
        async with self._lock:
            self._sessions.pop(symbol, None)

    async def list_symbols(self) -> list[str]:
        async with self._lock:
            return list(self._sessions.keys())

    async def stats(self) -> dict:
        async with self._lock:
            return {
                sym: {
                    "ready": s.is_ready,
                    "n_bars": len(s.price_buffer),
                    "refit_count": s._refit_count,
                    "tick_count": s._tick_count,
                    "last_regime": s._last_regime or "pending",
                }
                for sym, s in self._sessions.items()
            }

    async def invalidate_all(self) -> list[str]:
        """
        Invalidate all cached sessions.

        Called when stream.* config keys change so that the next
        tick() or get_or_create() creates fresh sessions with
        updated runtime config values.

        Returns list of invalidated symbol names.
        """
        async with self._lock:
            invalidated = list(self._sessions.keys())
            self._sessions.clear()
            if invalidated:
                log.info(
                    "Stream config changed — invalidated %d session(s): %s",
                    len(invalidated), ", ".join(invalidated),
                )
            return invalidated


async def on_stream_config_changed(key: str, entry) -> None:
    """
    Callback registered with runtime_config.register_on_change().

    When a stream.* config key changes, invalidate all cached StreamSessions
    so the next request creates fresh sessions with updated config.
    This ensures runtime config changes take effect immediately without
    requiring a server restart.

    Note: Alpaca WS params (alpaca.*) are read per-connection cycle,
    so reconnections naturally pick up new values. No explicit invalidation
    needed — the 30s TTL cache ensures freshness.
    """
    if key.startswith("stream."):
        await registry.invalidate_all()


# Singleton — imported everywhere
registry = SessionRegistry()
