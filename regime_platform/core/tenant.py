"""
Multi-Tenant Key Helper
═══════════════════════

Provides a simple utility for namespacing storage keys (Supabase
`nt_system_config` rows, formerly Redis keys) by organisation (org_id)
or, as a fallback, by user_id.

When Clerk Organisations are enabled the JWT carries an `org_id` claim.
We use that to scope data so that different orgs sharing the same store
never collide.

Key conventions:
  - org_id present  -> ``org:<org_id>:<base_key>``
  - no org_id, user_id present -> ``user:<user_id>:<base_key>``
  - neither -> ``<base_key>`` (global scope, backwards compatible)

Usage
─────
  from regime_platform.core.tenant import tenant_key

  key = tenant_key("noble:halt:global", org_id="org_123")
  # -> "org:org_123:noble:halt:global"

  key = tenant_key("noble:halt:global", user_id="user_456")
  # -> "user:user_456:noble:halt:global"

  key = tenant_key("noble:halt:global")
  # -> "noble:halt:global"
"""

from __future__ import annotations

from typing import Optional


def tenant_key(
    base_key: str,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """
    Namespace a storage key with org_id (or user_id as fallback).

    Parameters
    ──────────
    base_key : str
        The original key (e.g. ``"noble:halt:global"``).
    org_id : str, optional
        Clerk Organisation ID. Takes precedence when present.
    user_id : str, optional
        Fallback scoping when org_id is not available.

    Returns
    ───────
    str
        Namespaced key, or the original key when neither id is provided.
    """
    if org_id:
        return f"org:{org_id}:{base_key}"
    if user_id:
        return f"user:{user_id}:{base_key}"
    return base_key
