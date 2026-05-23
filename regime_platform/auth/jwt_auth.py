"""
JWT Authentication
═══════════════════

Provides JWT-based authentication for:
  • HTTP endpoints  — FastAPI Depends(get_current_user)
  • WebSocket       — Token passed as ?token= query param
  • API key fallback — X-API-Key header for service-to-service calls
  • SaaS API keys   — nt_live_* keys backed by Supabase api_keys table

Configuration (environment variables)
──────────────────────────────────────
  JWT_SECRET_KEY   — signing secret (required; no default for security)
  JWT_ALGORITHM    — default "HS256"
  JWT_EXPIRE_MINS  — token lifetime in minutes (default 60)
  API_KEYS         — comma-separated valid API keys (optional, static)
  AUTH_ENABLED     — set "false" to disable auth entirely (dev mode)

SaaS API Keys (nt_live_*)
─────────────────────────
  API keys with the nt_live_ prefix are looked up in the Supabase api_keys
  table via SHA-256 hash. They carry the user's role and plan from the key
  record + Clerk metadata, and respect plan-based rate limits.

Usage
─────
  # Protect an HTTP endpoint
  @router.post("/endpoint")
  async def handler(user=Depends(get_current_user)):
      ...

  # Protect a WebSocket endpoint (auth BEFORE accept)
  @router.websocket("/ws/{symbol}")
  async def ws(websocket, symbol, token=Query(None)):
      user = await ws_auth(websocket, token)
      await websocket.accept()
      ...

  # Issue a token (login endpoint)
  token = create_access_token({"sub": "trader1", "role": "viewer"})
"""

from __future__ import annotations

import hashlib
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Query, WebSocket, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyHeader

from regime_platform.core.settings import settings

log = logging.getLogger("regime.auth")

# ── Configuration ─────────────────────────────────────────────────────────────

JWT_SECRET_KEY  = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM   = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINS = int(os.getenv("JWT_EXPIRE_MINS", "60"))
API_KEYS        = set(filter(None, os.getenv("API_KEYS", "").split(",")))
AUTH_ENABLED    = os.getenv("AUTH_ENABLED", "true").lower() != "false"

if AUTH_ENABLED and not JWT_SECRET_KEY:
    log.error(
        "FATAL: JWT_SECRET_KEY is not set but AUTH_ENABLED=true. "
        "All JWT authentication will fail. Set JWT_SECRET_KEY or set AUTH_ENABLED=false for development."
    )
    # In production, refuse to start without a secret key
    _DEPLOY_ENV = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
    if _DEPLOY_ENV in ("production", "staging"):
        raise RuntimeError(
            "JWT_SECRET_KEY is required in production/staging. "
            "Set the JWT_SECRET_KEY environment variable or set AUTH_ENABLED=false."
        )


# ── Token data model ──────────────────────────────────────────────────────────

class TokenData:
    def __init__(
        self,
        sub: str,
        role: str = "viewer",
        exp: Optional[datetime] = None,
        org_id: Optional[str] = None,
        plan: str = "free",
        is_api_key: bool = False,
    ):
        self.sub         = sub       # subject / user ID
        self.role        = role      # "admin" | "trader" | "viewer"
        self.exp         = exp
        self.org_id      = org_id    # Clerk Org ID (None when not using Orgs)
        self.plan        = plan      # "free" | "premium" | "institutional"
        self.is_api_key  = is_api_key  # True if authenticated via SaaS API key

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


# ── SaaS API Key Lookup ─────────────────────────────────────────────────────

# In-memory cache for DB-backed API key lookups (key_hash → TokenData)
# Avoids hitting Supabase on every request for the same key.
_api_key_cache: dict[str, tuple[TokenData, float]] = {}
_API_KEY_CACHE_TTL = 60  # seconds — re-validate keys every minute


def _hash_api_key(api_key: str) -> str:
    """Hash an API key using SHA-256 + pepper (matches frontend api-keys.js)."""
    pepper = getattr(settings, "SUPABASE_ENCRYPTION_KEY", None) or "default-pepper-change-me"
    return hashlib.sha256((api_key + pepper).encode()).hexdigest()


def _lookup_saas_api_key(api_key: str) -> Optional[TokenData]:
    """
    Look up a SaaS API key (nt_live_*) in the Supabase api_keys table.
    Returns TokenData if valid, None otherwise.
    Results are cached for 60 seconds to avoid DB load.
    """
    if not api_key.startswith("nt_live_"):
        return None

    key_hash = _hash_api_key(api_key)

    # Check cache
    now = _time.time()
    cached = _api_key_cache.get(key_hash)
    if cached and (now - cached[1]) < _API_KEY_CACHE_TTL:
        return cached[0]

    # DB lookup
    try:
        import psycopg2
        db_url = getattr(settings, "DATABASE_URL", None)
        if not db_url:
            log.warning("[api-keys] DATABASE_URL not configured — cannot look up SaaS API keys")
            return None

        conn = psycopg2.connect(db_url)
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT clerk_user_id, plan_at_creation, role_at_creation, expires_at, is_active, rotation_grace_until
                   FROM api_keys
                   WHERE key_hash = %s AND is_active = true""",
                (key_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None

            clerk_user_id, plan, role, expires_at, is_active, grace_until = row

            # Check expiry
            if expires_at and expires_at < datetime.now(timezone.utc):
                # Auto-expire the key
                cur.execute(
                    "UPDATE api_keys SET is_active = false, revoked_at = now() WHERE key_hash = %s",
                    (key_hash,),
                )
                conn.commit()
                return None

            # Check rotation grace period
            if grace_until and grace_until < datetime.now(timezone.utc):
                cur.execute(
                    "UPDATE api_keys SET is_active = false, revoked_at = now() WHERE key_hash = %s",
                    (key_hash,),
                )
                conn.commit()
                return None

            # Update last_used_at (fire-and-forget, in same transaction)
            cur.execute(
                "UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s",
                (key_hash,),
            )
            conn.commit()

            # Build TokenData
            token_data = TokenData(
                sub=clerk_user_id,
                role=role or "viewer",
                plan=plan or "free",
                is_api_key=True,
            )

            # Cache it
            _api_key_cache[key_hash] = (token_data, now)
            return token_data

        finally:
            conn.close()

    except Exception as e:
        log.error(f"[api-keys] DB lookup failed: {e}")
        # Return cached value even if expired, better than failing
        if cached:
            return cached[0]
        return None


# ── HTTP authentication dependencies ─────────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    api_key: Optional[str] = Depends(_api_key_hdr),
) -> TokenData:
    """
    FastAPI dependency for HTTP endpoint authentication.

    Accepts:
      - Bearer JWT token in Authorization header
      - X-API-Key header (SaaS nt_live_* or static service key)

    If AUTH_ENABLED=false, returns a synthetic admin user (dev mode).
    """
    if not AUTH_ENABLED:
        # Dev mode — only allowed in development, never in production
        _DEPLOY_ENV = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
        if _DEPLOY_ENV in ("production", "staging"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="AUTH_ENABLED=false is not allowed in production/staging.",
            )
        log.warning("AUTH_ENABLED=false: returning dev admin user (development only)")
        return TokenData(sub="dev", role="admin")

    # API key check: try SaaS API key first, then static API_KEYS
    if api_key:
        # SaaS API key (nt_live_*) — DB-backed
        saas_user = _lookup_saas_api_key(api_key)
        if saas_user:
            return saas_user
        # Static API key (service-to-service)
        if api_key in API_KEYS:
            return TokenData(sub=f"apikey:{api_key[:8]}...", role="trader")

    # JWT check
    if credentials and credentials.scheme.lower() == "bearer":
        return decode_token(credentials.credentials)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Provide a Bearer JWT or X-API-Key header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


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
    try:
        import jwt as _jwt
        payload = _jwt.decode(
            token,
            key="",
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["RS256", "HS256"],
        )
    except Exception as e:
        log.debug(f"Cannot decode token for issuer check: {e}")
        return None

    iss = payload.get("iss", "")
    if "clerk" not in iss.lower() and ".accounts.dev" not in iss.lower():
        log.debug(f"Token issuer '{iss}' is not a Clerk domain — skipping Clerk verification")
        return None

    # Issuer is a Clerk domain — attempt full verification
    try:
        clerk_user = verify_clerk_token(token)
        role = clerk_user.role if clerk_user.role in ("admin", "trader", "viewer") else "viewer"
        log.info(f"Clerk JWT verified: sub={clerk_user.sub}, role={role}")
        return TokenData(
            sub=clerk_user.sub,
            role=role,
            org_id=clerk_user.org_id,
        )
    except HTTPException as e:
        log.warning(f"Clerk token verification failed (HTTP {e.status_code}): {e.detail}")
        raise
    except Exception as e:
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
      3. X-API-Key header → SaaS API key (nt_live_*) DB lookup
      4. X-API-Key header → static API key (service-to-service)
      5. AUTH_ENABLED=false → dev mode (synthetic admin)
    """
    # Dev mode bypass (production blocked)
    if not AUTH_ENABLED:
        _DEPLOY_ENV = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
        if _DEPLOY_ENV in ("production", "staging"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="AUTH_ENABLED=false is not allowed in production/staging.",
            )
        log.warning("AUTH_ENABLED=false: returning dev admin user (development only)")
        return TokenData(sub="dev", role="admin")

    # API key check: try SaaS API key first, then static API_KEYS
    if api_key:
        # SaaS API key (nt_live_*) — DB-backed
        saas_user = _lookup_saas_api_key(api_key)
        if saas_user:
            return saas_user
        # Static API key (service-to-service)
        if api_key in API_KEYS:
            return TokenData(sub=f"apikey:{api_key[:8]}...", role="trader")

    # No credentials at all
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide a Bearer JWT or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Try Clerk JWT verification first
    try:
        clerk_result = _try_clerk_verification(token)
        if clerk_result:
            return clerk_result
    except HTTPException:
        raise

    # Fall back to local JWT verification
    if JWT_SECRET_KEY:
        return decode_token(token)

    # No auth method worked
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Provide a valid Bearer JWT or X-API-Key header.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── Role-based dependencies ──────────────────────────────────────────────────

async def require_write(user: TokenData = Depends(get_authed_user)) -> TokenData:
    """Dependency that additionally requires trader or admin role."""
    if not user.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{user.role}' does not have write access.",
        )
    return user


async def require_admin(user: TokenData = Depends(get_authed_user)) -> TokenData:
    """Dependency that requires admin role."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required.",
        )
    return user


# ── WebSocket authentication ─────────────────────────────────────────────────

class WebSocketAuthError(Exception):
    """
    Raised when WebSocket authentication fails before accept().
    The caller should NOT call websocket.accept().
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
      2. ?api_key=<key> query parameter (SaaS or static)
      3. ?token=<jwt> query parameter (Clerk JWT → local JWT)
      4. X-API-Key header (SaaS or static)
    """
    if not AUTH_ENABLED:
        _DEPLOY_ENV = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
        if _DEPLOY_ENV in ("production", "staging"):
            await websocket.close(code=4001, reason="Auth disabled in production")
            raise HTTPException(status_code=401, detail="Auth disabled in production")
        log.warning("AUTH_ENABLED=false: WS dev mode (development only)")
        return TokenData(sub="dev", role="admin")

    # Query-param API key: try SaaS first, then static
    if api_key:
        saas_user = _lookup_saas_api_key(api_key)
        if saas_user:
            return saas_user
        if api_key in API_KEYS:
            return TokenData(sub=f"apikey:{api_key[:8]}...", role="trader")

    # Query-param JWT / Clerk JWT
    if token:
        try:
            clerk_result = _try_clerk_verification(token)
            if clerk_result:
                return clerk_result
        except HTTPException:
            raise WebSocketAuthError("Clerk token verification failed")

        if JWT_SECRET_KEY:
            try:
                return decode_token(token)
            except HTTPException:
                raise WebSocketAuthError("Invalid token")

        raise WebSocketAuthError("No auth method available")

    # Header API key: try SaaS first, then static
    hdr_key = websocket.headers.get("x-api-key")
    if hdr_key:
        saas_user = _lookup_saas_api_key(hdr_key)
        if saas_user:
            return saas_user
        if hdr_key in API_KEYS:
            return TokenData(sub=f"apikey:{hdr_key[:8]}...")

    raise WebSocketAuthError("Unauthorised — provide ?token=<jwt> or ?api_key=<key>")


# ── Login endpoint helper ─────────────────────────────────────────────────────

def make_login_response(sub: str, role: str = "viewer") -> dict:
    """
    Utility for a /auth/token endpoint to return a standard token response.
    """
    token = create_access_token({"sub": sub, "role": role})
    return {
        "access_token": token,
        "token_type":   "bearer",
        "expires_in":   JWT_EXPIRE_MINS * 60,
        "sub":          sub,
        "role":         role,
    }
