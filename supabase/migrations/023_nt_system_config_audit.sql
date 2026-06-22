-- ═══════════════════════════════════════════════════════════════
-- 023: Create nt_system_config_audit table
--
-- The nt_system_config table was renamed from system_config but
-- its audit table was never created. This migration adds it.
--
-- The audit log is append-only (no UPDATE, no DELETE via RLS)
-- and records every config value change with before/after snapshots.
-- ═══════════════════════════════════════════════════════════════

-- ─── nt_system_config_audit table ────────────────────────────

CREATE TABLE IF NOT EXISTS nt_system_config_audit (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key         TEXT NOT NULL,
  old_value   JSONB,
  new_value   JSONB,
  changed_by  TEXT NOT NULL,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason      TEXT
);

COMMENT ON TABLE  nt_system_config_audit IS 'Immutable audit log for nt_system_config changes';
COMMENT ON COLUMN nt_system_config_audit.id IS 'Auto-incrementing unique identifier';
COMMENT ON COLUMN nt_system_config_audit.key IS 'Config key that was changed';
COMMENT ON COLUMN nt_system_config_audit.old_value IS 'Previous value (NULL for new keys)';
COMMENT ON COLUMN nt_system_config_audit.new_value IS 'New value after change';
COMMENT ON COLUMN nt_system_config_audit.changed_by IS 'Clerk user sub or "system"';
COMMENT ON COLUMN nt_system_config_audit.changed_at IS 'When the change occurred (UTC)';
COMMENT ON COLUMN nt_system_config_audit.reason IS 'Optional human-readable reason for the change';

-- ─── Indexes ─────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_nt_system_config_audit_key
  ON nt_system_config_audit (key);

CREATE INDEX IF NOT EXISTS idx_nt_system_config_audit_time
  ON nt_system_config_audit (changed_at DESC);

-- ─── RLS ─────────────────────────────────────────────────────

ALTER TABLE nt_system_config_audit ENABLE ROW LEVEL SECURITY;

-- Authenticated users can READ the audit log (read-only)
CREATE POLICY "Authenticated users can read config audit"
  ON nt_system_config_audit
  FOR SELECT
  TO authenticated
  USING (true);

-- Service role has full access (INSERT from backend, SELECT for admins)
CREATE POLICY "Service role full access to config audit"
  ON nt_system_config_audit
  TO service_role
  USING (true)
  WITH CHECK (true);
