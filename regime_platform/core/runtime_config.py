"""
Runtime Configuration Engine
═════════════════════════════

Provides hot-reloadable, admin-adjustable configuration backed by Supabase.

Resolution order (highest priority wins):
  1. DB override  (system_config table → in-memory cache with TTL)
  2. Environment variable (e.g. RENKO_BRICK_SIZE)
  3. Hardcoded default (from the module that defines the param)

Usage:
    from regime_platform.core.runtime_config import runtime_config

    brick = runtime_config.get_float("renko.brick_size", default=0.50)
    symbol = runtime_config.get_str("renko.default_symbol", default="SPY")
    enabled = runtime_config.get_bool("renko.trailing_stop", default=True)
    n = runtime_config.get_int("renko.sl_bricks", default=3)
    sizes = runtime_config.get_json("renko.optimize_brick_sizes", default=[0.25, 0.50, 1.00])

Architecture:
    - Singleton RuntimeConfig with asyncio-safe caching
    - 30-second TTL on in-memory cache (configurable via RUNTIME_CONFIG_TTL env)
    - Manual reload via reload() or the /config/reload API endpoint
    - All mutations go through set_value() which writes to DB + audit log
    - Type coercion is strict: wrong-type values raise ConfigTypeError
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import httpx

from .settings import settings

log = logging.getLogger("regime.runtime_config")


# ── Exceptions ──────────────────────────────────────────────────────────────────

class ConfigError(Exception):
    """Base exception for configuration errors."""


class ConfigTypeError(ConfigError):
    """Raised when a config value cannot be coerced to the requested type."""


class ConfigRangeError(ConfigError):
    """Raised when a config value is outside its allowed range."""


class ConfigKeyError(ConfigError):
    """Raised when a config key is not found in the schema."""


# ── Value types ─────────────────────────────────────────────────────────────────

class ValueType(str, Enum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    STR = "str"
    JSON = "json"


# ── Config entry (in-memory representation) ─────────────────────────────────────

@dataclass
class ConfigEntry:
    """Represents one configuration parameter."""
    key: str
    value: Any
    value_type: ValueType
    category: str = "general"
    description: str = ""
    default_value: Any = None
    env_var: Optional[str] = None
    min_value: Any = None
    max_value: Any = None
    allowed_values: Optional[List[Any]] = None
    is_sensitive: bool = False
    requires_restart: bool = False
    updated_by: str = "system"
    updated_at: float = 0.0


# ── Core engine ─────────────────────────────────────────────────────────────────

class RuntimeConfig:
    """
    Hot-reloadable configuration engine backed by Supabase.

    Thread-safe for FastAPI async usage. Each request gets consistent
    values within a cache TTL window.
    """

    DEFAULT_TTL = 30.0  # seconds

    def __init__(self) -> None:
        self._cache: Dict[str, ConfigEntry] = {}
        self._cache_time: float = 0.0
        self._ttl: float = float(os.environ.get("RUNTIME_CONFIG_TTL", self.DEFAULT_TTL))
        self._lock = asyncio.Lock()
        self._schema_loaded = False
        self._supabase_url = settings.SUPABASE_URL
        self._supabase_key = settings.SUPABASE_KEY
        self._client: Optional[httpx.AsyncClient] = None
        self._on_change_callbacks: List[Any] = []  # list of async callables

    # ── Supabase client ─────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    @property
    def _db_available(self) -> bool:
        """True if Supabase URL and key are configured."""
        return bool(self._supabase_url and self._supabase_key)

    # ── DB operations ───────────────────────────────────────────────────────

    async def _fetch_all_from_db(self) -> List[ConfigEntry]:
        """Fetch all config entries from Supabase."""
        if not self._db_available:
            return []

        client = self._get_client()
        try:
            resp = await client.get(
                f"{self._supabase_url}/rest/v1/system_config",
                params={"select": "*"},
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            entries = []
            for row in rows:
                entry = self._row_to_entry(row)
                entries.append(entry)
            return entries
        except Exception as exc:
            log.warning("Failed to fetch config from DB: %s", exc)
            return []

    async def _fetch_category_from_db(self, category: str) -> List[ConfigEntry]:
        """Fetch config entries for a specific category."""
        if not self._db_available:
            return []

        client = self._get_client()
        try:
            resp = await client.get(
                f"{self._supabase_url}/rest/v1/system_config",
                params={"select": "*", "category": f"eq.{category}"},
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            return [self._row_to_entry(r) for r in resp.json()]
        except Exception as exc:
            log.warning("Failed to fetch config category %s from DB: %s", category, exc)
            return []

    async def _upsert_to_db(self, entry: ConfigEntry, changed_by: str, reason: Optional[str] = None) -> bool:
        """Write a config value to DB and log the change."""
        if not self._db_available:
            log.warning("Cannot persist config change: Supabase not configured")
            return False

        client = self._get_client()

        # Get old value for audit
        old_value = None
        if entry.key in self._cache:
            old_value = self._coerce_for_db(self._cache[entry.key].value, entry.value_type)

        new_value = self._coerce_for_db(entry.value, entry.value_type)

        # Upsert the config row
        try:
            row = {
                "key": entry.key,
                "value": new_value,
                "value_type": entry.value_type.value,
                "category": entry.category,
                "description": entry.description,
                "default_value": self._coerce_for_db(entry.default_value, entry.value_type) if entry.default_value is not None else None,
                "env_var": entry.env_var,
                "min_value": self._coerce_for_db(entry.min_value, entry.value_type) if entry.min_value is not None else None,
                "max_value": self._coerce_for_db(entry.max_value, entry.value_type) if entry.max_value is not None else None,
                "allowed_values": json.dumps(entry.allowed_values) if entry.allowed_values else None,
                "is_sensitive": entry.is_sensitive,
                "requires_restart": entry.requires_restart,
                "updated_by": changed_by,
            }
            resp = await client.post(
                f"{self._supabase_url}/rest/v1/system_config",
                json=row,
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates",
                },
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("Failed to upsert config %s: %s", entry.key, exc)
            return False

        # Write audit log
        try:
            audit_row = {
                "key": entry.key,
                "old_value": old_value,
                "new_value": new_value,
                "changed_by": changed_by,
                "reason": reason,
            }
            await client.post(
                f"{self._supabase_url}/rest/v1/system_config_audit",
                json=audit_row,
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                    "Content-Type": "application/json",
                },
            )
        except Exception as exc:
            log.warning("Failed to write config audit for %s: %s", entry.key, exc)

        return True

    # ── Cache management ────────────────────────────────────────────────────

    async def _ensure_cache(self) -> None:
        """Load or refresh the in-memory cache if TTL has expired."""
        now = time.monotonic()
        if self._cache and (now - self._cache_time) < self._ttl:
            return

        async with self._lock:
            # Double-check after acquiring lock
            now = time.monotonic()
            if self._cache and (now - self._cache_time) < self._ttl:
                return

            entries = await self._fetch_all_from_db()
            if entries:
                self._cache = {e.key: e for e in entries}
                self._cache_time = now
                self._schema_loaded = True
                log.debug("Refreshed runtime config cache: %d entries", len(self._cache))
            elif not self._cache:
                # Cold start with no DB data — populate from sync_config defaults
                # so that lookups for known keys don't 404 when Supabase is down.
                self._cache = self._defaults_to_entries()
                self._cache_time = now
                self._schema_loaded = True
                log.info(
                    "Populated config cache from sync defaults (%d keys) — DB unavailable",
                    len(self._cache),
                )

    async def reload(self) -> int:
        """Force a full reload from DB, ignoring TTL. Returns count of entries."""
        async with self._lock:
            self._cache_time = 0  # Force expiry
            entries = await self._fetch_all_from_db()
            if entries:
                self._cache = {e.key: e for e in entries}
                self._cache_time = time.monotonic()
                self._schema_loaded = True
            return len(self._cache)

    # ── Public getters (async) ──────────────────────────────────────────────

    async def get(self, key: str, default: Any = None) -> Any:
        """
        Get a config value with full resolution chain:
          DB override → env var → default
        """
        await self._ensure_cache()

        # 1. DB override
        if key in self._cache:
            entry = self._cache[key]
            return entry.value

        # 2. Environment variable
        entry_if_exists = self._cache.get(key)
        env_var = entry_if_exists.env_var if entry_if_exists else None
        if not env_var:
            # Derive env var name from key: renko.brick_size → RENKO_BRICK_SIZE
            env_var = key.upper().replace(".", "_")
        env_val = os.environ.get(env_var)
        if env_val is not None:
            value_type = entry_if_exists.value_type if entry_if_exists else self._infer_type(default)
            return self._coerce_from_str(env_val, value_type)

        # 3. Default
        return default

    async def get_float(self, key: str, default: float = 0.0) -> float:
        """Get a float config value."""
        val = await self.get(key, default)
        try:
            return float(val)
        except (TypeError, ValueError) as exc:
            raise ConfigTypeError(f"Config {key}={val!r} cannot be converted to float: {exc}") from exc

    async def get_int(self, key: str, default: int = 0) -> int:
        """Get an int config value."""
        val = await self.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError) as exc:
            raise ConfigTypeError(f"Config {key}={val!r} cannot be converted to int: {exc}") from exc

    async def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a bool config value."""
        val = await self.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    async def get_str(self, key: str, default: str = "") -> str:
        """Get a string config value.

        Handles enum defaults gracefully: if the default is an Enum,
        returns its .value (not its repr like 'BrickSizeMode.FIXED').
        """
        val = await self.get(key, default)
        if isinstance(val, str):
            return val
        # Enum instance → use .value (e.g. BrickSizeMode.FIXED → "fixed")
        from enum import Enum
        if isinstance(val, Enum):
            return val.value
        return str(val)

    async def get_json(self, key: str, default: Any = None) -> Any:
        """Get a JSON config value (parsed from string)."""
        val = await self.get(key, default)
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return default
        return val

    # ── Public setters ──────────────────────────────────────────────────────

    async def set_value(
        self,
        key: str,
        value: Any,
        changed_by: str = "api",
        reason: Optional[str] = None,
    ) -> ConfigEntry:
        """
        Set a config value. Validates against schema, writes to DB,
        and updates the in-memory cache immediately.

        Raises ConfigRangeError if value is outside allowed range.
        Raises ConfigKeyError if key is not in the schema.
        """
        await self._ensure_cache()

        if key not in self._cache:
            raise ConfigKeyError(f"Config key '{key}' not found in schema. Add it to the DB first.")

        entry = self._cache[key]

        # Type coercion
        coerced = self._coerce_value(value, entry.value_type)

        # Range validation
        if entry.min_value is not None and entry.value_type in (ValueType.FLOAT, ValueType.INT):
            min_v = float(entry.min_value) if not isinstance(entry.min_value, (int, float)) else entry.min_value
            if float(coerced) < min_v:
                raise ConfigRangeError(
                    f"Config {key}={coerced} is below minimum {entry.min_value}"
                )
        if entry.max_value is not None and entry.value_type in (ValueType.FLOAT, ValueType.INT):
            max_v = float(entry.max_value) if not isinstance(entry.max_value, (int, float)) else entry.max_value
            if float(coerced) > max_v:
                raise ConfigRangeError(
                    f"Config {key}={coerced} is above maximum {entry.max_value}"
                )

        # Enum validation
        if entry.allowed_values is not None and coerced not in entry.allowed_values:
            raise ConfigRangeError(
                f"Config {key}={coerced!r} not in allowed values: {entry.allowed_values}"
            )

        # Update entry
        entry.value = coerced
        entry.updated_by = changed_by
        entry.updated_at = time.time()

        # Persist to DB
        success = await self._upsert_to_db(entry, changed_by, reason)
        if not success:
            log.warning("Config change for %s not persisted to DB (DB unavailable?)", key)

        # Update cache immediately
        self._cache[key] = entry

        log.info(
            "Config %s updated to %s by %s%s",
            key, coerced, changed_by,
            f" (reason: {reason})" if reason else "",
        )

        # Fire on-change callbacks (non-blocking, errors caught)
        for cb in self._on_change_callbacks:
            try:
                await cb(key, entry)
            except Exception as exc:
                log.warning("On-change callback error for %s: %s", key, exc)

        return entry

    async def reset_value(self, key: str, changed_by: str = "api") -> ConfigEntry:
        """Reset a config value to its hardcoded default."""
        await self._ensure_cache()

        if key not in self._cache:
            raise ConfigKeyError(f"Config key '{key}' not found in schema.")

        entry = self._cache[key]
        if entry.default_value is None:
            raise ConfigError(f"Config {key} has no default value defined.")

        return await self.set_value(key, entry.default_value, changed_by, reason="Reset to default")

    # ── Category access ─────────────────────────────────────────────────────

    def register_on_change(self, callback) -> None:
        """
        Register an async callback to be invoked when a config value changes.

        The callback receives (key: str, entry: ConfigEntry).
        Callbacks are fired after the value is persisted and the cache is updated.
        Errors in callbacks are caught and logged — they never block set_value().
        """
        self._on_change_callbacks.append(callback)

    async def get_category(self, category: str) -> Dict[str, ConfigEntry]:
        """Get all config entries for a category."""
        await self._ensure_cache()
        return {k: v for k, v in self._cache.items() if v.category == category}

    async def get_all(self) -> Dict[str, ConfigEntry]:
        """Get all config entries."""
        await self._ensure_cache()
        return dict(self._cache)

    async def get_categories(self) -> List[str]:
        """Get list of all categories."""
        await self._ensure_cache()
        return sorted(set(e.category for e in self._cache.values()))

    # ── Audit trail ─────────────────────────────────────────────────────────

    async def get_audit_log(self, key: Optional[str] = None, limit: int = 50) -> List[dict]:
        """Get config change audit log."""
        if not self._db_available:
            return []

        client = self._get_client()
        params: dict = {"order": "changed_at.desc", "limit": str(limit)}
        if key:
            params["key"] = f"eq.{key}"

        try:
            resp = await client.get(
                f"{self._supabase_url}/rest/v1/system_config_audit",
                params=params,
                headers={
                    "apikey": self._supabase_key,
                    "Authorization": f"Bearer {self._supabase_key}",
                },
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.warning("Failed to fetch audit log: %s", exc)
            return []

    # ── Conversion helpers ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row: dict) -> ConfigEntry:
        """Convert a Supabase row dict to a ConfigEntry."""
        value_type = ValueType(row.get("value_type", "float"))
        raw_value = row.get("value")

        # Parse value based on type
        value = RuntimeConfig._parse_db_value(raw_value, value_type)

        return ConfigEntry(
            key=row["key"],
            value=value,
            value_type=value_type,
            category=row.get("category", "general"),
            description=row.get("description", ""),
            default_value=RuntimeConfig._parse_db_value(row.get("default_value"), value_type) if row.get("default_value") is not None else None,
            env_var=row.get("env_var"),
            min_value=RuntimeConfig._parse_db_value(row.get("min_value"), value_type) if row.get("min_value") is not None else None,
            max_value=RuntimeConfig._parse_db_value(row.get("max_value"), value_type) if row.get("max_value") is not None else None,
            allowed_values=json.loads(row["allowed_values"]) if row.get("allowed_values") else None,
            is_sensitive=row.get("is_sensitive", False),
            requires_restart=row.get("requires_restart", False),
            updated_by=row.get("updated_by", "system"),
            updated_at=row.get("updated_at", 0),
        )

    @staticmethod
    def _parse_db_value(raw: Any, value_type: ValueType) -> Any:
        """Parse a value from DB (JSONB) into the correct Python type."""
        if raw is None:
            return None

        if value_type == ValueType.FLOAT:
            return float(raw)
        elif value_type == ValueType.INT:
            return int(float(raw))  # float() handles "10.0" → 10
        elif value_type == ValueType.BOOL:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.lower() in ("true", "1", "yes")
            return bool(raw)
        elif value_type == ValueType.STR:
            # DB stores strings as JSON strings (with quotes) — strip if needed
            s = str(raw)
            if s.startswith('"') and s.endswith('"'):
                s = s[1:-1]
            return s
        elif value_type == ValueType.JSON:
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            return raw
        return raw

    @staticmethod
    def _coerce_for_db(value: Any, value_type: ValueType) -> Any:
        """Convert a Python value to a DB-friendly format."""
        if value is None:
            return None
        if value_type == ValueType.STR:
            return str(value)  # Store as plain string, not JSON-quoted
        if value_type == ValueType.JSON:
            return json.dumps(value) if not isinstance(value, str) else value
        return value

    @staticmethod
    def _coerce_value(value: Any, value_type: ValueType) -> Any:
        """Coerce a user-provided value to the correct Python type."""
        if value_type == ValueType.FLOAT:
            return float(value)
        elif value_type == ValueType.INT:
            return int(float(value))
        elif value_type == ValueType.BOOL:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        elif value_type == ValueType.STR:
            return str(value)
        elif value_type == ValueType.JSON:
            if isinstance(value, str):
                return json.loads(value)
            return value
        return value

    @staticmethod
    def _coerce_from_str(value: str, value_type: ValueType) -> Any:
        """Coerce a string env var value to the correct Python type."""
        if value_type == ValueType.FLOAT:
            return float(value)
        elif value_type == ValueType.INT:
            return int(value)
        elif value_type == ValueType.BOOL:
            return value.lower() in ("true", "1", "yes")
        elif value_type == ValueType.STR:
            return value
        elif value_type == ValueType.JSON:
            return json.loads(value)
        return value

    @staticmethod
    def _infer_type(value: Any) -> ValueType:
        """Infer the value type from a default value."""
        if isinstance(value, bool):
            return ValueType.BOOL
        elif isinstance(value, int):
            return ValueType.INT
        elif isinstance(value, float):
            return ValueType.FLOAT
        elif isinstance(value, str):
            return ValueType.STR
        elif isinstance(value, (list, dict)):
            return ValueType.JSON
        return ValueType.STR

    # ── Fallback: build ConfigEntry objects from sync_config defaults ────────

    @staticmethod
    def _defaults_to_entries() -> Dict[str, ConfigEntry]:
        """
        Build ConfigEntry objects from the sync_config._defaults registry.

        Used when the DB is unreachable (cold start + Supabase down).
        This ensures that known keys like 'quality_floor' still resolve
        instead of returning 404.
        """
        from .runtime_config import sync_config  # noqa: F811 — same module
        entries: Dict[str, ConfigEntry] = {}
        for key, default_val in _SyncProxy._defaults.items():
            value_type = RuntimeConfig._infer_type(default_val)
            # Derive category from key prefix (e.g. 'sizing.base_risk' → 'sizing')
            category = key.split(".")[0] if "." in key else "general"
            entries[key] = ConfigEntry(
                key=key,
                value=default_val,
                value_type=value_type,
                category=category,
                description=f"Default value for {key} (loaded from fallback)",
                default_value=default_val,
            )
        return entries

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ── Singleton ───────────────────────────────────────────────────────────────────

runtime_config = RuntimeConfig()


# ── Synchronous convenience (for use in non-async contexts) ─────────────────────

class _SyncProxy:
    """
    Synchronous proxy that returns hardcoded defaults when called
    outside of an async context. Modules should prefer the async
    interface, but this allows gradual migration.

    Usage (inside a dataclass default_factory or similar):
        from regime_platform.core.runtime_config import sync_config

        brick_size = sync_config.get_float("renko.brick_size", 0.50)
    """

    # Hardcoded defaults registry — populated by register_defaults()
    _defaults: Dict[str, Any] = {}

    @classmethod
    def register_defaults(cls, defaults: Dict[str, Any]) -> None:
        """Register default values for sync access."""
        cls._defaults.update(defaults)

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """Get a config value synchronously (env var → default only, no DB)."""
        # 1. Check env var
        env_var = key.upper().replace(".", "_")
        env_val = os.environ.get(env_var)
        if env_val is not None:
            return env_val
        # 2. Hardcoded default
        return cls._defaults.get(key, default)

    @classmethod
    def get_float(cls, key: str, default: float = 0.0) -> float:
        val = cls.get(key, default)
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @classmethod
    def get_int(cls, key: str, default: int = 0) -> int:
        val = cls.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    @classmethod
    def get_bool(cls, key: str, default: bool = False) -> bool:
        val = cls.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return default

    @classmethod
    def get_str(cls, key: str, default: str = "") -> str:
        return str(cls.get(key, default))

    @classmethod
    def get_json(cls, key: str, default: Any = None) -> Any:
        val = cls.get(key, default)
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return default
        return val


sync_config = _SyncProxy


# ── Register all module defaults for sync fallback ─────────────────────────────

sync_config.register_defaults({
    # Renko Brick Engine
    "renko.brick_size": 0.50,
    "renko.brick_size_mode": "fixed",
    "renko.atr_period": 14,
    "renko.atr_multiplier": 1.0,
    "renko.reversal_bricks": 2,
    # Renko Swing
    "renko.swing_lookback": 3,
    "renko.min_swing_distance": 2,
    # Renko Pattern
    "renko.bull_trigger_n": 3,
    "renko.bear_trigger_n": 3,
    "renko.double_top_bricks": 6,
    "renko.consolidation_max_mix": 0.4,
    # Renko Session
    "renko.session_start": "09:35",
    "renko.session_end": "15:45",
    "renko.skip_lunch": True,
    "renko.lunch_start": "11:30",
    "renko.lunch_end": "13:00",
    "renko.max_trades_per_session": 15,
    "renko.max_daily_loss_bricks": 10.0,
    "renko.max_consecutive_losses": 3,
    "renko.cooldown_seconds": 30.0,
    "renko.regime_gate": True,
    # Renko Risk
    "renko.sl_bricks": 3,
    "renko.tp_bricks": 5,
    "renko.trailing_stop": True,
    "renko.trail_after_bricks": 3,
    "renko.trail_distance_bricks": 2,
    "renko.time_stop_bricks": 10,
    "renko.partial_exit_pct": 0.0,
    "renko.partial_exit_bricks": 3,
    # Renko Sizing
    "renko.kelly_fraction": 0.5,
    "renko.max_position_pct": 0.10,
    "renko.min_position_usd": 50.0,
    "renko.default_win_rate": 0.55,
    # Renko Costs
    "renko.slippage_bps": 2.0,
    "renko.commission_bps": 5.0,
    "renko.spread_bps": 1.0,
    # Renko Execution
    "renko.oco_priority": "sl_first",
    "renko.market_impact_mode": "none",
    "renko.adv_shares": 10_000_000,
    "renko.fill_probability_mode": "always_fill",
    "renko.borrow_rate_bps": 50.0,
    "renko.margin_rate_bps": 150.0,
    "renko.is_hard_to_borrow": False,
    "renko.dividend_yield_bps": 200.0,
    "renko.initial_capital": 100_000.0,
    "renko.confidence_level": 0.95,
    # Renko Pipeline
    "renko.default_symbol": "SPY",
    "renko.timezone": "America/New_York",
    # Renko Backend
    "renko.snapshot_interval": 100,
    "renko.backtest_chunk_size": 150,
    "renko.loss_alert_bricks": -5,
    "renko.batch_notify_min_ticks": 50,
    # Sizing (Masaniello)
    "sizing.base_risk": 0.005,
    "sizing.min_risk": 0.0025,
    "sizing.max_risk": 0.010,
    "sizing.min_prob": 0.50,
    "sizing.min_rr": 2.50,
    "sizing.max_drawdown": 0.10,
    "sizing.batch_halt_dd": 0.05,
    "sizing.regime_floor": 0.50,
    "sizing.use_kelly_overlay": False,
    "sizing.kelly_fraction": 0.25,
    "sizing.batch_size": 5,
    "sizing.target_wins": 3,
    "sizing.mc_simulations": 1000,
    # Risk
    "risk.annualise_factor": 252,
    "risk.base_risk_limit": 0.02,
    "risk.stop_cvar_multiplier": 2,
    "risk.tp_cvar_multiplier": 3,
    # Risk — Stress tests
    "risk.stress_crash_mean": -0.02,
    "risk.stress_crash_std": 0.04,
    "risk.stress_crash_seed": 42,
    "risk.stress_flash_drop": -0.10,
    "risk.stress_vol_multiplier": 3,
    "risk.stress_vol_spike_bars": 20,
    "risk.stress_rate_shock_bps": 0.002,
    "risk.stress_liquidity_shift": 0.001,
    # Regime
    "regime.vol_window_long": 20,
    "regime.vol_window_short": 5,
    "regime.trend_window_short": 10,
    "regime.trend_window_long": 30,
    "regime.hmm_random_state": 42,
    "regime.hmm_n_iter": 100,
    "regime.stability_lookback": 20,
    # Execution
    "exec.sigma_daily": 0.02,
    "exec.permanent_impact_coeff": 0.1,
    "exec.temporary_impact_coeff": 0.142,
    "exec.default_adv_shares": 10_000_000,
    "exec.default_avg_price": 450.0,
    "exec.fill_time_horizon_hours": 6.5,
    "exec.fill_sensitivity": 5.0,
    "exec.fill_threshold": 1.0,
    "exec.htb_premium_bps": 500.0,
    "exec.trading_days_per_year": 252,
    # Execution — financing
    "exec.borrow_rate_bps": 50.0,
    "exec.margin_rate_bps": 150.0,
    "exec.dividend_yield_bps": 200.0,
    "exec.trading_days_per_quarter": 63,
    # Execution — fill probability
    "exec.default_participation_rate": 0.01,
    "exec.volume_penalty_multiplier": 10.0,
    "exec.max_volume_penalty": 0.3,
    "exec.stop_min_fill_probability": 0.90,
    "exec.stop_adverse_sigma": 0.5,
    "exec.limit_adverse_sigma": 0.2,
    "exec.fill_interp_high": 0.8,
    "exec.fill_interp_mid": 0.5,
    "exec.fill_interp_low": 0.2,
    # Stream
    "stream.min_prices_for_fit": 81,
    "stream.window": 500,
    "stream.kelly_fraction": 0.5,
    "stream.target_vol": 0.15,
    "stream.base_risk_limit": 0.02,
    "stream.refit_every": 50,
    "stream.regime_debounce": 3,
    "stream.subscriber_queue_size": 500,
    # Alpaca — WS core
    "alpaca.ws_max_reconnects": 20,
    "alpaca.ws_base_backoff": 1.0,
    "alpaca.ws_ping_interval": 30,
    "alpaca.ws_ping_timeout": 10,
    "alpaca.snapshot_interval_sec": 5,
    "alpaca.stream_grace_period": 30,
    "alpaca.sse_queue_size": 500,
    # Alpaca — WS timeouts & throttling
    "alpaca.ws_close_timeout": 5,
    "alpaca.ws_backoff_cap": 8,
    "alpaca.supabase_http_timeout": 10.0,
    "alpaca.bootstrap_http_timeout": 10.0,
    "alpaca.bootstrap_throttle_timeout": 10.0,
    "alpaca.position_refresh_http_timeout": 5.0,
    "alpaca.position_refresh_throttle_timeout": 5.0,
    "alpaca.data_no_position_wait_timeout": 5.0,
    "alpaca.data_tick_min_interval": 1.0,
    "alpaca.data_resubscribe_interval": 2.0,
    # Risk — stress test extended
    "risk.stress_min_returns": 20,
    "risk.stress_liquidity_downside_mult": 1.5,
    "risk.stress_liquidity_upside_mult": 0.8,
    # Sizing — quality floor (alias for regime_floor, used by MCP tools)
    "sizing.quality_floor": 0.50,
    "renko.quality_floor": 0.50,
    # Renko — sizing mode (exposed for MCP tool discovery)
    "renko.sizing_mode": "kelly",
})
