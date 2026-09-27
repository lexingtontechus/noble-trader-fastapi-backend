-- 177_drop_duplicate_indexes.sql  (2026-09-27)
--
-- Part of the Supabase CPU/write-amplification harmonization pass
-- (175 = dashboard-cache overlap guard + nt_signal_sim pagination index,
--  176 = nt_renko_bricks no-op-update guard).
--
-- Found via `pg_indexes` inspection: three EXACT duplicate index pairs on
-- the two highest-write-volume tables in the schema. Every INSERT/UPDATE
-- on these tables was maintaining two btrees with the identical definition
-- for zero query benefit — pure write overhead (WAL + index page splits +
-- vacuum work), on tables that already see the heaviest traffic in the
-- system (nt_sweep_result: 177,871 rows / 114MB / ~174K inserts logged in
-- pg_stat_statements; nt_regime_log: 257,477 rows / 88MB / ~178K inserts).
--
--   nt_sweep_result.idx_nt_sweep_result_symbol_ts
--     ≡ nt_sweep_result.idx_sweep_result_symbol_timestamp   (symbol, sweep_timestamp DESC)
--   nt_sweep_result.idx_sweep_timestamp
--     ≡ nt_sweep_result.idx_sweep_ts                         (sweep_timestamp DESC)
--   nt_regime_log.idx_regime_detected
--     ≡ nt_regime_log.idx_regime_ts                          (detected_at DESC)
--
-- Keeps the more descriptively-named index of each pair, drops the other.
-- No query plan changes expected — the surviving index in each pair
-- satisfies every query the dropped one did (identical definition).

-- Plain DROP INDEX (not CONCURRENTLY): Supabase's SQL Management API runs
-- statements inside an implicit transaction, where CONCURRENTLY is
-- rejected. A plain drop takes a brief ACCESS EXCLUSIVE lock but does not
-- scan/rewrite the table, so it completes in milliseconds even on these
-- 100MB+ tables.
DROP INDEX IF EXISTS public.idx_sweep_result_symbol_timestamp;
DROP INDEX IF EXISTS public.idx_sweep_ts;
DROP INDEX IF EXISTS public.idx_regime_ts;

-- POST-RUN VERIFICATION:
--   SELECT indexname FROM pg_indexes
--   WHERE tablename IN ('nt_sweep_result','nt_regime_log') ORDER BY tablename, indexname;
--   -- confirm no plan regression on the hot lookup paths:
--   EXPLAIN SELECT * FROM nt_sweep_result WHERE symbol='XAUUSD' ORDER BY sweep_timestamp DESC LIMIT 20;
--   EXPLAIN SELECT * FROM nt_regime_log ORDER BY detected_at DESC LIMIT 20;
