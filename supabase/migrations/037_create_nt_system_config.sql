-- ═══════════════════════════════════════════════════════════════════════════════
-- 037: Create missing nt_system_config table
--
-- PROBLEM:
--   Migration 023 creates nt_system_config_audit (the immutable audit log)
--   but assumes nt_system_config (the base config table) already exists —
--   its comment says "The nt_system_config table was renamed from
--   system_config but its audit table was never created." However, no
--   migration in the repo actually creates the base table.
--
--   On a fresh DB apply, the table is never created. On the live Studio
--   instance, the table was apparently created manually (out-of-band) but
--   was subsequently dropped, causing a 404 on every
--   GET /rest/v1/nt_system_config call:
--
--     Client error '404 Not Found' for url '
--     https://pcvscowltlrxzgxjurcr.supabase.co/rest/v1/nt_system_config
--     ?select=%2A'
--     {"code":"PGRST205","message":"Could not find the table
--      'public.nt_system_config' in the schema cache"}
--
--   The runtime_config engine (regime_platform/core/runtime_config.py)
--   catches this 404 gracefully and falls back to 146 hardcoded defaults
--   with extended TTL, but the table MUST exist for:
--     • admin config overrides via the /config/reload or /config API
--     • migration 024's INSERT INTO nt_system_config (which has been
--       silently failing on fresh deploys)
--     • runtime_config.set_value() writes (admin UI edits)
--
-- This migration creates the table with the exact column set that
-- runtime_config.py's _row_to_entry() reads and set_value() writes:
--   key, value (JSONB), value_type, category, description, default_value,
--   env_var, min_value, max_value, allowed_values, is_sensitive,
--   requires_restart, updated_at, updated_by
--
-- Migration 024 inserts (key, value, value_type, category, description,
-- default_value) — all nullable except key/value/value_type — so this
-- migration must be idempotent with CREATE TABLE IF NOT EXISTS.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Create the base config table ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_system_config (
  key             TEXT PRIMARY KEY,                                   -- config param key, e.g. "risk.annualise_factor"
  value           JSONB NOT NULL,                                     -- the runtime value (type-coerced at read)
  value_type      TEXT NOT NULL CHECK (value_type IN ('float','int','bool','str','json')),  -- enum: ValueType
  category        TEXT NOT NULL DEFAULT 'general',                    -- logical grouping: risk, ev, renko, l2, etc.
  description     TEXT DEFAULT '',                                    -- human-readable description
  default_value   JSONB,                                              -- original default (for reset)
  env_var         TEXT,                                               -- optional env var override name
  min_value       JSONB,                                              -- type-coerced min bound (for validation)
  max_value       JSONB,                                              -- type-coerced max bound
  allowed_values  JSONB,                                              -- JSON array of allowed enum values
  is_sensitive    BOOLEAN NOT NULL DEFAULT FALSE,                     -- hide from logs / mask in UI
  requires_restart BOOLEAN NOT NULL DEFAULT FALSE,                   -- needs app restart to take effect
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),                -- last write timestamp
  updated_by      TEXT NOT NULL DEFAULT 'system'                      -- who changed it (user sub, "system", "migration")
);

COMMENT ON TABLE nt_system_config IS
  'Runtime configuration parameters (hot-reloadable). Read by regime_platform/core/runtime_config.py. '
  'Migration 024 seeds L2 + EV config keys; migration 023 created the _audit companion table.';

COMMENT ON COLUMN nt_system_config.key IS
  'Dot-path config key, e.g. risk.annualise_factor, l2.enabled, ev.calibration_min_records';
COMMENT ON COLUMN nt_system_config.value IS
  'Config value stored as JSONB for type flexibility (float/int/bool/str/json). Type coercion at read-time via ValueType enum.';
COMMENT ON COLUMN nt_system_config.value_type IS
  'Enum: float, int, bool, str, json. Determines how value is parsed by RuntimeConfig._parse_db_value.';
COMMENT ON COLUMN nt_system_config.category IS
  'Logical grouping for filtering: risk, ev, l2, renko, general, etc.';
COMMENT ON COLUMN nt_system_config.default_value IS
  'Original default from code — enables admin UI "reset to default" action.';

-- ─── 2. Indexes ────────────────────────────────────────────────────────

-- Category filter (admin UI grouping)
CREATE INDEX IF NOT EXISTS idx_nt_system_config_category
  ON nt_system_config(category);

-- Updated-at ordering (admin UI: recently modified)
CREATE INDEX IF NOT EXISTS idx_nt_system_config_updated_at
  ON nt_system_config(updated_at DESC);

-- Env var resolution (lookups by env var name)
CREATE INDEX IF NOT EXISTS idx_nt_system_config_env_var
  ON nt_system_config(env_var)
  WHERE env_var IS NOT NULL;

-- ─── 3. Row Level Security ─────────────────────────────────────────────
-- 
-- Service role: full access (orchestrator writes + reads)
-- Authenticated: read-only (admin dashboard, agent inspection)

ALTER TABLE nt_system_config ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role full access to nt_system_config"
  ON nt_system_config
  FOR ALL
  TO service_role
  USING (true)
  WITH CHECK (true);

CREATE POLICY "Authenticated users can read nt_system_config"
  ON nt_system_config
  FOR SELECT
  TO authenticated
  USING (true);

-- ─── 4. Audit trigger ──────────────────────────────────────────────────
--
-- Mirrors nt_system_config_audit (migration 023): every UPDATE or DELETE
-- on nt_system_config inserts a row into the audit log with old/new values.
-- INSERTs are not audited (the audit row's old_value would be NULL, which
-- the _audit schema already supports for "new key" events).

CREATE OR REPLACE FUNCTION nt_system_config_audit_trigger()
RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    INSERT INTO nt_system_config_audit (key, old_value, new_value, changed_by, reason)
    VALUES (OLD.key, OLD.value, NEW.value, NEW.updated_by, 'update');
    RETURN NEW;
  ELSIF TG_OP = 'DELETE' THEN
    INSERT INTO nt_system_config_audit (key, old_value, new_value, changed_by, reason)
    VALUES (OLD.key, OLD.value, NULL, OLD.updated_by, 'delete');
    RETURN OLD;
  ELSE
    RETURN NULL;
  END IF;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS nt_system_config_audit_on_change ON nt_system_config;
CREATE TRIGGER nt_system_config_audit_on_change
  AFTER UPDATE OR DELETE ON nt_system_config
  FOR EACH ROW EXECUTE FUNCTION nt_system_config_audit_trigger();

-- ─── 5. Verify ─────────────────────────────────────────────────────────
SELECT
  table_name,
  column_name,
  data_type,
  is_nullable,
  column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'nt_system_config'
ORDER BY ordinal_position;
