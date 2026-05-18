"""
Rate limiting middleware using slowapi.

Provides per-IP rate limiting on compute-heavy endpoints to prevent abuse.
Configuration via environment variables:
  RATE_LIMIT_DEFAULT  — default limit for general endpoints (default "60/minute")
  RATE_LIMIT_COMPUTE  — limit for HMM/optimization endpoints (default "10/minute")
  RATE_LIMIT_WRITE    — limit for write/trade endpoints (default "20/minute")
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from regime_platform.core.settings import settings

# Configurable rate limits
RATE_LIMIT_DEFAULT = settings.RATE_LIMIT_DEFAULT
RATE_LIMIT_COMPUTE = settings.RATE_LIMIT_COMPUTE
RATE_LIMIT_WRITE = settings.RATE_LIMIT_WRITE

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri="memory://",  # In-memory storage; use Redis for distributed
)
