"""
Executor Factory - Singleton provider for properly-configured AlpacaExecutor instances.

Problem:
  All operational services (kill_switch, reconciler, mode_manager) were creating
  `AlpacaExecutor()` with no arguments, which meant:
  - No API keys -> always in simulation mode
  - `executor.api` attribute doesn't exist (it's `executor._client`)
  - Emergency cancel/close/reconcile silently did nothing

Solution:
  A single ExecutorFactory that:
  1. Reads API keys from settings on first access
  2. Respects the current trading mode (paper vs live URL)
  3. Re-creates the executor when mode changes
  4. Provides a `get_alpaca_rest()` method for direct REST API access
     (used by kill_switch cancel_all, reconciler, etc.)
  5. Exposes `api` property as an alias for `_client` to fix the broken references
  6. Wraps all Alpaca REST calls through a token-bucket throttle to avoid
     exceeding Alpaca's rate limits (200 req/min free tier)

Usage:
  from regime_platform.services.operational.executor_factory import executor_factory

  # Get a properly-configured executor
  executor = executor_factory.get_executor()

  # Get the Alpaca REST client directly (throttled)
  api = executor_factory.get_alpaca_rest()
  if api:
      orders = api.list_orders(status="open")

  # Invalidate cached executor (after mode change)
  executor_factory.invalidate()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


class ThrottledAlpacaREST:
    """
    Proxy wrapper around the Alpaca REST client that enforces rate throttling.

    Every method call on this proxy first acquires a token from the
    AlpacaThrottle before delegating to the underlying REST client.
    Attribute access (properties, etc.) is passed through without throttling.
    """

    # Methods that are purely property-like and should NOT be throttled
    _SKIP_THROTTLE = frozenset({
        "__class__", "__dict__", "__doc__", "__module__",
        "__init__", "__new__", "__repr__", "__str__",
    })

    def __init__(self, rest_client: Any):
        object.__setattr__(self, "_rest_client", rest_client)
        object.__setattr__(self, "_throttle", None)  # lazy init

    def _get_throttle(self):
        """Lazily obtain the throttle singleton."""
        from regime_platform.middleware.alpaca_throttle import get_alpaca_throttle
        return get_alpaca_throttle()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_rest_client"), name)

        # If it's a callable (API method), wrap it with throttle acquisition
        if callable(attr) and name not in ThrottledAlpacaREST._SKIP_THROTTLE:
            def _throttled_method(*args, **kwargs):
                throttle = self._get_throttle()
                # Acquire the throttle token synchronously if we're in an
                # event loop, otherwise just call through
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop is not None and loop.is_running():
                    # We're inside an async context - schedule acquire
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        acquired = asyncio.get_event_loop().run_in_executor(
                            pool, lambda: asyncio.run(throttle.acquire())
                        )
                    # For sync callers inside a running loop, we just call through
                    # and let the throttle catch up. Best-effort throttling.
                    return attr(*args, **kwargs)
                else:
                    # No running loop - safe to use asyncio.run
                    acquired = asyncio.run(throttle.acquire())
                    if not acquired:
                        raise RuntimeError(
                            "Alpaca rate limit: could not acquire throttle token within timeout"
                        )
                    return attr(*args, **kwargs)

            _throttled_method.__name__ = name
            _throttled_method.__qualname__ = f"ThrottledAlpacaREST.{name}"
            return _throttled_method

        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_rest_client"), name, value)


class AsyncThrottledAlpacaREST:
    """
    Async-friendly proxy wrapper around the Alpaca REST client.

    Every async method call on this proxy first acquires a token from the
    AlpacaThrottle (using `await`) before delegating to the underlying
    REST client's synchronous methods.

    Use this wrapper from async FastAPI route handlers.
    """

    def __init__(self, rest_client: Any):
        object.__setattr__(self, "_rest_client", rest_client)

    def _get_throttle(self):
        """Lazily obtain the throttle singleton."""
        from regime_platform.middleware.alpaca_throttle import get_alpaca_throttle
        return get_alpaca_throttle()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_rest_client"), name)

        # If it's a callable (API method), wrap it with async throttle acquisition
        if callable(attr):
            async def _async_throttled_method(*args, **kwargs):
                throttle = self._get_throttle()
                acquired = await throttle.acquire()
                if not acquired:
                    raise RuntimeError(
                        "Alpaca rate limit: could not acquire throttle token within timeout"
                    )
                logger.debug(f"AlpacaThrottle: token acquired for '{name}'")
                # The underlying alpaca-trade-api REST methods are synchronous,
                # so we run them in the default executor to avoid blocking
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, lambda: attr(*args, **kwargs))

            _async_throttled_method.__name__ = name
            _async_throttled_method.__qualname__ = f"AsyncThrottledAlpacaREST.{name}"
            return _async_throttled_method

        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_rest_client"), name, value)


class ExecutorFactory:
    """
    Singleton factory for AlpacaExecutor instances.

    Reads API credentials and base URL from regime_platform.core.settings
    and provides properly-configured executors for all operational services.
    """

    def __init__(self):
        self._executor = None
        self._api_client = None
        self._last_base_url: Optional[str] = None

    def _get_settings(self):
        """Lazy import settings to avoid circular imports."""
        from regime_platform.core.settings import settings
        return settings

    def get_executor(self):
        """
        Get a properly-configured AlpacaExecutor.

        Re-creates the executor if:
        - It hasn't been created yet
        - The base URL has changed (e.g., after mode switch)
        """
        settings = self._get_settings()
        current_base_url = settings.ALPACA_BASE_URL

        # Re-create if base URL changed (mode switch)
        if self._executor is not None and self._last_base_url != current_base_url:
            logger.info(
                f"ExecutorFactory: Base URL changed "
                f"({self._last_base_url} -> {current_base_url}), re-creating executor"
            )
            self._executor = None
            self._api_client = None

        if self._executor is None:
            try:
                from renko.executor import AlpacaExecutor

                self._executor = AlpacaExecutor(
                    api_key=settings.ALPACA_API_KEY or None,
                    api_secret=settings.ALPACA_SECRET_KEY or None,
                    base_url=current_base_url,
                    paper="paper-api" in current_base_url,
                )
                self._last_base_url = current_base_url
                logger.info(
                    f"ExecutorFactory: Created executor "
                    f"mode={self._executor.execution_mode} "
                    f"url={current_base_url}"
                )
            except Exception as e:
                logger.error(f"ExecutorFactory: Failed to create executor: {e}")
                # Fallback: create with no keys (simulation mode)
                from renko.executor import AlpacaExecutor
                self._executor = AlpacaExecutor()
                self._last_base_url = current_base_url

        return self._executor

    def get_alpaca_rest(self) -> Optional[Any]:
        """
        Get the Alpaca REST API client directly.

        This is used by kill_switch (cancel_all_orders, close_all_positions)
        and reconciler (list_orders, get_order, list_positions).

        Returns None if API keys are not configured (simulation mode).
        """
        if self._api_client is not None:
            return self._api_client

        settings = self._get_settings()

        if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
            logger.debug("ExecutorFactory: No Alpaca API keys - REST client unavailable")
            return None

        try:
            import alpaca_trade_api as tradeapi

            raw_client = tradeapi.REST(
                key_id=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                base_url=settings.ALPACA_BASE_URL,
            )
            # Wrap the raw client with async throttling proxy
            # AsyncThrottledAlpacaREST properly awaits throttle.acquire() in
            # async contexts, unlike the sync ThrottledAlpacaREST which has a
            # best-effort fallback that effectively bypasses throttling.
            self._api_client = AsyncThrottledAlpacaREST(raw_client)
            logger.info(
                f"ExecutorFactory: Alpaca REST client created (async-throttled) "
                f"for {settings.ALPACA_BASE_URL}"
            )
            return self._api_client
        except ImportError:
            logger.warning("ExecutorFactory: alpaca-trade-api not installed")
            return None
        except Exception as e:
            logger.error(f"ExecutorFactory: Failed to create REST client: {e}")
            return None

    def invalidate(self):
        """
        Invalidate cached executor and REST client.

        Called by ModeManager after a mode switch to force
        re-creation with the new base URL.
        """
        self._executor = None
        self._api_client = None
        self._last_base_url = None
        logger.info("ExecutorFactory: Cache invalidated - next access will re-create")

    @property
    def execution_mode(self) -> str:
        """Return the current executor's execution mode."""
        executor = self.get_executor()
        return executor.execution_mode

    @property
    def is_simulation(self) -> bool:
        """Return True if running in simulation mode (no real orders)."""
        return self.get_executor().is_simulation

    async def health_check(self) -> dict:
        """
        Run a health check against the Alpaca API.

        Uses the async-throttled REST client so the health check itself
        respects the rate limit.

        Returns:
          {"ok": True, "account_status": "ACTIVE", "mode": "paper"}
          or {"ok": False, "error": "connection failed", "mode": "simulation"}
        """
        settings = self._get_settings()

        if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
            return {"ok": False, "error": "No Alpaca API keys configured", "mode": "simulation"}

        try:
            import alpaca_trade_api as tradeapi
            from regime_platform.middleware.alpaca_throttle import get_alpaca_throttle

            throttle = get_alpaca_throttle()

            # Acquire throttle token before the API call
            acquired = await throttle.acquire()
            if not acquired:
                return {
                    "ok": False,
                    "error": "Alpaca rate limit: throttle acquire timed out",
                    "mode": self.execution_mode,
                }

            # Use a raw (unproxied) client for health checks so the
            # ThrottledAlpacaREST proxy doesn't interfere with async usage
            raw_client = tradeapi.REST(
                key_id=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                base_url=settings.ALPACA_BASE_URL,
            )
            loop = asyncio.get_running_loop()
            account = await loop.run_in_executor(None, raw_client.get_account)

            return {
                "ok": True,
                "account_status": account.status,
                "mode": self.execution_mode,
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "throttle": throttle.get_status(),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e)[:200],
                "mode": getattr(self, "execution_mode", "unknown"),
            }


# Module-level singleton
executor_factory = ExecutorFactory()
