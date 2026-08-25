"""
API Key Authentication
═══════════════════════

Single shared-secret gatekeeping (2026-08-25). Replaces the prior
JWT/Clerk/per-user-role auth system: there are no live users and the
only deployment target is a single Render fallback instance, so a
single `X-API-Key` header checked against one `API_KEY` setting is
sufficient — "generic auth for gatekeeping," not identity.

No roles, no per-user/org identity, no `TokenData` object. Endpoints
that used to accept `user: TokenData = Depends(get_authed_user)` for
data-scoping (audit trails, Alpaca credential resolution) now use the
fixed `DEFAULT_ACTOR` constant below instead — there was never a real
multi-tenant identity behind those fields.

Configuration
─────────────
  API_KEY - the single shared secret. Empty (default) means the gate
            fails closed: no value will ever authenticate.

Usage
─────
  @router.post("/endpoint")
  async def handler(_: None = Depends(require_api_key)):
      ...

  @router.websocket("/ws/{symbol}")
  async def ws(websocket, symbol, api_key: Optional[str] = Query(None)):
      await ws_auth(websocket, api_key)
      await websocket.accept()
      ...
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import APIKeyHeader

from regime_platform.core.settings import settings

log = logging.getLogger("regime.auth")

# Fixed identity used where the old per-user auth system fed a user_id/
# org_id into data-scoping or credential-resolution calls. There is no
# real multi-tenant identity behind these anymore.
DEFAULT_ACTOR = "system"

_api_key_hdr = APIKeyHeader(name="X-API-Key", auto_error=False)


class WebSocketAuthError(Exception):
    """Raised when WebSocket authentication fails before accept().
    The caller should NOT call websocket.accept()."""
    def __init__(self, reason: str = "Unauthorised"):
        self.reason = reason
        super().__init__(reason)


def _valid(key: Optional[str]) -> bool:
    return bool(settings.API_KEY) and key == settings.API_KEY


async def require_api_key(api_key: Optional[str] = Depends(_api_key_hdr)) -> None:
    """FastAPI dependency: gate an endpoint behind the single configured API_KEY.

    Fails closed when API_KEY is unset — an empty/missing key never
    authorizes a request, matching the prior system's "no key
    configured" posture.
    """
    if not _valid(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide the X-API-Key header.",
            headers={"WWW-Authenticate": "APIKey"},
        )


async def ws_auth(websocket: WebSocket, api_key: Optional[str] = None) -> None:
    """Authenticate a WebSocket connection BEFORE calling accept().

    Checks the ?api_key=<key> query param, falling back to the
    X-API-Key header.
    """
    key = api_key or websocket.headers.get("x-api-key")
    if not _valid(key):
        raise WebSocketAuthError(
            "Unauthorised - provide ?api_key=<key> or the X-API-Key header."
        )
