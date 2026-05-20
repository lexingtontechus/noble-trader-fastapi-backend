"""
JWT Authentication
══════════════════

Provides JWT-based authentication for:
  • HTTP endpoints  — FastAPI Depends(get_current_user)
  • WebSocket       — Token passed as ?token= query param
  • API key fallback — X-API-Key header for service-to-service calls

Configuration (environment variables)
──────────────────────────────────────
  JWT_SECRET_KEY   — signing secret (required; no default for security)
  JWT_ALGORITHM    — default "HS256"
  JWT_EXPIRE_MINS  — token lifetime in minutes (default 60)
  API_KEYS         — comma-separated valid API keys (optional)
  AUTH_ENABLED     — set "false" to disable auth entirely (dev mode)

Usage
─────
  # Protect an HTTP endpoint
  @router.post("/endpoint")
  async def handler(user=Depends(get_current_user)):
      ...

  # Protect a WebSocket endpoint (auth BEFORE accept)
  @router.websocket("/ws/{symbol}")
  async def ws(websocket, symbol, token=Query(None)):
      user = await ws_auth(websocket, token)  # raises WebSocketAuthError if invalid
      await websocket.accept()                # only reached if auth passes
      ...

  # Issue a token (login endpoint)
  token = create_access_token({"sub": "trader1", "role": "viewer"})
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Query, WebSocket, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader

from regime_platform.core.settings import settings

log = logging.getLogger("regime.auth")

# ── Configuration ─────────────────────────────────────────────────────────────

JWT_SECRET_KEY  = settings.JWT_SECRET_KEY
JWT_ALGORITHM   = settings.JWT_ALGORITHM
JWT_EXPIRE_MINS = settings.JWT_EXPIRE_MINS
API_KEYS        = settings.api_keys_set
AUTH_ENABLED    = settings.AUTH_ENABLED


# ── Token data model ──────────────────────────────────────────────────────────

class TokenData:
    def __init__(self, sub: str, role: str = "viewer", exp: Optional[datetime] = None, org_id: Optional[str] = None):
        self.sub    = sub      # subject / user ID
        self.role   = role     # "admin" | "trader" | "viewer"
        self.exp    = exp
        self.org_id = org_id   # Clerk Org ID (None when not using Orgs)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "trader")


# ── Token creation ────────────────────────────────────────────────────────────

def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed JWT access token.

    Parameters
    ──────────
    data           Payload dict. Should include "sub" (subject/user).
    expires_delta  Token lifetime. Defaults to JWT_EXPIRE_MINS.

    Returns
    ───────
    Signed JWT string.
    """
    try:
        from jose import jwt
    except ImportError:
        raise RuntimeError("python-jose not installed: pip install python-jose[cryptography]")

    if not JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured")

    payload = data.copy()
    expire  = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=JWT_EXPIRE_MINS)
    )
    payload.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> TokenData:
    """
    Decode and validate a JWT token.

    Raises
    ──────
    HTTPException 401 if token is invalid, expired, or secret is missing.
    """
    try:
        from jose import jwt, JWTError
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="python-jose not installed on server",
        )

    if not JWT_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT authentication not configured on server",
        )

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        sub    = payload.get("sub")
        role   = payload.get("role", "viewer")
        exp    = payload.get("exp")
        org_id = payload.get("org_id")
        if sub is None:
            raise HTTPException(status_code=401, detail="Token missing 'sub' claim")
        return TokenData(sub=str(sub), role=str(role), org_id=org_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI security schemes ──────────────────────────────────────────────────

_bearer     = HTTPBearer(auto_error=False)
_api_key_hdr = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    api_key: Optional[str] = Depends(_api_key_hdr),
) -> TokenData:
    """
    FastAPI dependency for HTTP endpoint authentication.

    Accepts:
      - Bearer JWT token in Authorization header
      - X-API-Key header (service-to-service)

    If AUTH_ENABLED=false, returns a synthetic admin user (dev mode).

    Usage:
        @router.post("/endpoint")
        async def handler(user: TokenData = Depends(get_current_user)):
            ...
    """
    if not AUTH_ENABLED:
        return TokenData(sub="dev", role="admin")

    # API key check first (simpler, faster)
    if api_key and api_key in API_KEYS:
        return TokenData(sub=f"apikey:{api_key[:8]}...", role="trader")

    # JWT check
    if credentials and credentials.scheme.lower() == "bearer":
        return decode_token(credentials.credentials)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Provide a Bearer JWT or X-API-Key header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_write(user: TokenData = Depends(get_authed_user)) -> TokenData:
    """Dependency that additionally requires trader or admin role.
    Uses get_authed_user which supports Clerk JWT → local JWT → API key."""
    if not user.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{user.role}' does not have write access.",
        )
    return user


async def require_admin(user: TokenData = Depends(get_authed_user)) -> TokenData:
    """Dependency that requires admin role.
    Uses get_authed_user which supports Clerk JWT → local JWT → API key."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return user


# ── Unified auth (Clerk JWT → old JWT → API key) ─────────────────────────────

def _try_clerk_verification(token: str) -> Optional[TokenData]:
    """
    Try to verify a token as a Clerk JWT.
    Uses dynamic JWKS based on the token's issuer claim.
    Returns TokenData if successful, None if not a Clerk token.
    """
    try:
        from .clerk_auth import verify_clerk_token, CLERK_AUTH_ENABLED
    except ImportError:
        log.debug("clerk_auth module not importable; skipping Clerk verification")
        return None

    if not CLERK_AUTH_ENABLED:
        return None

    # Check if token looks like a Clerk JWT by examining the issuer
    # Clerk JWTs have an "iss" claim like https://xxx.clerk.accounts.dev
    try:
        import jwt as _jwt
        payload = _jwt.decode(
            token,
            key="",            # Required by some PyJWT versions even with verify_signature=False
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["RS256", "HS256"],
        )
    except Exception as e:
        # Can't decode at all — probably not a JWT, move on
        log.debug(f"Cannot decode token for issuer check: {e}")
        return None

    iss = payload.get("iss", "")
    if "clerk" not in iss.lower() and ".accounts.dev" not in iss.lower():
        log.debug(f"Token issuer '{iss}' is not a Clerk domain — skipping Clerk verification")
        return None

    # Issuer is a Clerk domain — attempt full verification
    try:
        clerk_user = verify_clerk_token(token)
        # verify_clerk_token now does privateMetadata role lookup,
        # so clerk_user.role should be correct (admin/trader/viewer).
        # Fallback to "viewer" only if something unexpected happens.
        role = clerk_user.role if clerk_user.role in ("admin", "trader", "viewer") else "viewer"
        log.info(f"Clerk JWT verified: sub={clerk_user.sub}, role={role}")
        return TokenData(
            sub=clerk_user.sub,
            role=role,
            org_id=clerk_user.org_id,
        )
    except HTTPException as e:
        # Clerk verification failed with a known HTTP error (e.g. expired token, invalid signature)
        # Re-raise this so the caller gets a proper 401 instead of falling through to local JWT
        log.warning(f"Clerk token verification failed (HTTP {e.status_code}): {e.detail}")
        raise
    except Exception as e:
        # Unexpected error — log it and fall through to local JWT
        log.error(f"Unexpected error during Clerk verification: {type(e).__name__}: {e}")
        return None


async def get_authed_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    api_key: Optional[str] = Depends(_api_key_hdr),
) -> TokenData:
    """
    Unified auth dependency that tries Clerk JWT first, then falls back
    to the traditional JWT/API-key auth.

    Priority order:
      1. Bearer token → try Clerk JWKS verification (dynamic issuer)
      2. Bearer token → try local JWT_SECRET_KEY verification
      3. X-API-Key header
      4. AUTH_ENABLED=false → dev mode (synthetic admin)

    This allows both Clerk-issued tokens AND locally-signed tokens to work,
    making the transition seamless.
    """
    # Dev mode bypass
    if not AUTH_ENABLED:
        return TokenData(sub="dev", role="admin")

    # API key check (fastest)
    if api_key and api_key in API_KEYS:
        return TokenData(sub=f"apikey:{api_key[:8]}...", role="trader")

    # No credentials at all
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide a Bearer JWT or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Try Clerk JWT verification first (dynamic — works with any Clerk instance)
    # NOTE: _try_clerk_verification may raise HTTPException (e.g. expired Clerk token)
    # which we should propagate rather than falling through to local JWT.
    try:
        clerk_result = _try_clerk_verification(token)
        if clerk_result:
            return clerk_result
    except HTTPException:
        raise  # Re-raise Clerk auth errors (expired, invalid signature, etc.)

    # Fall back to local JWT verification
    if JWT_SECRET_KEY:
        return decode_token(token)

    # No auth method worked
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Provide a valid Bearer JWT or X-API-Key header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── WebSocket authentication ──────────────────────────────────────────────────

class WebSocketAuthError(Exception):
    """
    Raised when WebSocket authentication fails before accept().

    The caller should NOT call websocket.accept(). The ASGI server
    will reject the connection automatically since it was never accepted.
    """
    def __init__(self, reason: str = "Unauthorised"):
        self.reason = reason
        super().__init__(reason)


async def ws_auth(
    websocket: WebSocket,
    token: Optional[str] = None,
    api_key: Optional[str] = None,
) -> TokenData:
    """
    Authenticate a WebSocket connection BEFORE calling accept().

    Checks (in order):
      1. AUTH_ENABLED=false → dev mode, always accept
      2. ?api_key=<key> query parameter
      3. ?token=<jwt> query parameter  (Clerk JWT → local JWT)
      4. X-API-Key header

    Raises WebSocketAuthError if authentication fails.
    The caller should NOT call websocket.accept() after this exception.
    The ASGI server will reject the unaccepted connection automatically.

    Usage:
        @router.websocket("/ws/{symbol}")
        async def ws_handler(
            websocket: WebSocket,
            symbol: str,
            token: Optional[str] = Query(None),
            api_key: Optional[str] = Query(None),
        ):
            user = await ws_auth(websocket, token, api_key)
            await websocket.accept()
            ...
    """
    if not AUTH_ENABLED:
        return TokenData(sub="dev", role="admin")

    # Query-param API key
    if api_key and api_key in API_KEYS:
        return TokenData(sub=f"apikey:{api_key[:8]}...", role="trader")

    # Query-param JWT / Clerk JWT
    if token:
        # Try Clerk verification first (dynamic)
        try:
            clerk_result = _try_clerk_verification(token)
            if clerk_result:
                return clerk_result
        except HTTPException:
            raise WebSocketAuthError("Clerk token verification failed")

        # Fall back to local JWT
        if JWT_SECRET_KEY:
            try:
                return decode_token(token)
            except HTTPException:
                raise WebSocketAuthError("Invalid token")

        raise WebSocketAuthError("No auth method available")

    # Header API key
    hdr_key = websocket.headers.get("x-api-key")
    if hdr_key and hdr_key in API_KEYS:
        return TokenData(sub=f"apikey:{hdr_key[:8]}...", role="trader")

    raise WebSocketAuthError("Unauthorised — provide ?token=<jwt> or ?api_key=<key>")


# ── Login endpoint helper ─────────────────────────────────────────────────────

def make_login_response(sub: str, role: str = "viewer") -> dict:
    """
    Utility for a /auth/token endpoint to return a standard token response.

    Usage:
        @router.post("/auth/token")
        async def login(form: OAuth2PasswordRequestForm = Depends()):
            # Validate credentials here ...
            return make_login_response(sub=form.username, role="trader")
    """
    token = create_access_token({"sub": sub, "role": role})
    return {
        "access_token": token,
        "token_type":   "bearer",
        "expires_in":   JWT_EXPIRE_MINS * 60,
        "sub":          sub,
        "role":         role,
    }
