-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 108 (REVISED): nt_renko_bricks — RLS + grants only
--
-- NOTE: nt_renko_bricks was ALREADY created (user DDL) with schema:
--   brick_id serial PK, symbol, direction (up|down check), brick_size,
--   open_price, close_price, high, low, label, brick_index, ts timestamptz,
--   session_date date, UNIQUE (symbol, brick_index, session_date)
--   + idx_nt_bricks_symbol (symbol, ts desc) + partial idx_nt_bricks_label
--
-- So this migration does NOT create the table (would 42710/42703 against the
-- existing shape). It only: (1) grants the anon SELECT (public validation
-- view — migration 107 pattern), (2) enables RLS + read-only anon policy.
-- Idempotent: DROP POLICY IF EXISTS before CREATE POLICY.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_renko_bricks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role full access" ON nt_renko_bricks;
CREATE POLICY "Service role full access" ON nt_renko_bricks
  FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "anon read nt_renko_bricks" ON nt_renko_bricks;
CREATE POLICY "anon read nt_renko_bricks" ON nt_renko_bricks
  FOR SELECT USING (true);

GRANT SELECT ON nt_renko_bricks TO anon;

-- ═══════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   SELECT tablename, policyname, cmd FROM pg_policies
--     WHERE tablename = 'nt_renko_bricks' ORDER BY cmd;
--   Expect 2 rows: "Service role full access" (ALL) + "anon read" (SELECT).
-- ═══════════════════════════════════════════════════════════════════════════
