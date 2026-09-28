-- 185_renko_live_grid_and_rank_support_indexes.sql  (2026-09-28)
--
-- Efficiency support for migrations 183/184 (verified against live REST timings
-- 2026-09-28: latest_signals ~467ms, per-symbol renko top-8 ~286ms, sim top-50
-- ~410ms via PostgREST — acceptable, but the laterals deserve covering indexes
-- so they stay index-scans as nt_renko_bricks / nt_signal_sim grow).
--
-- 1) nt_renko_bricks live-grid window (184 + dashboard RenkoSection):
--      WHERE symbol = ?
--        AND brick_size ≈ live
--      ORDER BY session_date DESC, brick_index DESC
--      LIMIT 8
--    Existing unique (symbol, brick_index, session_date, brick_size) helps
--    symbol+index but not the session_date DESC, brick_index DESC order
--    under a brick_size band. This composite matches the lateral exactly.
--
-- 2) nt_signal_sim rank stamp (183 lateral):
--      WHERE symbol = ? AND kairos_rank_score IS NOT NULL
--      ORDER BY ts DESC LIMIT 1
--    Partial index keeps the 1.7k stamped rows (of 80k+) out of the scan.

-- ── 1. Renko live-grid window ─────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_nt_renko_bricks_live_grid
  ON public.nt_renko_bricks (symbol, brick_size, session_date DESC, brick_index DESC);

-- ── 2. Rank stamp lookup for v_talaria_latest_signals (183) ──────────────
CREATE INDEX IF NOT EXISTS idx_signal_sim_rank_stamp
  ON public.nt_signal_sim (symbol, ts DESC)
  WHERE kairos_rank_score IS NOT NULL;

COMMENT ON INDEX public.idx_nt_renko_bricks_live_grid IS
  'Supports refresh_talaria_dashboard_cache renkoBricks lateral (migration 184) '
  'and v_talaria_renko live-grid fetch: symbol + brick_size band + newest bricks.';

COMMENT ON INDEX public.idx_signal_sim_rank_stamp IS
  'Supports v_talaria_latest_signals rank lateral (migration 183): latest '
  'kairos_rank_score per symbol without scanning null-stamp rows.';

-- POST-RUN VERIFICATION:
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT * FROM nt_renko_bricks
--   WHERE symbol = 'XAUAUD'
--     AND abs(brick_size - 1.474358) <= 0.001474358
--   ORDER BY session_date DESC, brick_index DESC
--   LIMIT 8;
--   -- expect Index Scan using idx_nt_renko_bricks_live_grid
--
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT kairos_rank_score FROM nt_signal_sim
--   WHERE symbol = 'AAPL' AND kairos_rank_score IS NOT NULL
--   ORDER BY ts DESC LIMIT 1;
--   -- expect Index Scan using idx_signal_sim_rank_stamp
