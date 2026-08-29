-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 129: v_entry_drift — admin-viewable post-entry drift
--
-- nt_entry_drift (migration 121, Phase 2 of the calibration reframe) is
-- the one Phase-2 diagnostic that had no admin-facing view once
-- v_bracket_performance / v_macro_filter_summary were added (migration
-- 127) — this closes that gap, same exact pattern: anon read policy +
-- GRANT on the base table (prerequisite for the view, per migration
-- 107/127), then the view itself, then GRANT SELECT ON the view TO anon.
--
-- nt_entry_drift is already a clean one-row-per-symbol shape — no join
-- or pivot needed, so the view is a plain passthrough (kept as a VIEW
-- rather than granting the base table directly to anon-facing consumers
-- so every admin-surfaced object follows the same "view, not raw table"
-- convention, and so a future column change to nt_entry_drift can be
-- reshaped here without touching the base table's own contract).
-- ═══════════════════════════════════════════════════════════════════════════════

DROP POLICY IF EXISTS "anon read nt_entry_drift" ON nt_entry_drift;
CREATE POLICY "anon read nt_entry_drift" ON nt_entry_drift
  FOR SELECT TO anon USING (true);

GRANT SELECT ON nt_entry_drift TO anon;

CREATE OR REPLACE VIEW v_entry_drift AS
SELECT
  symbol,
  n,
  avg_drift_1h_pct,
  stdev_drift_1h_pct,
  avg_drift_4h_pct,
  stdev_drift_4h_pct,
  avg_drift_24h_pct,
  stdev_drift_24h_pct,
  computed_at
FROM nt_entry_drift
ORDER BY symbol;

COMMENT ON VIEW v_entry_drift IS
  'Admin-viewable post-entry price drift per symbol at 1h/4h/24h horizons (mean + stdev), across all resolved nt_signal_sim rows (any outcome). Passthrough of nt_entry_drift (migration 121). Read by nobletradingapp/Hermes via anon key.';

GRANT SELECT ON v_entry_drift TO anon;

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   SELECT * FROM v_entry_drift ORDER BY symbol;
--   SELECT policyname FROM pg_policies WHERE tablename = 'nt_entry_drift';
--   -- Expect 2 rows: "Service role full access" FOR ALL + "anon read" FOR SELECT.
-- ═══════════════════════════════════════════════════════════════════════════════
