-- ═══════════════════════════════════════════════════════════════════════════
-- 112: anon READ-ONLY hardening — Talaria-facing tables
--
-- Symptom (VERIFIED LIVE 2026-08-09): the Talaria/admin plugins use the
-- PUBLIC anon key (sb_publishable_…). Live probes showed anon could
-- PATCH and DELETE on nt_symbol / nt_sweep_result / nt_renko_bricks
-- (HTTP 204 + real mutation): an AAPL nt_symbol row was deleted, AAPL
-- sweep-result rows were deleted, and AAPL brick directions were flipped.
-- Root cause: these tables did not enforce write-blocking for anon — the
-- SELECT-only policies from migrations 107/108 were evidently never applied
-- to the live project, and Supabase's default privileges grant ALL to anon.
--
-- Fix (both layers — policy AND privilege):
--   1. ENABLE ROW LEVEL SECURITY (idempotent).
--   2. SELECT-only anon policies (no WITH CHECK → writes blocked by policy).
--   3. REVOKE INSERT/UPDATE/DELETE from anon + GRANT SELECT (privilege layer).
--   4. Data repair for probe damage (AAPL brick directions recomputed from
--      open/close prices — deterministic).
--
-- The backend (service_role) is UNAFFECTED — all writes use the service key.
-- The plugins only ever SELECT, so nothing breaks.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. Enable RLS ──────────────────────────────────────────────────────────
ALTER TABLE nt_symbol        ENABLE ROW LEVEL SECURITY;
ALTER TABLE nt_sweep_result  ENABLE ROW LEVEL SECURITY;
ALTER TABLE nt_renko_bricks  ENABLE ROW LEVEL SECURITY;
ALTER TABLE nt_paper_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE nt_signal_sim    ENABLE ROW LEVEL SECURITY;

-- ── 2. SELECT-only anon policies ───────────────────────────────────────────
DROP POLICY IF EXISTS "anon read nt_symbol" ON nt_symbol;
CREATE POLICY "anon read nt_symbol" ON nt_symbol
  FOR SELECT USING (true);

DROP POLICY IF EXISTS "anon read nt_sweep_result" ON nt_sweep_result;
CREATE POLICY "anon read nt_sweep_result" ON nt_sweep_result
  FOR SELECT USING (true);

DROP POLICY IF EXISTS "anon read nt_renko_bricks" ON nt_renko_bricks;
CREATE POLICY "anon read nt_renko_bricks" ON nt_renko_bricks
  FOR SELECT USING (true);

DROP POLICY IF EXISTS "anon read nt_paper_positions" ON nt_paper_positions;
CREATE POLICY "anon read nt_paper_positions" ON nt_paper_positions
  FOR SELECT USING (true);

DROP POLICY IF EXISTS "anon read nt_signal_sim" ON nt_signal_sim;
CREATE POLICY "anon read nt_signal_sim" ON nt_signal_sim
  FOR SELECT USING (true);

-- ── 3. Privilege layer: anon = SELECT only ─────────────────────────────────
REVOKE INSERT, UPDATE, DELETE ON nt_symbol FROM anon;
REVOKE INSERT, UPDATE, DELETE ON nt_sweep_result FROM anon;
REVOKE INSERT, UPDATE, DELETE ON nt_renko_bricks FROM anon;
REVOKE INSERT, UPDATE, DELETE ON nt_paper_positions FROM anon;
REVOKE INSERT, UPDATE, DELETE ON nt_signal_sim FROM anon;

GRANT SELECT ON nt_symbol TO anon;
GRANT SELECT ON nt_sweep_result TO anon;
GRANT SELECT ON nt_renko_bricks TO anon;
GRANT SELECT ON nt_paper_positions TO anon;
GRANT SELECT ON nt_signal_sim TO anon;

-- ── 4. Data repair — probe damage (2026-08-09) ─────────────────────────────
-- Anon PATCH flipped AAPL brick directions to 'up'. Recompute deterministically
-- from prices (close < open ⇒ down, close > open ⇒ up).
UPDATE nt_renko_bricks
SET direction = CASE
    WHEN close_price < open_price THEN 'down'
    WHEN close_price > open_price THEN 'up'
    ELSE direction
END
WHERE symbol = 'AAPL'
  AND direction <> CASE
      WHEN close_price < open_price THEN 'down'
      WHEN close_price > open_price THEN 'up'
      ELSE direction
  END;

-- ── Verify (run with the PUBLIC anon key) ──────────────────────────────────
--   SELECT on any of the 5 tables            → HTTP 200
--   INSERT / PATCH / DELETE on any           → HTTP 401/403 (privilege revoked)
--   backend sweeps continue (service role)   → nt_sweep_result rows keep landing
--   AAPL bricks: direction matches close<open ⇒ down / close>open ⇒ up
