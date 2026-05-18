"""
Lightweight Circuit Breaker
═══════════════════════════

Prevents cascading failures when downstream services (Alpaca, Finnhub,
Clerk, Supabase) are unavailable. After `failure_threshold` consecutive
failures, the circuit opens and all calls fail fast for `reset_timeout`
seconds before trying again.

Usage
─────
    from regime_platform.core.circuit_breaker import circuit

    # Wrap a downstream call
    result = await circuit("alpaca").call(fetch_positions, symbol="SPY")

    # Or as a decorator
    @circuit_protected("clerk")
    async def verify_token(token):
        ...

States
──────
  CLOSED   — normal operation, calls pass through
  OPEN     — failures exceeded threshold, calls fail fast
  HALF_OPEN — timeout expired, one probe call allowed
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from functools import wraps
from typing import Any, Callable, Coroutine, Optional

log = logging.getLogger("regime.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Single circuit breaker instance for one downstream service.

    Parameters
    ──────────
    name : str
        Human-readable name (e.g. "alpaca", "clerk")
    failure_threshold : int
        Consecutive failures before opening the circuit (default 5)
    reset_timeout : float
        Seconds before attempting a probe call (default 30)
    success_threshold : int
        Consecutive successes in HALF_OPEN to close the circuit (default 2)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        success_threshold: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._last_failure_detail: str = ""

    @property
    def state(self) -> CircuitState:
        """Current state, auto-transitioning from OPEN → HALF_OPEN if timeout expired."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.reset_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                log.info(f"Circuit [{self.name}]: OPEN → HALF_OPEN (probe allowed)")
        return self._state

    @property
    def is_open(self) -> bool:
        """True if the circuit is OPEN (calls should fail fast)."""
        return self.state == CircuitState.OPEN

    async def call(
        self,
        func: Callable[..., Coroutine],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Execute an async function through the circuit breaker.

        Raises
        ──────
        CircuitOpenError  if the circuit is OPEN
        """
        current_state = self.state

        if current_state == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit [{self.name}] is OPEN — failing fast. "
                f"Last failure: {self._last_failure_detail} "
                f"({int(time.monotonic() - self._last_failure_time)}s ago)"
            )

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            raise

    def _on_success(self) -> None:
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                log.info(f"Circuit [{self.name}]: HALF_OPEN → CLOSED (recovered)")
        else:
            self._failure_count = 0

    def _on_failure(self, exc: Exception) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        self._last_failure_detail = str(exc)[:200]

        if self._state == CircuitState.HALF_OPEN:
            # Probe failed — back to OPEN
            self._state = CircuitState.OPEN
            log.warning(
                f"Circuit [{self.name}]: HALF_OPEN → OPEN (probe failed: {exc})"
            )
        elif self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            log.warning(
                f"Circuit [{self.name}]: CLOSED → OPEN "
                f"({self._failure_count} consecutive failures, last: {exc})"
            )

    def reset(self) -> None:
        """Manually reset the circuit to CLOSED."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        log.info(f"Circuit [{self.name}]: manually reset to CLOSED")

    @property
    def stats(self) -> dict:
        """Current circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_detail[:100] if self._last_failure_detail else None,
            "last_failure_ago_s": (
                int(time.monotonic() - self._last_failure_time)
                if self._last_failure_time else None
            ),
        }


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is OPEN and rejects a call."""
    pass


# ── Registry ────────────────────────────────────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {}


def circuit(
    name: str,
    failure_threshold: int = 5,
    reset_timeout: float = 30.0,
    success_threshold: int = 2,
) -> CircuitBreaker:
    """
    Get or create a circuit breaker by name.

    Repeated calls with the same name return the same instance.
    """
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
            success_threshold=success_threshold,
        )
    return _breakers[name]


def all_circuit_stats() -> dict[str, dict]:
    """Return stats for all registered circuit breakers."""
    return {name: cb.stats for name, cb in _breakers.items()}


def circuit_protected(
    name: str,
    failure_threshold: int = 5,
    reset_timeout: float = 30.0,
) -> Callable:
    """
    Decorator that wraps an async function with a circuit breaker.

    Usage:
        @circuit_protected("clerk")
        async def fetch_jwks(issuer):
            ...
    """
    def decorator(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        cb = circuit(name, failure_threshold=failure_threshold, reset_timeout=reset_timeout)

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await cb.call(func, *args, **kwargs)

        # Attach the circuit breaker for inspection
        wrapper.circuit_breaker = cb  # type: ignore[attr-defined]
        return wrapper

    return decorator
