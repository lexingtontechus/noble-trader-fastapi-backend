"""
Admin Configuration Router
═══════════════════════════

REST API for runtime configuration management, gated by the single
shared API key (2026-08-25 — no more per-user roles; the key holder has
full access, equivalent to the old "admin" role).

Endpoints:
    GET    /config/                 - List all config entries (grouped by category)
    GET    /config/{category}       - Get config entries for a category
    GET    /config/key/{key}        - Get a single config entry
    PATCH  /config/key/{key}        - Update a config value (admin only)
    POST   /config/key/{key}/reset  - Reset a config to its default (admin only)
    POST   /config/reload           - Force reload from DB (admin only)
    GET    /config/schema           - Full schema with types, ranges, descriptions
    GET    /config/audit            - Config change audit log
    GET    /config/categories       - List all categories
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from regime_platform.auth import require_api_key, DEFAULT_ACTOR
from regime_platform.core.runtime_config import (
    ConfigEntry,
    ConfigError,
    ConfigKeyError,
    ConfigRangeError,
    ConfigTypeError,
    runtime_config,
    _SyncProxy,
)

log = logging.getLogger("regime.config_router")

config_router = APIRouter(prefix="/config", tags=["config"])


# ── Pydantic models ─────────────────────────────────────────────────────────────

from pydantic import BaseModel, Field


class ConfigValueUpdate(BaseModel):
    """Request body for updating a config value."""
    value: Any = Field(..., description="New value for the config key")
    reason: Optional[str] = Field(None, description="Optional reason for the change")


class ConfigEntryResponse(BaseModel):
    """Public response for a config entry (masks sensitive values)."""
    key: str
    value: Any
    value_type: str
    category: str
    description: str
    default_value: Any = None
    env_var: Optional[str] = None
    min_value: Any = None
    max_value: Any = None
    allowed_values: Optional[List[Any]] = None
    is_sensitive: bool = False
    requires_restart: bool = False
    updated_by: str = "system"
    updated_at: float = 0

    class Config:
        from_attributes = True


class ConfigCategoryResponse(BaseModel):
    """Response for a category of config entries."""
    category: str
    entries: Dict[str, ConfigEntryResponse]
    count: int


class ConfigListResponse(BaseModel):
    """Response listing all config entries grouped by category."""
    categories: Dict[str, ConfigCategoryResponse]
    total_entries: int


class ConfigReloadResponse(BaseModel):
    """Response for config reload."""
    entries_loaded: int
    message: str


class ConfigAuditEntry(BaseModel):
    """Single audit log entry."""
    id: int
    key: str
    old_value: Any = None
    new_value: Any = None
    changed_by: str
    changed_at: str
    reason: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _entry_to_response(entry: ConfigEntry, is_admin: bool = False) -> ConfigEntryResponse:
    """Convert a ConfigEntry to a response, masking sensitive values for non-admins."""
    value = entry.value
    if entry.is_sensitive and not is_admin:
        value = "********"

    return ConfigEntryResponse(
        key=entry.key,
        value=value,
        value_type=entry.value_type.value,
        category=entry.category,
        description=entry.description,
        default_value=entry.default_value,
        env_var=entry.env_var,
        min_value=entry.min_value,
        max_value=entry.max_value,
        allowed_values=entry.allowed_values,
        is_sensitive=entry.is_sensitive,
        requires_restart=entry.requires_restart,
        updated_by=entry.updated_by,
        updated_at=entry.updated_at,
    )


# ── Endpoints ───────────────────────────────────────────────────────────────────

@config_router.get("/", response_model=ConfigListResponse)
async def list_config(
    _: None = Depends(require_api_key),
):
    """List all configuration entries grouped by category."""
    is_admin = True  # 2026-08-25: single shared API key, no roles left to distinguish
    try:
        all_entries = await runtime_config.get_all()
    except Exception as exc:
        log.error("Failed to load config list: %s", exc)
        raise HTTPException(status_code=503, detail=f"Config service unavailable: {exc}")

    categories: Dict[str, Dict[str, ConfigEntryResponse]] = {}

    for key, entry in all_entries.items():
        cat = entry.category
        if cat not in categories:
            categories[cat] = {}
        categories[cat][key] = _entry_to_response(entry, is_admin)

    result = {}
    for cat_name, cat_entries in categories.items():
        result[cat_name] = ConfigCategoryResponse(
            category=cat_name,
            entries=cat_entries,
            count=len(cat_entries),
        )

    return ConfigListResponse(
        categories=result,
        total_entries=len(all_entries),
    )


@config_router.get("/categories", response_model=List[str])
async def list_categories(
    _: None = Depends(require_api_key),
):
    """List all configuration categories."""
    try:
        return await runtime_config.get_categories()
    except Exception as exc:
        log.error("Failed to list config categories: %s", exc)
        raise HTTPException(status_code=503, detail=f"Config service unavailable: {exc}")


@config_router.get("/schema", response_model=Dict[str, ConfigEntryResponse])
async def get_schema(
    _: None = Depends(require_api_key),
):
    """Get full schema with types, ranges, descriptions (values always shown for admin reference)."""
    is_admin = True  # 2026-08-25: single shared API key, no roles left to distinguish
    try:
        all_entries = await runtime_config.get_all()
    except Exception as exc:
        log.error("Failed to load config schema: %s", exc)
        raise HTTPException(status_code=503, detail=f"Config service unavailable: {exc}")
    return {
        key: _entry_to_response(entry, is_admin)
        for key, entry in all_entries.items()
    }


@config_router.get("/audit", response_model=List[ConfigAuditEntry])
async def get_audit_log(
    key: Optional[str] = Query(None, description="Filter by config key"),
    limit: int = Query(50, ge=1, le=500, description="Max entries to return"),
    _: None = Depends(require_api_key),
):
    """Get config change audit log."""
    try:
        return await runtime_config.get_audit_log(key=key, limit=limit)
    except Exception as exc:
        log.error("Failed to load audit log: %s", exc)
        raise HTTPException(status_code=503, detail=f"Config service unavailable: {exc}")


@config_router.get("/reload", response_model=ConfigReloadResponse)
async def reload_config(
    _: None = Depends(require_api_key),
):
    """Force reload of all config from DB. Admin only."""
    try:
        count = await runtime_config.reload()
    except Exception as exc:
        log.error("Failed to reload config: %s", exc)
        raise HTTPException(status_code=503, detail=f"Config reload failed: {exc}")
    return ConfigReloadResponse(
        entries_loaded=count,
        message=f"Reloaded {count} config entries from database.",
    )


@config_router.get("/{category}", response_model=ConfigCategoryResponse)
async def get_category(
    category: str,
    _: None = Depends(require_api_key),
):
    """Get all config entries for a specific category."""
    is_admin = True  # 2026-08-25: single shared API key, no roles left to distinguish
    try:
        entries = await runtime_config.get_category(category)
    except Exception as exc:
        log.error("Failed to load config category %s: %s", category, exc)
        raise HTTPException(status_code=503, detail=f"Config service unavailable: {exc}")

    if not entries:
        raise HTTPException(status_code=404, detail=f"Category '{category}' not found.")

    return ConfigCategoryResponse(
        category=category,
        entries={
            key: _entry_to_response(entry, is_admin)
            for key, entry in entries.items()
        },
        count=len(entries),
    )


@config_router.get("/key/{key:path}", response_model=ConfigEntryResponse)
async def get_config_key(
    key: str,
    _: None = Depends(require_api_key),
):
    """Get a single config entry by key.

    Falls back to sync_config defaults if the key isn't in the DB cache,
    so that known keys (like 'quality_floor') always resolve even when
    Supabase is unreachable.
    """
    is_admin = True  # 2026-08-25: single shared API key, no roles left to distinguish
    try:
        all_entries = await runtime_config.get_all()
    except Exception as exc:
        log.error("Failed to load config for key %s: %s", key, exc)
        raise HTTPException(status_code=503, detail=f"Config service unavailable: {exc}")

    if key in all_entries:
        return _entry_to_response(all_entries[key], is_admin)

    # Fallback: check sync_config defaults (covers keys not yet seeded in DB)
    from regime_platform.core.runtime_config import sync_config as _sync

    # Key alias resolution: bare keys like "quality_floor" may exist as
    # "sizing.quality_floor" or "renko.quality_floor" in defaults.  Try
    # common category prefixes so MCP tools don't need the exact dotted key.
    candidate_keys = [key]
    if "." not in key:
        for prefix in ("sizing", "renko", "risk", "exec", "stream", "alpaca", "regime"):
            candidate_keys.append(f"{prefix}.{key}")

    for candidate in candidate_keys:
        if candidate in _SyncProxy._defaults:
            from regime_platform.core.runtime_config import ConfigEntry as _CE, ValueType as _VT
            default_val = _SyncProxy._defaults[candidate]
            value_type = _VT.FLOAT if isinstance(default_val, float) else (_VT.INT if isinstance(default_val, int) else (_VT.BOOL if isinstance(default_val, bool) else _VT.STR))
            category = candidate.split(".")[0] if "." in candidate else "general"
            entry = _CE(
                key=candidate,
                value=default_val,
                value_type=value_type,
                category=category,
                description=f"Default for {candidate} (resolved from alias '{key}')",
                default_value=default_val,
            )
            return _entry_to_response(entry, is_admin)

    raise HTTPException(status_code=404, detail=f"Config key '{key}' not found (also tried prefixed aliases).")


@config_router.patch("/key/{key:path}", response_model=ConfigEntryResponse)
async def update_config_key(
    key: str,
    body: ConfigValueUpdate,
    _: None = Depends(require_api_key),
):
    """
    Update a config value. Admin only.

    The value is validated against the schema (type, min/max range, allowed values).
    The change is persisted to DB and written to the audit log.
    """
    try:
        entry = await runtime_config.set_value(
            key=key,
            value=body.value,
            changed_by=DEFAULT_ACTOR,
            reason=body.reason,
        )
        return _entry_to_response(entry, is_admin=True)
    except ConfigKeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConfigTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ConfigRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@config_router.post("/key/{key:path}/reset", response_model=ConfigEntryResponse)
async def reset_config_key(
    key: str,
    _: None = Depends(require_api_key),
):
    """Reset a config value to its hardcoded default. Admin only."""
    try:
        entry = await runtime_config.reset_value(key=key, changed_by=DEFAULT_ACTOR)
        return _entry_to_response(entry, is_admin=True)
    except ConfigKeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
