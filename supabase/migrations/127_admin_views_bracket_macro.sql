-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 127: admin-viewable bracket-performance + macro-filter summaries
--
-- WHY: nobletradingapp and the Hermes admin plugin both render generic
-- Supabase table/view results — same pattern already used by the Talaria
-- plugin (migration 110): the admin app reads Supabase REST with the
-- PUBLIC anon key, so no new backend/API code is needed, only a
-- read-only view + the anon grants it depends on.
--
-- Prerequisite (mirrors migration 107 exactly): a view runs with its
-- OWNER's privileges, but Supabase's RLS setup still expects the anon
-- role to have its own SELECT policy + GRANT on every base table a view
-- reads — migration 110's views relied on migration 107 having already
-- anon-granted nt_signal_sim/nt_paper_positions/nt_sweep_result. None of
-- the four base tables below have ever been anon-granted (they've all
-- been service-role-only diagnostics until now), so step 1 adds that,
-- identically to migration 107's shape, before the views are created.
--
--   v_bracket_performance  — one row per (symbol, distance_atr_bucket):
--                            nt_bracket_reachability (reachability: hit/
--                            expire rates, hours-to-resolve) LEFT JOINed
--                            with nt_calibration_bias_by_bucket (win_rate,
--                            bias, expectancy_r) on their shared key.
--                            LEFT JOIN (not INNER) so an all-expired
--                            bucket still shows up, with the calibration
--                            columns NULL rather than silently vanishing.
--   v_macro_filter_summary — one row per symbol: nt_macro_regime's
--                            current price-only read, plus
--                            nt_macro_edge_by_symbol's 3 alignment rows
--                            (aligned/opposed/neutral) pivoted into
--                            columns via FILTER, same idiom migration
--                            110's v_talaria_signal_health already uses.
--
-- IDEMPOTENT: DROP POLICY IF EXISTS before CREATE POLICY, CREATE OR
-- REPLACE VIEW, GRANT ... TO anon — all naturally re-runnable.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. anon read access on the 4 base tables (mirrors migration 107) ──────────

DROP POLICY IF EXISTS "anon read nt_bracket_reachability" ON nt_bracket_reachability;
CREATE POLICY "anon read nt_bracket_reachability" ON nt_bracket_reachability
  FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS "anon read nt_calibration_bias_by_bucket" ON nt_calibration_bias_by_bucket;
CREATE POLICY "anon read nt_calibration_bias_by_bucket" ON nt_calibration_bias_by_bucket
  FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS "anon read nt_macro_regime" ON nt_macro_regime;
CREATE POLICY "anon read nt_macro_regime" ON nt_macro_regime
  FOR SELECT TO anon USING (true);

DROP POLICY IF EXISTS "anon read nt_macro_edge_by_symbol" ON nt_macro_edge_by_symbol;
CREATE POLICY "anon read nt_macro_edge_by_symbol" ON nt_macro_edge_by_symbol
  FOR SELECT TO anon USING (true);

GRANT SELECT ON nt_bracket_reachability TO anon;
GRANT SELECT ON nt_calibration_bias_by_bucket TO anon;
GRANT SELECT ON nt_macro_regime TO anon;
GRANT SELECT ON nt_macro_edge_by_symbol TO anon;

-- ─── 2. v_bracket_performance ───────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_bracket_performance AS
SELECT
  r.symbol,
  r.distance_atr_bucket,
  r.n                       AS n_all,        -- reachability population: any outcome (tp/sl/expired)
  r.pct_hit_tp,
  r.pct_hit_sl,
  r.pct_expired,
  r.median_hours_to_resolve,
  r.p75_hours,
  r.p90_hours,
  r.atr_source,
  b.n                       AS n_resolved,   -- calibration-bias population: win/loss only (subset of n_all)
  b.avg_p_win,
  b.win_rate,
  b.bias,
  b.avg_rr_winners,
  b.expectancy_r,
  b.daily_expectancy_r,
  r.computed_at
FROM nt_bracket_reachability r
LEFT JOIN nt_calibration_bias_by_bucket b
  ON b.symbol = r.symbol AND b.distance_atr_bucket = r.distance_atr_bucket
ORDER BY r.symbol, r.distance_atr_bucket;

COMMENT ON VIEW v_bracket_performance IS
  'Admin-viewable bracket-width performance: nt_bracket_reachability (reachability: hit/expire rates, hours-to-resolve, any outcome) LEFT JOINed with nt_calibration_bias_by_bucket (win_rate/bias/expectancy_r, win/loss-resolved only) per (symbol, distance/ATR bucket). n_all vs n_resolved distinguishes the two populations rather than conflating them. Read by nobletradingapp/Hermes via anon key.';

GRANT SELECT ON v_bracket_performance TO anon;

-- ─── 3. v_macro_filter_summary ──────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_macro_filter_summary AS
SELECT
  m.symbol,
  m.macro_score,
  m.macro_score_smoothed,
  m.macro_label,
  m.macro_strength,
  m.computed_at                                                            AS macro_computed_at,
  MAX(e.n) FILTER (WHERE e.alignment = 'aligned')                          AS aligned_n,
  MAX(e.win_rate) FILTER (WHERE e.alignment = 'aligned')                   AS aligned_win_rate,
  MAX(e.avg_rr_winners) FILTER (WHERE e.alignment = 'aligned')             AS aligned_avg_rr_winners,
  MAX(e.expectancy_r) FILTER (WHERE e.alignment = 'aligned')               AS aligned_expectancy_r,
  MAX(e.median_hours_to_resolve) FILTER (WHERE e.alignment = 'aligned')    AS aligned_median_hours,
  MAX(e.n) FILTER (WHERE e.alignment = 'opposed')                          AS opposed_n,
  MAX(e.win_rate) FILTER (WHERE e.alignment = 'opposed')                   AS opposed_win_rate,
  MAX(e.avg_rr_winners) FILTER (WHERE e.alignment = 'opposed')             AS opposed_avg_rr_winners,
  MAX(e.expectancy_r) FILTER (WHERE e.alignment = 'opposed')               AS opposed_expectancy_r,
  MAX(e.median_hours_to_resolve) FILTER (WHERE e.alignment = 'opposed')    AS opposed_median_hours,
  MAX(e.n) FILTER (WHERE e.alignment = 'neutral')                         AS neutral_n,
  MAX(e.win_rate) FILTER (WHERE e.alignment = 'neutral')                  AS neutral_win_rate,
  MAX(e.avg_rr_winners) FILTER (WHERE e.alignment = 'neutral')            AS neutral_avg_rr_winners,
  MAX(e.expectancy_r) FILTER (WHERE e.alignment = 'neutral')              AS neutral_expectancy_r,
  MAX(e.median_hours_to_resolve) FILTER (WHERE e.alignment = 'neutral')   AS neutral_median_hours
FROM nt_macro_regime m
LEFT JOIN nt_macro_edge_by_symbol e ON e.symbol = m.symbol
GROUP BY m.symbol, m.macro_score, m.macro_score_smoothed, m.macro_label, m.macro_strength, m.computed_at
ORDER BY m.symbol;

COMMENT ON VIEW v_macro_filter_summary IS
  'Admin-viewable one-row-per-symbol macro filter summary: nt_macro_regime''s current price-only read (score/label/strength) plus nt_macro_edge_by_symbol''s win/loss+PnL evidence pivoted into aligned_*/opposed_*/neutral_* columns (n, win_rate, avg_rr_winners, expectancy_r, median_hours_to_resolve). Read by nobletradingapp/Hermes via anon key.';

GRANT SELECT ON v_macro_filter_summary TO anon;

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   SELECT * FROM v_bracket_performance ORDER BY symbol, distance_atr_bucket;
--   SELECT * FROM v_macro_filter_summary ORDER BY symbol;
--   SELECT tablename, policyname FROM pg_policies
--     WHERE tablename IN ('nt_bracket_reachability', 'nt_calibration_bias_by_bucket',
--                          'nt_macro_regime', 'nt_macro_edge_by_symbol')
--     ORDER BY tablename, policyname;
--   -- Expect 2 rows per table: "Service role full access" FOR ALL + "anon read" FOR SELECT.
-- ═══════════════════════════════════════════════════════════════════════════════
