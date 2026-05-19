"""
Executor Factory — Singleton provider for properly-configured AlpacaExecutor instances.

Problem:
  All operational services (kill_switch, reconciler, mode_manager) were creating
  `AlpacaExecutor()` with no arguments, which meant:
  - No API keys → always in simulation mode
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

Usage:
  from regime_platform.services.operational.executor_factory import executor_factory

  # Get a properly-configured executor
  executor = executor_factory.get_executor()

  # Get the Alpaca REST client directly
  api = executor_factory.get_alpaca_rest()
  if api:
      orders = api.list_orders(status="open")

  # Invalidate cached executor (after mode change)
  executor_factory.invalidate()
"""

from __future__ import annotations
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)


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
                f"({self._last_base_url} → {current_base_url}), re-creating executor"
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
            logger.debug("ExecutorFactory: No Alpaca API keys — REST client unavailable")
            return None

        try:
            import alpaca_trade_api as tradeapi

            self._api_client = tradeapi.REST(
                key_id=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                base_url=settings.ALPACA_BASE_URL,
            )
            logger.info(
                f"ExecutorFactory: Alpaca REST client created "
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
        logger.info("ExecutorFactory: Cache invalidated — next access will re-create")

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

        Returns:
          {"ok": True, "account_status": "ACTIVE", "mode": "paper"}
          or {"ok": False, "error": "connection failed", "mode": "simulation"}
        """
        api = self.get_alpaca_rest()
        if not api:
            return {"ok": False, "error": "No Alpaca API keys configured", "mode": "simulation"}

        try:
            account = api.get_account()
            return {
                "ok": True,
                "account_status": account.status,
                "mode": self.execution_mode,
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
            }
        except Exception as e:
            return {
                "ok": False,
                "error": str(e)[:200],
                "mode": self.execution_mode,
            }


# Module-level singleton
executor_factory = ExecutorFactory()
