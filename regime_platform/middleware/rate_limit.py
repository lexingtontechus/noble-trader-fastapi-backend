"""
Rate limiting middleware using slowapi.

Provides per-IP rate limiting on compute-heavy endpoints to prevent abuse.
Configuration via environment variables:
  RATE_LIMIT_DEFAULT  — default limit for general endpoints (default "60/minute")
  RATE_LIMIT_COMPUTE  — limit for HMM/optimization endpoints (default "10/minute")
  RATE_LIMIT_WRITE    — limit for write/trade endpoints (default "20/minute")
"""

import os
from slowapi import Limiter
from slowapi.util import get_remote_address

# Configurable rate limits
RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "60/minute")
RATE_LIMIT_COMPUTE = os.getenv("RATE_LIMIT_COMPUTE", "10/minute")
RATE_LIMIT_WRITE = os.getenv("RATE_LIMIT_WRITE", "20/minute")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri="memory://",  # In-memory storage; use Redis for distributed
)
