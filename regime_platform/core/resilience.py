"""
Circuit breaker and retry utilities for external service calls.

No external dependencies — uses only stdlib + asyncio.

Usage::

    from regime_platform.core.resilience import CircuitBreaker, retry_with_backoff, CircuitOpenError

    # Circuit breaker
    alpaca_breaker = CircuitBreaker(name="alpaca", failure_threshold=3, recovery_timeout=30)

    async def call_alpaca():
        async with alpaca_breaker:
            return await some_alpaca_api_call(...)

    # Retry with exponential backoff
    result = await retry_with_backoff(
        lambda: alpaca_api.submit_order(...),
        max_retries=3,
        base_delay=1.0,
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Tuple

log = logging.getLogger("regime.resilience")


# ── Circuit breaker ──────────────────────────────────────────────────────


class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal — requests flow through
    OPEN = "open"            # Tripped — requests are rejected
    HALF_OPEN = "half_open"  # Testing — one request allowed through


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open and requests are rejected."""
    pass


@dataclass
class CircuitBreaker:
    """
    Simple circuit breaker for external service calls.

    States:
      - CLOSED:  Normal operation. All requests pass through.
      - OPEN:    Tripped. All requests are rejected with CircuitOpenError.
                 Transitions to HALF_OPEN after recovery_timeout seconds.
      - HALF_OPEN: Probing. Requests are allowed through; success_threshold
                 consecutive successes close the circuit, any failure re-opens it.

    Usage::

        breaker = CircuitBreaker(name="alpaca", failure_threshold=3, recovery_timeout=30)

        async def safe_call():
            async with breaker:
                return await external_api(...)
    """

    name: str = "default"
    failure_threshold: int = 3          # Trips after this many consecutive failures
    recovery_timeout: float = 30.0      # Seconds before trying half-open
    success_threshold: int = 2          # Consecutive successes in half-open to close

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    @property
    def state(self) -> CircuitState:
        """Current state, with automatic OPEN → HALF_OPEN transition."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                log.info("Circuit breaker [%s]: OPEN → HALF_OPEN", self.name)
        return self._state

    def record_success(self) -> None:
        """Record a successful call."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                log.info("Circuit breaker [%s]: HALF_OPEN → CLOSED", self.name)
        else:
            # In CLOSED state, reset failure count on success
            self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                old_state = self._state.value
                self._state = CircuitState.OPEN
                log.warning(
                    "Circuit breaker [%s]: %s → OPEN (after %d failures)",
                    self.name,
                    old_state,
                    self._failure_count,
                )

    async def __aenter__(self):
        """Enter the circuit breaker context. Raises CircuitOpenError if OPEN."""
        if self.state == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit breaker [{self.name}] is OPEN — requests rejected"
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit the context, recording success or failure."""
        if exc_type is None:
            self.record_success()
        else:
            self.record_failure()
        return False  # Don't suppress exceptions

    @property
    def is_open(self) -> bool:
        """Check if the circuit breaker is currently open."""
        return self.state == CircuitState.OPEN

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        log.info("Circuit breaker [%s]: manually reset to CLOSED", self.name)

    def status(self) -> dict:
        """Return a dict of circuit breaker state for health/stats endpoints."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "is_open": self.is_open,
        }


# ── Retry with backoff ──────────────────────────────────────────────────


async def retry_with_backoff(
    fn: Callable,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: Tuple[type, ...] = (ConnectionError, TimeoutError, OSError),
) -> Any:
    """
    Call *fn* with exponential backoff on retryable exceptions.

    Parameters
    ----------
    fn : Callable
        The function to call. Can be sync or async.
    max_retries : int
        Maximum number of retries (total attempts = max_retries + 1).
    base_delay : float
        Base delay in seconds for the first retry.
    max_delay : float
        Maximum delay in seconds (caps exponential growth).
    retryable_exceptions : tuple
        Exception types that trigger a retry. Other exceptions
        propagate immediately without retry.

    Returns
    -------
    Any
        The return value of *fn*.

    Raises
    ------
    Exception
        The last retryable exception if all attempts fail, or
        any non-retryable exception immediately.

    Usage::

        result = await retry_with_backoff(
            lambda: alpaca_api.submit_order(...),
            max_retries=3,
            base_delay=1.0,
        )

        # With async fn
        result = await retry_with_backoff(
            async_fn,  # will be detected as coroutine function
            max_retries=2,
        )
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn()
            else:
                return fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                log.warning(
                    "Retry %d/%d after %.1fs: %s",
                    attempt + 1,
                    max_retries,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
            else:
                raise
    # Should never reach here, but just in case
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry_with_backoff: unexpected state (no exception, no result)")
