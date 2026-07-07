"""
Enhanced authentication configuration for LightningAI deployments.

Provides fine-grained control over authentication methods:
- FULL: JWT + API Keys + Clerk (default)
- API_KEY_ONLY: Only API keys (recommended for LightningAI)
- DISABLED: No authentication (development only)
"""

import os
from enum import Enum
from typing import Optional

class AuthMode(Enum):
    """Authentication modes for different deployment scenarios."""
    FULL = "full"          # JWT + API Keys + Clerk
    API_KEY_ONLY = "api_key_only"  # Only API keys (recommended for LightningAI)
    DISABLED = "disabled"   # No authentication (development only)

def get_auth_mode() -> AuthMode:
    """Get authentication mode from environment variable."""
    # Check AUTH_MODE first
    mode_str = os.getenv("AUTH_MODE", "").lower()
    
    if mode_str:
        try:
            return AuthMode(mode_str)
        except ValueError:
            pass  # Fall through to backward compatibility
    
    # Check AUTH_ENABLED for backward compatibility
    auth_enabled = os.getenv("AUTH_ENABLED", "true").lower()
    if auth_enabled == "false":
        return AuthMode.DISABLED
    else:
        return AuthMode.FULL

def is_auth_enabled() -> bool:
    """Check if any authentication is enabled."""
    mode = get_auth_mode()
    return mode != AuthMode.DISABLED

def is_jwt_enabled() -> bool:
    """Check if JWT authentication is enabled."""
    mode = get_auth_mode()
    return mode == AuthMode.FULL

def is_api_key_enabled() -> bool:
    """Check if API key authentication is enabled."""
    mode = get_auth_mode()
    return mode != AuthMode.DISABLED

def is_clerk_enabled() -> bool:
    """Check if Clerk authentication is enabled."""
    mode = get_auth_mode()
    return mode == AuthMode.FULL

def get_mode_description() -> str:
    """Get human-readable description of current auth mode."""
    mode = get_auth_mode()
    descriptions = {
        AuthMode.FULL: "Full authentication: JWT + API Keys + Clerk",
        AuthMode.API_KEY_ONLY: "API Keys only (recommended for LightningAI)",
        AuthMode.DISABLED: "No authentication (development only)"
    }
    return descriptions.get(mode, "Unknown auth mode")

def validate_auth_config() -> Optional[str]:
    """Validate authentication configuration and return warnings."""
    mode = get_auth_mode()
    warnings = []
    
    # Check for production deployment with insecure modes
    deploy_env = os.getenv("DEPLOY_ENV", os.getenv("NODE_ENV", "development")).lower()
    if deploy_env in ("production", "staging"):
        if mode == AuthMode.DISABLED:
            warnings.append("AUTH_MODE=disabled in production - this is insecure!")
        elif mode == AuthMode.API_KEY_ONLY:
            warnings.append("Running in API_KEY_ONLY mode in production")
    
    # Check API keys configuration
    if is_api_key_enabled() and not os.getenv("API_KEYS"):
        warnings.append("API_KEYS not configured but authentication is enabled")
    
    # Check JWT configuration
    if is_jwt_enabled() and not os.getenv("JWT_SECRET_KEY"):
        warnings.append("JWT_SECRET_KEY not configured but JWT auth is enabled")
    
    return "; ".join(warnings) if warnings else None