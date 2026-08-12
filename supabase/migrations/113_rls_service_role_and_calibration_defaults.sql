-- ═══════════════════════════════════════════════════════════════
-- Migration 113: RLS hardening + calibration-record defaults
-- (2026-08-12 backend deep-audit fixes H5/H7)
--
-- 1. "Service role full access" policies previously had NO `TO` clause,
--    which applies to EVERY role (incl. anon). The anon read-only model
--    built by migrations 107/112 was only saved by the GRANT layer; any
--    future table-level GRANT would open full DML to anon. Recreate the
--    policies with `TO service_role` (service_role bypasses RLS anyway).
-- 2. nt_calibration_records.id TEXT PRIMARY KEY had NO default — every
--    insert path that doesn't synthesize a UUID failed with 23502 (the
--    EOD resolver dead-loop root cause). Add DEFAULT gen_random_uuid().
--    aggression is NOT NULL CHECK — keep NOT NULL but default 'mid'.
--
-- Idempotent: DROP POLICY IF EXISTS before CREATE (CREATE POLICY has no
-- IF NOT EXISTS).
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. RLS: scope "full access" policies to service_role only ────────────

DROP POLICY IF EXISTS "Service role full access" ON nt_venues;
CREATE POLICY "Service role full access" ON nt_venues
  FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access" ON nt_calibration_records;
CREATE POLICY "Service role full access" ON nt_calibration_records
  FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access" ON nt_account_snapshots;
CREATE POLICY "Service role full access" ON nt_account_snapshots
  FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access" ON nt_signal_sim;
CREATE POLICY "Service role full access" ON nt_signal_sim
  FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access" ON nt_eod_actions;
CREATE POLICY "Service role full access" ON nt_eod_actions
  FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access" ON nt_paper_positions;
CREATE POLICY "Service role full access" ON nt_paper_positions
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─── 2. nt_calibration_records defaults (H7) ───────────────────────────────

ALTER TABLE nt_calibration_records
  ALTER COLUMN id SET DEFAULT gen_random_uuid();

ALTER TABLE nt_calibration_records
  ALTER COLUMN aggression SET DEFAULT 'mid';
