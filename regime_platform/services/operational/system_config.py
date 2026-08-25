"""
System Config KV Helper - thin Supabase-backed key/value store.

Replaces the ad-hoc Redis GET/SET/DELETE calls that mode_manager.py,
kill_switch.py, and reconciler.py each used to duplicate independently
(Redis retired 2026-08-25). Backed by `nt_system_config` (key, value,
category), the same durable table those services already wrote through
to for crash-recovery. Fail-open: every method returns None/False on
error rather than raising — this is best-effort persistence for
operational state, not a source of truth callers should depend on being
available (in-memory state remains the hot path everywhere it's used).
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("regime.system_config")


async def get_config(key: str) -> Optional[str]:
    """Return the stored value for `key`, or None if absent/unreachable."""
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return None
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{settings.SUPABASE_URL}/rest/v1/nt_system_config",
                headers={
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                },
                params={"key": f"eq.{key}", "select": "value"},
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    return rows[0]["value"]
    except Exception as exc:
        logger.debug("system_config: get(%s) failed: %s", key, exc)
    return None


async def set_config(key: str, value: str, category: str = "operational") -> bool:
    """Upsert `value` for `key`. Returns True on a confirmed write."""
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return False
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.SUPABASE_URL}/rest/v1/nt_system_config",
                headers={
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
                json={"key": key, "value": value, "category": category},
            )
            return resp.status_code in (200, 201, 204)
    except Exception as exc:
        logger.debug("system_config: set(%s) failed: %s", key, exc)
        return False


async def delete_config(key: str) -> bool:
    """Delete the row for `key`. Returns True on a confirmed delete."""
    try:
        from regime_platform.core.settings import settings
        if not (settings.SUPABASE_URL and settings.SUPABASE_KEY):
            return False
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(
                f"{settings.SUPABASE_URL}/rest/v1/nt_system_config",
                headers={
                    "apikey": settings.SUPABASE_KEY,
                    "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                },
                params={"key": f"eq.{key}"},
            )
            return resp.status_code in (200, 204)
    except Exception as exc:
        logger.debug("system_config: delete(%s) failed: %s", key, exc)
        return False
