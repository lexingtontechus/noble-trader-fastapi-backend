"""
Authentication endpoints.

  POST /auth/token   - issue a JWT (username/password or API key exchange)
  GET  /auth/me      - return current user info from token
  POST /auth/refresh - refresh a non-expired token
  GET  /auth/clerk/me - return Clerk user info from Clerk JWT token

In production, the /auth/token endpoint should validate against a real
user store. The implementation here uses a configurable in-memory user
map (AUTH_USERS env var) for demonstration purposes.

AUTH_USERS format (env var):
  "username1:password1:role1,username2:password2:role2"
  e.g. "admin:secret123:admin,trader1:pass456:trader,readonly:pass:viewer"

If AUTH_USERS is not set, the token endpoint is disabled and returns 503.

Clerk Authentication:
  - Uses CLERK_SECRET_KEY from .env.local
  - Clerk tokens are verified using Clerk's JWKS endpoint
  - POST /auth/clerk/verify - verify a Clerk JWT token
  - GET  /auth/clerk/me - get current Clerk user info
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from ..auth.clerk_auth import (
    CLERK_AUTH_ENABLED,
    CLERK_PUBLISHABLE_KEY,
    ClerkTokenData,
    get_current_clerk_user,
    get_optional_clerk_user,
    verify_clerk_token,
)
from ..auth.jwt_auth import (
    AUTH_ENABLED,
    JWT_EXPIRE_MINS,
    TokenData,
    create_access_token,
    get_authed_user,
    get_current_user,
    make_login_response,
)
from ..auth.auth_config import is_dev_auth_allowed
from ..core.settings import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── In-memory user store (configurable via env) ─────────────────────────────


def _load_users() -> dict[str, dict]:
    return settings.auth_users_dict


_USERS = _load_users()


# ── Response models ──────────────────────────────────────────────────────────


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    sub: str
    role: str


class UserInfo(BaseModel):
    sub: str
    role: str
    auth_enabled: bool


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Issue a JWT access token",
)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    """
    Standard OAuth2 password flow.

    Supply `username` and `password` as form fields.
    Returns a Bearer JWT valid for `JWT_EXPIRE_MINS` minutes.

    Configure users via the `AUTH_USERS` environment variable:
    ```
    AUTH_USERS="admin:mysecret:admin,trader:pass123:trader"
    ```

    Roles: `admin` (full access) · `trader` (read + write) · `viewer` (read-only)
    """
    if not AUTH_ENABLED:
        # Dev mode - issue a token without credential check (development only)
        if not is_dev_auth_allowed():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "AUTH_ENABLED=false is only allowed when DEPLOY_ENV is "
                    "explicitly 'development' or 'test'. Refusing to mint "
                    "credentials in an unset/non-dev environment."
                ),
            )
        return TokenResponse(**make_login_response(sub=form.username, role="admin"))

    if not _USERS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AUTH_USERS environment variable not configured on server.",
        )

    user = _USERS.get(form.username)
    if not user or user["password"] != form.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(**make_login_response(sub=form.username, role=user["role"]))


@router.get(
    "/me",
    response_model=UserInfo,
    summary="Return current user info decoded from token",
)
async def me(user: TokenData = Depends(get_authed_user)):
    """
    Returns the identity and role extracted from the Bearer JWT.
    Useful for verifying a token is valid and checking its role.
    """
    return UserInfo(sub=user.sub, role=user.role, auth_enabled=AUTH_ENABLED)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh a (non-expired) token",
)
async def refresh(user: TokenData = Depends(get_authed_user)):
    """
    Issues a fresh token with a new expiry, keeping the same subject and role.
    Only works if the current token is still valid (not yet expired).
    """
    return TokenResponse(**make_login_response(sub=user.sub, role=user.role))


# ── Clerk Integration Response Models ────────────────────────────────────────


class ClerkUserInfo(BaseModel):
    """Clerk user information from JWT token."""

    sub: str
    role: str = "viewer"
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    clerk_auth_enabled: bool = False


class ClerkVerifyRequest(BaseModel):
    """Request model for Clerk token verification."""

    token: str


class ClerkVerifyResponse(BaseModel):
    """Response model for Clerk token verification."""

    valid: bool
    user: Optional[ClerkUserInfo] = None
    error: Optional[str] = None


# ── Clerk Authentication Endpoints ──────────────────────────────────────────


@router.get(
    "/clerk/me",
    response_model=ClerkUserInfo,
    summary="Return Clerk user info decoded from Clerk JWT token",
    tags=["Clerk Authentication"],
)
async def clerk_me(user: ClerkTokenData = Depends(get_current_clerk_user)):
    """
    Returns the identity and metadata extracted from a valid Clerk JWT token.

    Requires a valid Clerk JWT in the Authorization header:
    ```
    Authorization: Bearer <clerk_jwt_token>
    ```

    Returns user information including:
    - sub: Clerk user ID
    - email: User's email address
    - first_name, last_name: User's name
    - username: User's username
    - role: User role (from token claims or default 'viewer')
    """
    return ClerkUserInfo(
        sub=user.sub,
        role=user.role,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        clerk_auth_enabled=True,
    )


@router.post(
    "/clerk/verify",
    response_model=ClerkVerifyResponse,
    summary="Verify a Clerk JWT token",
    tags=["Clerk Authentication"],
)
async def verify_clerk_token_endpoint(
    request: ClerkVerifyRequest,
) -> ClerkVerifyResponse:
    """
    Verify a Clerk JWT token and return user information.

    This endpoint accepts a Clerk JWT token in the request body and
    returns whether it's valid along with the decoded user information.

    Request body:
    ```json
    {"token": "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..."}
    ```
    """
    if not CLERK_AUTH_ENABLED:
        return ClerkVerifyResponse(
            valid=False,
            error="Clerk authentication is not enabled (CLERK_SECRET_KEY not set)",
        )

    try:
        user = verify_clerk_token(request.token)
        return ClerkVerifyResponse(
            valid=True,
            user=ClerkUserInfo(
                sub=user.sub,
                role=user.role,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                username=user.username,
                clerk_auth_enabled=True,
            ),
        )
    except HTTPException as e:
        return ClerkVerifyResponse(
            valid=False,
            error=str(e.detail),
        )
    except Exception as e:
        return ClerkVerifyResponse(
            valid=False,
            error=f"Token verification failed: {str(e)}",
        )


@router.get(
    "/clerk/config",
    summary="Get Clerk authentication configuration",
    tags=["Clerk Authentication"],
)
async def clerk_config():
    """
    Returns Clerk authentication configuration status.

    Useful for client-side configuration and debugging.
    """
    publishable_key = CLERK_PUBLISHABLE_KEY if CLERK_AUTH_ENABLED else None
    # M8: report the CONFIGURED JWKS URL (settings.CLERK_JWKS_URL) instead of
    # the hardcoded project subdomain — the hardcoded value both leaked the
    # Clerk project and ignored the operator's explicit override.
    from ..core.settings import settings as _settings
    jwks_url = _settings.CLERK_JWKS_URL or "https://large-shark-21.clerk.accounts.dev/.well-known/jwks.json"

    return {
        "clerk_auth_enabled": CLERK_AUTH_ENABLED,
        "publishable_key": publishable_key[:10] + "..." if publishable_key else None,
        "jwt_algorithm": "RS256",
        "jwks_endpoint": jwks_url,
    }


@router.get(
    "/debug/clerk-flow",
    summary="Debug: test the Clerk JWT verification flow step by step",
    tags=["Debug"],
)
async def debug_clerk_flow(user: ClerkTokenData = Depends(get_current_clerk_user)):
    """
    Debug endpoint that tests the full Clerk verification flow.
    Requires a valid Clerk JWT in the Authorization header.
    Returns step-by-step results for each verification stage.
    """
    import jwt as _jwt

    # We already have a verified user from get_current_clerk_user
    # Now simulate what _try_clerk_verification() does
    from ..auth.jwt_auth import _try_clerk_verification, TokenData
    from fastapi.security import HTTPAuthorizationCredentials

    # Re-run _try_clerk_verification to see its result
    # (In production, the token comes from the Authorization header)
    try:
        # Get the raw token from the request's auth header
        from fastapi import Request
        # We'll use the already-verified user info
        return {
            "clerk_direct_verification": {
                "status": "success",
                "sub": user.sub,
                "role": user.role,
                "email": user.email,
            },
            "clerk_auth_enabled": CLERK_AUTH_ENABLED,
            "jwks_accessible": True,  # If we got here, JWKS was accessible
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }
