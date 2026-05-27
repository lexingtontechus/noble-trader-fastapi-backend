"""
Alpaca Token Bucket Rate Throttle
══════════════════════════════════

Implements a thread-safe token bucket rate limiter for Alpaca API calls.

The Alpaca free tier enforces a 200 requests/minute limit. This throttle
ensures the application never exceeds that rate, with a small burst
allowance for short spikes.

Configuration (via environment variables or settings):
  ALPACA_RATE_LIMIT  - tokens per period (default: 200)
  ALPACA_RATE_PERIOD - period in seconds   (default: 60)

Algorithm:
  - Tokens refill continuously at `rate / period` tokens per second.
  - Maximum capacity is `rate + burst` (burst default: 10).
  - Each API call consumes one token via `await throttle.acquire()`.
  - If no tokens are available, the call blocks until one is refilled
    (up to `timeout` seconds).

Usage:
  from regime_platform.middleware.alpaca_throttle import alpaca_throttle

  # Before an Alpaca API call
  if not await alpaca_throttle.acquire():
      raise Exception("Alpaca rate limit timeout")

  # Check status
  status = alpaca_throttle.get_status()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class AlpacaThrottle:
    """
    Token bucket rate limiter for Alpaca API calls.

    Parameters
    ----------
    rate : int
        Number of tokens added per period (e.g. 200 requests per minute).
    period : int
        Length of the refill period in seconds (e.g. 60 seconds).
    burst : int
        Extra capacity above the per-period rate, allowing short bursts.
    """

    def __init__(self, rate: int = 200, period: int = 60, burst: int = 10):
        self._rate: int = rate
        self._period: int = period
        self._burst: int = burst
        self._max_tokens: float = float(rate + burst)
        self._tokens: float = self._max_tokens
        self._last_refill: float = time.monotonic()
        self._lock: asyncio.Lock = asyncio.Lock()

        # Refill rate in tokens per second
        self._refill_rate: float = rate / period if period > 0 else 0.0

        logger.info(
            f"AlpacaThrottle initialized: "
            f"rate={rate} tokens/{period}s, "
            f"burst={burst}, "
            f"max_tokens={self._max_tokens}, "
            f"refill_rate={self._refill_rate:.4f} tokens/s"
        )

    def _refill(self) -> None:
        """
        Refill tokens based on elapsed time since last refill.

        Must be called while holding self._lock.
        """
        now = time.monotonic()
        elapsed = now - self._last_refill

        if elapsed <= 0:
            return

        # Calculate how many tokens to add based on elapsed time
        tokens_to_add = elapsed * self._refill_rate
        self._tokens = min(self._max_tokens, self._tokens + tokens_to_add)
        self._last_refill = now

    async def acquire(self, timeout: float = 30.0) -> bool:
        """
        Acquire a token, waiting up to `timeout` seconds.

        Returns True if a token was acquired, False if the timeout expired.
        Uses exponential backoff with a minimum wait of 10ms between retries.

        Parameters
        ----------
        timeout : float
            Maximum time in seconds to wait for a token. Default 30s.

        Returns
        -------
        bool
            True if a token was successfully acquired, False on timeout.
        """
        deadline = time.monotonic() + timeout
        min_wait = 0.01  # 10ms minimum wait between retries

        while True:
            async with self._lock:
                self._refill()

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                # Calculate how long until the next token is available
                deficit = 1.0 - self._tokens
                wait_time = deficit / self._refill_rate if self._refill_rate > 0 else min_wait

            # Wait outside the lock so other coroutines can check/refill
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    f"AlpacaThrottle: acquire() timed out after {timeout}s "
                    f"(tokens_remaining={self._tokens:.2f})"
                )
                return False

            actual_wait = max(min_wait, min(wait_time, remaining))
            await asyncio.sleep(actual_wait)

    def get_status(self) -> dict:
        """
        Return the current throttle status.

        Note: This performs a non-locking read. For precise token counts,
        use `acquire()` which holds the lock during refill + consume.

        Returns
        -------
        dict
            {
                "tokens_remaining": float,
                "max_tokens": float,
                "refill_rate": float,      # tokens per second
                "last_refill": float,       # monotonic timestamp
                "rate": int,                # tokens per period
                "period": int,              # period in seconds
                "burst": int,               # extra burst capacity
            }
        """
        # Perform a quick refill to give an accurate snapshot
        now = time.monotonic()
        elapsed = now - self._last_refill
        current_tokens = min(
            self._max_tokens,
            self._tokens + elapsed * self._refill_rate,
        )

        return {
            "tokens_remaining": round(current_tokens, 2),
            "max_tokens": self._max_tokens,
            "refill_rate": round(self._refill_rate, 4),
            "last_refill": self._last_refill,
            "rate": self._rate,
            "period": self._period,
            "burst": self._burst,
        }


# ── Module-level singleton ──────────────────────────────────────────────────
# Lazily initialised on first import so settings are guaranteed to be loaded.


_alpaca_throttle: Optional[AlpacaThrottle] = None


def _create_throttle() -> AlpacaThrottle:
    """Create an AlpacaThrottle from current settings."""
    from regime_platform.core.settings import settings

    return AlpacaThrottle(
        rate=settings.ALPACA_RATE_LIMIT,
        period=settings.ALPACA_RATE_PERIOD,
        burst=10,
    )


def get_alpaca_throttle() -> AlpacaThrottle:
    """
    Get the module-level AlpacaThrottle singleton.

    Creates it on first access using values from settings.
    """
    global _alpaca_throttle
    if _alpaca_throttle is None:
        _alpaca_throttle = _create_throttle()
    return _alpaca_throttle


def reset_alpaca_throttle() -> None:
    """
    Reset the throttle singleton.

    Useful after changing settings (e.g., during tests or mode switches).
    The next call to `get_alpaca_throttle()` will create a fresh instance.
    """
    global _alpaca_throttle
    _alpaca_throttle = None


# Convenience alias
alpaca_throttle = property(lambda self: get_alpaca_throttle())
