# Admin Configuration API Reference

**Version:** v7.5.0
**Last Updated:** 2026-06-22
**Router:** `regime_platform/routers/config.py`
**Prefix:** `/config`
**Auth:** All mutations require `require_admin`; reads require `get_authed_user`

---

## Overview

The Configuration API provides runtime management of all system parameters stored in the `nt_system_config` Supabase table. It enables admins to read, update, and reset configuration values without redeploying the backend. All changes are validated against the config schema (type, range, allowed values), persisted to the database, and recorded in an immutable audit trail.

The API is the backend half of the Admin Configuration Panel. The frontend panel calls these endpoints through a BFF proxy that injects Clerk JWTs.

---

## Authentication

| Endpoint Category | Auth Level | Dependency |
|-------------------|-----------|------------|
| Read endpoints (GET) | Any authenticated user | `Depends(get_authed_user)` |
| Write endpoints (PATCH, POST) | Admin only | `Depends(require_admin)` |
| Audit log | Admin only | Manual role check in handler |

Non-admin users can read config entries, but sensitive values are masked as `"********"`. Only admins see real values for keys where `is_sensitive = true`.

---

## Endpoints

### GET /config/

List all configuration entries grouped by category.

**Auth:** Any authenticated user

**Response:** `ConfigListResponse`

```json
{
  "categories": {
    "renko": {
      "category": "renko",
      "entries": {
        "renko.brick_size": {
          "key": "renko.brick_size",
          "value": 0.50,
          "value_type": "float",
          "category": "renko",
          "description": "Fixed dollar brick size",
          "default_value": 0.50,
          "env_var": "RENKO_BRICK_SIZE",
          "min_value": 0.01,
          "max_value": 100.0,
          "allowed_values": null,
          "is_sensitive": false,
          "requires_restart": false,
          "updated_by": "system",
          "updated_at": 1716480000.0
        },
        "...": "..."
      },
      "count": 56
    },
    "execution": { "...": "..." },
    "alpaca": { "...": "..." },
    "risk": { "...": "..." },
    "sizing": { "...": "..." },
    "stream": { "...": "..." },
    "auth": { "...": "..." },
    "regime": { "...": "..." }
  },
  "total_entries": 148
}
```

**Notes:**
- Sensitive values are masked for non-admin users
- Entries are keyed by their full key name (e.g., `"renko.brick_size"`)
- Categories are sorted alphabetically

---

### GET /config/categories

List all configuration category names.

**Auth:** Any authenticated user

**Response:** `List[str]`

```json
["alpaca", "auth", "execution", "regime", "renko", "risk", "sizing", "stream"]
```

---

### GET /config/{category}

Get all config entries for a specific category.

**Auth:** Any authenticated user

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `category` | string | Category name (e.g., "renko", "risk", "execution") |

**Response:** `ConfigCategoryResponse`

```json
{
  "category": "renko",
  "entries": {
    "renko.brick_size": { "...ConfigEntryResponse..." },
    "renko.brick_size_mode": { "...ConfigEntryResponse..." },
    "...": "..."
  },
  "count": 56
}
```

**Errors:**

| Status | Condition |
|--------|-----------|
| 404 | Category not found |

---

### GET /config/key/{key}

Get a single config entry by its full key path.

**Auth:** Any authenticated user

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string (path) | Full config key (e.g., "renko.brick_size") - supports dot notation |

**Response:** `ConfigEntryResponse`

```json
{
  "key": "renko.brick_size",
  "value": 0.50,
  "value_type": "float",
  "category": "renko",
  "description": "Fixed dollar brick size",
  "default_value": 0.50,
  "env_var": "RENKO_BRICK_SIZE",
  "min_value": 0.01,
  "max_value": 100.0,
  "allowed_values": null,
  "is_sensitive": false,
  "requires_restart": false,
  "updated_by": "system",
  "updated_at": 1716480000.0
}
```

**Errors:**

| Status | Condition |
|--------|-----------|
| 404 | Config key not found |

---

### PATCH /config/key/{key}

Update a config value. Admin only.

**Auth:** Admin only (`require_admin`)

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string (path) | Full config key to update |

**Request Body:** `ConfigValueUpdate`

```json
{
  "value": 0.75,
  "reason": "Testing wider bricks in high-vol regime"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `value` | Any | Yes | New value for the config key (will be coerced to the correct type) |
| `reason` | string | No | Optional human-readable reason for the change (recorded in audit log) |

**Validation:**

The backend validates the value against the config schema before persisting:

1. **Type coercion** - The value is coerced to the key's `value_type`:
   - `float`: `float(value)`
   - `int`: `int(float(value))`
   - `bool`: True/true/1/yes -> True, everything else -> False
   - `str`: `str(value)`
   - `json`: `json.loads(value)` if string, else pass-through

2. **Range check** - If `min_value` or `max_value` are defined and the type is `float` or `int`:
   - `value >= min_value`
   - `value <= max_value`

3. **Enum check** - If `allowed_values` is defined:
   - `value in allowed_values`

**Response:** `ConfigEntryResponse` (with updated value)

**Errors:**

| Status | Error Type | Condition |
|--------|-----------|-----------|
| 404 | `ConfigKeyError` | Key not found in schema |
| 422 | `ConfigTypeError` | Value cannot be coerced to the key's type |
| 422 | `ConfigRangeError` | Value outside min/max range or not in allowed_values |
| 500 | `ConfigError` | Unexpected internal error |

**Side Effects:**
- In-memory cache updated immediately
- Row upserted to Supabase `nt_system_config` table
- Audit row inserted to Supabase `nt_system_config_audit` table
- On-change callbacks fired (non-blocking)

---

### POST /config/key/{key}/reset

Reset a config value to its hardcoded default. Admin only.

**Auth:** Admin only (`require_admin`)

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | string (path) | Full config key to reset |

**Response:** `ConfigEntryResponse` (with default value)

**Errors:**

| Status | Error Type | Condition |
|--------|-----------|-----------|
| 404 | `ConfigKeyError` | Key not found in schema |
| 500 | `ConfigError` | Key has no default value defined |

**Side Effects:**
- Same as PATCH - cache updated, DB upserted, audit logged
- Audit `reason` is automatically set to "Reset to default"

---

### POST /config/reload

Force a full reload of all config entries from the database, ignoring the TTL cache. Admin only.

**Auth:** Admin only (`require_admin`)

**Response:** `ConfigReloadResponse`

```json
{
  "entries_loaded": 148,
  "message": "Reloaded 148 config entries from database."
}
```

**When to Use:**
- After directly modifying the Supabase `nt_system_config` table (e.g., via SQL editor)
- When you suspect the in-memory cache is stale
- After deploying a new migration that adds config keys

**Note:** This only reloads the cache in the current backend process. If multiple Render instances are running, each has its own cache and will refresh independently within their TTL windows.

---

### GET /config/schema

Get the full config schema with types, ranges, and descriptions for all entries. Values are always shown (even for non-admins, this endpoint is intended for developer reference).

**Auth:** Any authenticated user (values shown for admin reference)

**Response:** `Dict[str, ConfigEntryResponse]`

```json
{
  "renko.brick_size": {
    "key": "renko.brick_size",
    "value": 0.50,
    "value_type": "float",
    "category": "renko",
    "description": "Fixed dollar brick size",
    "default_value": 0.50,
    "env_var": "RENKO_BRICK_SIZE",
    "min_value": 0.01,
    "max_value": 100.0,
    "allowed_values": null,
    "is_sensitive": false,
    "requires_restart": false,
    "updated_by": "system",
    "updated_at": 1716480000.0
  },
  "...148 entries..."
}
```

---

### GET /config/audit

Get the config change audit log. Admin only.

**Auth:** Admin only (manual role check)

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `key` | string | null | Filter by config key (e.g., "renko.brick_size") |
| `limit` | int | 50 | Maximum entries to return (1–500) |

**Response:** `List[ConfigAuditEntry]`

```json
[
  {
    "id": 42,
    "key": "renko.brick_size",
    "old_value": 0.50,
    "new_value": 0.75,
    "changed_by": "user_2xyz...",
    "changed_at": "2026-05-24T14:32:00Z",
    "reason": "Testing wider bricks in high-vol regime"
  },
  {
    "id": 41,
    "key": "risk.base_risk_limit",
    "old_value": 0.02,
    "new_value": 0.03,
    "changed_by": "user_2xyz...",
    "changed_at": "2026-05-24T14:28:00Z",
    "reason": "Increased for earnings season"
  }
]
```

**Notes:**
- Entries are ordered by `changed_at` descending (most recent first)
- The `changed_by` field contains the Clerk user `sub` (e.g., `user_2xyz...`)
- The audit log is append-only - entries cannot be modified or deleted
- If Supabase is unavailable, an empty list is returned (graceful degradation)

---

## Pydantic Models

### ConfigValueUpdate

```python
class ConfigValueUpdate(BaseModel):
    value: Any = Field(..., description="New value for the config key")
    reason: Optional[str] = Field(None, description="Optional reason for the change")
```

### ConfigEntryResponse

```python
class ConfigEntryResponse(BaseModel):
    key: str
    value: Any
    value_type: str             # "float" | "int" | "bool" | "str" | "json"
    category: str               # "renko" | "risk" | "execution" | etc.
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
```

### ConfigCategoryResponse

```python
class ConfigCategoryResponse(BaseModel):
    category: str
    entries: Dict[str, ConfigEntryResponse]
    count: int
```

### ConfigListResponse

```python
class ConfigListResponse(BaseModel):
    categories: Dict[str, ConfigCategoryResponse]
    total_entries: int
```

### ConfigReloadResponse

```python
class ConfigReloadResponse(BaseModel):
    entries_loaded: int
    message: str
```

### ConfigAuditEntry

```python
class ConfigAuditEntry(BaseModel):
    id: int
    key: str
    old_value: Any = None
    new_value: Any = None
    changed_by: str
    changed_at: str
    reason: Optional[str] = None
```

---

## Error Responses

All errors follow the standard FastAPI HTTPException format:

```json
{
  "detail": "Config key 'renko.nonexistent_key' not found in schema. Add it to the DB first."
}
```

```json
{
  "detail": "Config renko.brick_size=0.001 is below minimum 0.01"
}
```

```json
{
  "detail": "Config renko.brick_size_mode='invalid' not in allowed values: ['fixed', 'atr', 'dynamic']"
}
```

---

## Rate Limiting

The config endpoints are not rate-limited separately - they fall under the default rate limit tier. Since config changes are infrequent admin operations, rate limiting is not a concern in practice.

---

## Database Schema

### system_config Table

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT PRIMARY KEY | Hierarchical key (e.g., "renko.brick_size") |
| `value` | JSONB NOT NULL | Typed value (number, string, bool, array) |
| `value_type` | TEXT NOT NULL DEFAULT 'float' | "float" \| "int" \| "bool" \| "str" \| "json" |
| `category` | TEXT NOT NULL DEFAULT 'general' | Category grouping |
| `description` | TEXT NOT NULL DEFAULT '' | Human-readable explanation |
| `default_value` | JSONB | Hardcoded default for reset |
| `env_var` | TEXT | Corresponding env var name |
| `min_value` | JSONB | Optional minimum for numeric types |
| `max_value` | JSONB | Optional maximum for numeric types |
| `allowed_values` | JSONB | Optional enum list |
| `is_sensitive` | BOOLEAN NOT NULL DEFAULT FALSE | Hide value from non-admins |
| `requires_restart` | BOOLEAN NOT NULL DEFAULT FALSE | Change needs server restart |
| `updated_by` | TEXT NOT NULL DEFAULT 'system' | Who made the change |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | Auto-updated on row change |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | Row creation time |

### system_config_audit Table

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGINT GENERATED ALWAYS AS IDENTITY PK | Auto-incrementing ID |
| `key` | TEXT NOT NULL | Config key that was changed |
| `old_value` | JSONB | Previous value (null for new keys) |
| `new_value` | JSONB | New value |
| `changed_by` | TEXT NOT NULL | Clerk user sub or "system" |
| `changed_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | When the change occurred |
| `reason` | TEXT | Optional change reason |

### Indexes

| Index | Table | Columns | Purpose |
|-------|-------|---------|---------|
| `idx_nt_system_config_category` | system_config | (category) | Fast category lookups |
| `idx_nt_system_config_audit_key` | system_config_audit | (key) | Filter audit by key |
| `idx_nt_system_config_audit_time` | system_config_audit | (changed_at DESC) | Recent changes first |

### RLS Policies

| Policy | Table | Role | Operation |
|--------|-------|------|-----------|
| "Authenticated users can read config" | system_config | authenticated | SELECT |
| "Service role full access to config" | system_config | service_role | ALL |
| "Authenticated users can read config audit" | system_config_audit | authenticated | SELECT |
| "Service role full access to config audit" | system_config_audit | service_role | ALL |

### Triggers

| Trigger | Table | Function | When |
|---------|-------|----------|------|
| `trg_nt_system_config_updated_at` | system_config | `update_nt_system_config_updated_at()` | BEFORE UPDATE - sets `updated_at = now()` |
