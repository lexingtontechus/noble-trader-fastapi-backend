"""
Clerk JWT Authentication
═════════════════════════

Provides Clerk-based JWT authentication for FastAPI.
Uses Clerk's publishable and secret keys from environment variables.

Configuration (environment variables)
──────────────────────────────────────
  CLERK_PUBLISHABLE_KEY  - Clerk publishable key (for client-side)
  CLERK_SECRET_KEY      - Clerk secret key (for server-side verification)
  CLERK_JWKS_URL        - Optional: explicit JWKS URL (overrides auto-detection)

Clerk Token Verification
─────────────────────────
Clerk issues JWT tokens that can be verified using the JWKS endpoint.
The JWKS URL is dynamically extracted from the JWT's issuer claim,
so tokens from ANY Clerk instance can be verified.

Usage
─────
  # Verify a Clerk JWT token
  from regime_platform.auth.clerk_auth import verify_clerk_token, get_current_clerk_user

  token = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..."
  user_data = verify_clerk_token(token)

  # Or use as FastAPI dependency
  from regime_platform.auth.clerk_auth import get_current_clerk_user

  @router.get("/protected")
  async def protected_route(user = Depends(get_current_clerk_user)):
      return {"user_id": user.sub, "role": user.role}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from regime_platform.core.settings import settings
from regime_platform.core.circuit_breaker import circuit, CircuitOpenError

try:
    import jwt

    JWT_AVAILABLE = True
except ImportError:
    JWT_AVAILABLE = False
    logging.warning(
        "PyJWT not installed. Clerk auth will not work. Install with: pip install PyJWT[crypto]"
    )

log = logging.getLogger("regime.auth.clerk")

# ── Configuration ─────────────────────────────────────────────────────────────

CLERK_PUBLISHABLE_KEY = settings.CLERK_PUBLISHABLE_KEY
CLERK_SECRET_KEY = settings.CLERK_SECRET_KEY
CLERK_JWKS_URL = settings.CLERK_JWKS_URL or None  # Optional: explicit JWKS URL

# Clerk uses RS256 algorithm for signing JWTs
CLERK_JWT_ALGORITHM = "RS256"

# Enable/disable Clerk authentication
CLERK_AUTH_ENABLED = settings.clerk_auth_enabled

if not CLERK_AUTH_ENABLED:
    log.warning(
        "CLERK_SECRET_KEY is not set. Clerk authentication will be disabled. "
        "Set CLERK_SECRET_KEY in .env.local to enable Clerk auth."
    )

# ── Role Cache (privateMetadata lookup) ───────────────────────────────────────
# Clerk JWTs don't include the user's role by default - it's stored in
# privateMetadata. We look it up via the Clerk REST API and cache it.

_role_cache: dict[str, tuple[str, float]] = {}   # user_id -> (role, expiry)
ROLE_CACHE_TTL = 300  # 5 minutes


def _get_role_from_clerk_api(user_id: str) -> str:
    """
    Look up a user's role from Clerk privateMetadata via the REST API.

    The role is stored at ``privateMetadata.role`` in the Clerk user object.
    Falls back to ``"viewer"`` if the API call fails or no role is found.

    Results are cached for ROLE_CACHE_TTL seconds to avoid hammering Clerk.
    """
    import httpx

    now = datetime.now(timezone.utc).timestamp()

    # Check cache first
    if user_id in _role_cache:
        cached_role, expiry = _role_cache[user_id]
        if now < expiry:
            return cached_role

    if not CLERK_SECRET_KEY:
        log.debug("No CLERK_SECRET_KEY - cannot look up role, defaulting to viewer")
        return "viewer"

    try:
        resp = httpx.get(
            f"https://api.clerk.com/v1/users/{user_id}",
            headers={
                "Authorization": f"Bearer {CLERK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract role from privateMetadata
        private_meta = data.get("private_metadata", {}) or {}
        role = private_meta.get("role", "viewer")

        # Validate against known roles
        valid_roles = {"admin", "trader", "viewer"}
        if role not in valid_roles:
            log.warning(
                f"User {user_id} has invalid role '{role}' in privateMetadata, defaulting to viewer"
            )
            role = "viewer"

        # Cache it
        _role_cache[user_id] = (role, now + ROLE_CACHE_TTL)
        log.debug(f"Looked up role for {user_id}: {role}")
        return role

    except Exception as e:
        log.warning(f"Failed to look up role for {user_id}: {e}")
        # If we had a cached value, use it even if expired
        if user_id in _role_cache:
            return _role_cache[user_id][0]
        return "viewer"

# ── JWKS Cache ────────────────────────────────────────────────────────────────

_jwks_cache: dict[str, dict] = {}  # issuer -> JWKS
_jwks_cache_ttl: dict[str, float] = {}  # issuer -> expiry timestamp
JWKS_CACHE_SECONDS = 3600  # 1 hour cache


# ── Token Data Model ────────────────────────────────────────────────────────


class ClerkTokenData:
    """
    Represents a verified Clerk user from a JWT token.

    Attributes:
        sub: User ID (Clerk user ID)
        role: User role (extracted from token or default)
        email: User email address
        first_name: User's first name
        last_name: User's last name
        username: User's username
        exp: Token expiration timestamp
        iat: Token issued at timestamp
        claims: Full token payload
    """

    def __init__(
        self,
        sub: str,
        role: str = "viewer",
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        username: Optional[str] = None,
        exp: Optional[datetime] = None,
        iat: Optional[datetime] = None,
        claims: Optional[dict] = None,
        org_id: Optional[str] = None,
    ):
        self.sub = sub
        self.role = role
        self.email = email
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.exp = exp
        self.iat = iat
        self.claims = claims or {}
        self.org_id = org_id

    @property
    def is_admin(self) -> bool:
        """Check if user has admin role."""
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        """Check if user has write access (admin or trader)."""
        return self.role in ("admin", "trader")

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON responses."""
        return {
            "sub": self.sub,
            "role": self.role,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "org_id": self.org_id,
        }


# ── Token Verification ──────────────────────────────────────────────────────


def _get_issuer_from_token(token: str) -> Optional[str]:
    """
    Extract the issuer (iss) claim from a JWT token without verification.
    Returns the issuer URL or None if it can't be extracted.
    """
    import jwt as _jwt

    try:
        # Decode the payload without verification to get the issuer
        payload = _jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
            algorithms=["RS256", "HS256"],
        )
        return payload.get("iss")
    except Exception as e:
        log.debug(f"Could not extract issuer from token: {e}")
        return None


def _issuer_to_jwks_url(issuer: str) -> str:
    """
    Convert a Clerk issuer URL to its JWKS endpoint.
    
    Clerk issuer format: https://<instance>.clerk.accounts.dev
    or: https://clerk.<domain>.com
    
    JWKS format: https://<instance>.clerk.accounts.dev/.well-known/jwks.json
    """
    if not issuer:
        return ""
    
    # Remove trailing slash
    issuer = issuer.rstrip("/")
    
    # Append JWKS path
    return f"{issuer}/.well-known/jwks.json"


def _get_clerk_jwks(issuer: Optional[str] = None) -> dict:
    """
    Fetch Clerk's JWKS (JSON Web Key Set) from the appropriate endpoint.

    The JWKS URL is determined by:
    1. CLERK_JWKS_URL env var (explicit override)
    2. Dynamic: derived from the JWT's issuer claim
    3. Fallback: derived from CLERK_PUBLISHABLE_KEY

    Results are cached for 1 hour per issuer.
    """
    import httpx

    # Determine the JWKS URL
    if CLERK_JWKS_URL:
        jwks_url = CLERK_JWKS_URL
    elif issuer:
        jwks_url = _issuer_to_jwks_url(issuer)
    else:
        # Fallback: try to derive from publishable key or use a default
        jwks_url = "https://large-shark-21.clerk.accounts.dev/.well-known/jwks.json"

    if not jwks_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cannot determine Clerk JWKS URL. Set CLERK_JWKS_URL or CLERK_PUBLISHABLE_KEY.",
        )

    # Check cache
    cache_key = jwks_url
    now = datetime.now(timezone.utc).timestamp()
    if cache_key in _jwks_cache and _jwks_cache_ttl.get(cache_key, 0) > now:
        return _jwks_cache[cache_key]

    try:
        # Circuit breaker protects against repeated Clerk outages
        cb = circuit("clerk_jwks", failure_threshold=3, reset_timeout=60.0)

        if cb.is_open:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Clerk authentication temporarily unavailable (circuit open)",
            )

        response = httpx.get(jwks_url, timeout=5.0)
        response.raise_for_status()
        jwks_data = response.json()

        # Record success
        cb._on_success()

        # Cache the result
        _jwks_cache[cache_key] = jwks_data
        _jwks_cache_ttl[cache_key] = now + JWKS_CACHE_SECONDS

        log.info(f"Fetched Clerk JWKS from {jwks_url} (issuer={issuer})")
        return jwks_data
    except HTTPException:
        raise
    except Exception as e:
        # Record failure in circuit breaker
        cb._on_failure(e)
        log.error(f"Failed to fetch Clerk JWKS from {jwks_url}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch Clerk JWKS from {jwks_url}",
        )


def _find_matching_key(jwks: dict, kid: Optional[str] = None) -> Optional[dict]:
    """
    Find a matching key in JWKS by key ID (kid).
    If no kid specified, use the first RS256 key.
    """
    keys = jwks.get("keys", [])

    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return key
        return None

    # Return first RS256 key if no specific kid
    for key in keys:
        if key.get("kty") == "RSA" and key.get("use") == "sig":
            return key

    return None


def _verify_token_with_jwks(token: str, jwks: dict, kid: Optional[str] = None) -> dict:
    """
    Verify a JWT token using JWKS keys.
    """
    import jwt
    from jwt import PyJWK

    jwk = _find_matching_key(jwks, kid)
    if not jwk:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No matching key found in JWKS for kid: {kid}",
        )

    # Create PyJWK from the key
    public_key = PyJWK.from_dict(jwk)

    try:
        payload = jwt.decode(
            token,
            public_key.key,
            algorithms=[CLERK_JWT_ALGORITHM],
            options={"verify_exp": True, "verify_iat": True},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Clerk API Enrichment ──────────────────────────────────────────────────────


# Cache enriched user profiles to avoid hitting Clerk API on every request.
# Keyed by Clerk user ID (sub). Values expire after _ENRICH_CACHE_SECONDS.
_enrich_cache: dict[str, dict] = {}
_enrich_cache_ttl: dict[str, float] = {}
_ENRICH_CACHE_SECONDS = 300  # 5 minutes


def _enrich_from_clerk_api(sub: str) -> dict:
    """
    Call the Clerk Backend API to fetch user profile info.

    Used as a fallback when the JWT does not contain email/name claims
    (i.e. the 'server' JWT template has not been configured yet).

    Results are cached for 5 minutes per user to limit API calls.

    Parameters
    ----------
    sub : str
        The Clerk user ID (from the JWT's ``sub`` claim).

    Returns
    -------
    dict
        Enriched fields: email, first_name, last_name, username, role.
        Empty dict on failure.
    """
    import httpx

    now = datetime.now(timezone.utc).timestamp()

    # Check cache
    if sub in _enrich_cache and _enrich_cache_ttl.get(sub, 0) > now:
        return _enrich_cache[sub]

    if not CLERK_SECRET_KEY:
        return {}

    try:
        res = httpx.get(
            f"https://api.clerk.com/v1/users/{sub}",
            headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
            timeout=5.0,
        )
        if res.status_code != 200:
            log.debug(f"Clerk API enrichment failed for {sub}: HTTP {res.status_code}")
            return {}

        user = res.json()

        # Extract primary email
        email = None
        primary_id = user.get("primary_email_address_id")
        for ea in user.get("email_addresses", []):
            if ea.get("id") == primary_id:
                email = ea.get("email_address")
                break

        # Extract role from private_metadata (matching our frontend convention)
        role = None
        private_meta = user.get("private_metadata", {})
        if isinstance(private_meta, dict):
            role = private_meta.get("role")

        enriched = {
            "email": email,
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "username": user.get("username"),
            "role": role,
        }

        # Cache the result
        _enrich_cache[sub] = enriched
        _enrich_cache_ttl[sub] = now + _ENRICH_CACHE_SECONDS

        log.debug(f"Enriched Clerk user {sub} from API: email={email}, role={role}")
        return enriched

    except Exception as e:
        log.warning(f"Clerk API enrichment failed for {sub}: {e}")
        return {}


# ── Token Verification ──────────────────────────────────────────────────────


def verify_clerk_token(token: str) -> ClerkTokenData:
    """
    Verify a Clerk-issued JWT token.

    This function:
    1. Decodes the JWT header to get the key ID (kid)
    2. Extracts the issuer (iss) from the token payload (without verification)
    3. Fetches the appropriate JWKS based on the issuer
    4. Finds the matching key
    5. Verifies the token signature and expiration
    6. Extracts user information

    The JWKS URL is dynamically determined from the JWT's issuer claim,
    so tokens from ANY Clerk instance can be verified.

    Parameters
    ----------
    token : str
        The JWT token string

    Returns
    -------
    ClerkTokenData
        Verified user data from the token

    Raises
    ------
    HTTPException
        If token is invalid, expired, or verification fails
    """
    if not CLERK_AUTH_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clerk authentication is not enabled",
        )

    if not JWT_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PyJWT is not installed",
        )

    try:
        import jwt

        # Decode header to get kid without verification
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")

        # Extract issuer from token payload (without verification)
        # This allows us to dynamically determine the JWKS URL
        issuer = _get_issuer_from_token(token)

        # Fetch JWKS based on the issuer
        jwks = _get_clerk_jwks(issuer=issuer)

        # Verify token
        payload = _verify_token_with_jwks(token, jwks, kid)

        # Extract user information
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing 'sub' claim",
            )

        # Extract Clerk-specific claims
        # NOTE: Clerk JWTs do NOT include a 'role' claim by default.
        # The role is stored in the user's privateMetadata on the Clerk side.
        # We first check the JWT claims (in case a JWT template adds it),
        # then fall back to a Clerk API lookup of privateMetadata.role.
        role = payload.get("role")
        org_id = payload.get("org_id")

        if not role or role not in ("admin", "trader", "viewer"):
            # Role not in JWT - look it up from Clerk privateMetadata
            role = _get_role_from_clerk_api(sub)

        # Extract user metadata if available in token
        email = payload.get("email") or payload.get("email_addresses", [{}])[0].get(
            "email_address"
        )
        first_name = payload.get("first_name") or payload.get("firstName")
        last_name = payload.get("last_name") or payload.get("lastName")
        username = payload.get("username")

        # ── Enrichment: if claims are missing, call Clerk API ──────────
        # When the JWT template is not configured, the default token
        # only has sub/iss/exp. We call GET /v1/users/{sub} to fill in
        # email, name, and role from the user's Clerk profile.
        needs_enrichment = not email and not first_name and not username
        if needs_enrichment and CLERK_SECRET_KEY:
            enriched = _enrich_from_clerk_api(sub)
            if enriched:
                email = email or enriched.get("email")
                first_name = first_name or enriched.get("first_name")
                last_name = last_name or enriched.get("last_name")
                username = username or enriched.get("username")
                # Only override role if it's the default and Clerk has one
                if role == "viewer" and enriched.get("role"):
                    role = enriched["role"]

        # Convert timestamps
        exp = payload.get("exp")
        iat = payload.get("iat")

        if exp:
            exp = datetime.fromtimestamp(exp, tz=timezone.utc)
        if iat:
            iat = datetime.fromtimestamp(iat, tz=timezone.utc)

        return ClerkTokenData(
            sub=str(sub),
            role=str(role),
            email=str(email) if email else None,
            first_name=str(first_name) if first_name else None,
            last_name=str(last_name) if last_name else None,
            username=str(username) if username else None,
            exp=exp,
            iat=iat,
            claims=payload,
            org_id=str(org_id) if org_id else None,
        )

    except HTTPException:
        raise
    except jwt.DecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token verification failed: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI Dependencies ────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def get_current_clerk_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> ClerkTokenData:
    """
    FastAPI dependency for Clerk JWT authentication.

    Extracts and verifies a Clerk JWT from the Authorization header.

    Usage:
        @router.get("/protected")
        async def protected_route(user = Depends(get_current_clerk_user)):
            return {"user_id": user.sub, "email": user.email}

    Returns
    -------
    ClerkTokenData
        Verified user data

    Raises
    ------
    HTTPException
        If authentication fails
    """
    if not CLERK_AUTH_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Clerk authentication is not enabled",
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization scheme must be Bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    return verify_clerk_token(token)


async def get_optional_clerk_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[ClerkTokenData]:
    """
    FastAPI dependency for optional Clerk JWT authentication.

    Returns None if no token is provided, otherwise verifies and returns user.

    Usage:
        @router.get("/public-or-private")
        async def route(user = Depends(get_optional_clerk_user)):
            if user:
                return {"message": f"Hello, {user.email}"}
            return {"message": "Hello, guest"}
    """
    if not CLERK_AUTH_ENABLED:
        return None

    if credentials is None:
        return None

    if credentials.scheme.lower() != "bearer":
        return None

    try:
        token = credentials.credentials
        return verify_clerk_token(token)
    except HTTPException:
        return None


async def require_clerk_admin(
    user: ClerkTokenData = Depends(get_current_clerk_user),
) -> ClerkTokenData:
    """
    Dependency that requires admin role from Clerk-authenticated user.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


async def require_clerk_write(
    user: ClerkTokenData = Depends(get_current_clerk_user),
) -> ClerkTokenData:
    """
    Dependency that requires write access (admin or trader role) from Clerk-authenticated user.
    """
    if not user.can_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write access required (admin or trader role)",
        )
    return user


# ── Token Creation (Server-side) ─────────────────────────────────────────────


def create_local_token(
    sub: str,
    role: str = "viewer",
    org_id: Optional[str] = None,
    email: Optional[str] = None,
    expires_in_minutes: int = 60,
) -> str:
    """
    Create a locally-signed JWT token (HS256) for the FastAPI backend.

    This does NOT create a Clerk-compatible token - Clerk uses RS256 with
    asymmetric keys that we don't have. Instead, this creates a token that
    the backend's local JWT auth (jwt_auth.decode_token) can verify.

    Use this for server-to-server calls or when Clerk is unavailable.
    For user-facing auth, always use Clerk-issued tokens.
    """
    if not JWT_AVAILABLE:
        raise RuntimeError("PyJWT is not installed")

    from regime_platform.core.settings import settings as _settings

    if not _settings.JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured")

    from datetime import timedelta
    from jose import jwt as jose_jwt

    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_in_minutes)

    payload = {
        "sub": sub,
        "role": role,
        "exp": exp,
        "iat": now,
    }

    if org_id:
        payload["org_id"] = org_id
    if email:
        payload["email"] = email

    return jose_jwt.encode(
        payload,
        _settings.JWT_SECRET_KEY,
        algorithm=_settings.JWT_ALGORITHM,
    )


def create_clerk_compatible_token(
    sub: str,
    role: str = "viewer",
    email: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    username: Optional[str] = None,
    expires_in_minutes: int = 60,
) -> str:
    """
    DEPRECATED: Use create_local_token() instead.

    This function previously tried to sign tokens with CLERK_SECRET_KEY
    using RS256, which is incorrect - CLERK_SECRET_KEY is an API secret
    string, not an RSA private key. The function now delegates to
    create_local_token() which uses HS256 with JWT_SECRET_KEY.

    For user-facing auth, always use Clerk-issued tokens via the frontend.
    """
    log.warning(
        "create_clerk_compatible_token() is deprecated - use create_local_token()"
    )
    return create_local_token(
        sub=sub,
        role=role,
        email=email,
        expires_in_minutes=expires_in_minutes,
    )


# ── Webhook Verification ────────────────────────────────────────────────────


async def verify_clerk_webhook(
    request: Request,
    secret: Optional[str] = None,
) -> dict:
    """
    Verify a Clerk webhook request.

    Clerk sends webhooks with a signature in the Svix-Signature header.
    This function verifies that the webhook came from Clerk.

    Parameters
    ----------
    request : Request
        FastAPI Request object
    secret : str, optional
        Webhook secret. If not provided, uses CLERK_SECRET_KEY

    Returns
    -------
    dict
        Parsed webhook payload

    Raises
    ------
    HTTPException
        If webhook verification fails
    """
    import hashlib
    import hmac

    webhook_secret = secret or CLERK_SECRET_KEY

    if not webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret not configured",
        )

    svix_signature = request.headers.get("Svix-Signature")
    if not svix_signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Svix-Signature header",
        )

    svix_id = request.headers.get("Svix-Id")
    svix_timestamp = request.headers.get("Svix-Timestamp")

    if not svix_id or not svix_timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Svix-Id or Svix-Timestamp header",
        )

    # Read request body
    body = await request.body()

    # Construct the signed payload
    signed_payload = f"{svix_id}.{svix_timestamp}.{body.decode('utf-8')}"

    # Calculate expected signature
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    # Verify signature (Svix-Signature format: "v1,{signature}")
    provided_signature = (
        svix_signature.split(",v1=")[1] if ",v1=" in svix_signature else svix_signature
    )

    if not hmac.compare_digest(expected_signature, provided_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # Parse and return payload
    import json

    return json.loads(body.decode("utf-8"))
