"""
Authentication module exports.

Single shared-secret (X-API-Key) gatekeeping — see api_key.py.
"""

from .api_key import (
    DEFAULT_ACTOR,
    WebSocketAuthError,
    require_api_key,
    ws_auth,
)

__all__ = [
    "require_api_key",
    "ws_auth",
    "WebSocketAuthError",
    "DEFAULT_ACTOR",
]
