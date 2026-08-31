-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 130: composite indexes for v_talaria_latest_signals
--
-- WHY: 2026-08-31 disk-IO investigation (user-reported, via Supabase Edge
-- request stats + query_logs). v_talaria_latest_signals (migration 110) is
-- by far the single most expensive query in the whole project — avg 1.57s
-- per call, 4,912 calls/24h, ~2.1 HOURS of cumulative backend processing
-- time per day from this one view alone (confirmed via query_logs
-- aggregation of response.origin_time). Root cause, confirmed via
-- pg_get_viewdef + pg_indexes:
--
--   SELECT DISTINCT ON (s.symbol) ... FROM nt_sweep_result s
--     LEFT JOIN LATERAL (
--       SELECT ... FROM nt_regime_log rl WHERE rl.symbol = s.symbol
--       ORDER BY rl.detected_at DESC LIMIT 1
--     ) l ON true
--   ORDER BY s.symbol, s.sweep_timestamp DESC
--
-- nt_sweep_result (128,458 rows / 77MB) has separate single-column indexes
-- on `symbol` and `sweep_timestamp DESC` but NO composite
-- (symbol, sweep_timestamp DESC) — so the DISTINCT ON / ORDER BY can't be
-- satisfied by a single index scan and gets more expensive as the table
-- grows (it grows every sweep tick, forever).
--
-- nt_regime_log (208,482 rows / 64MB) has a composite
-- (symbol, detected_at DESC) index, but it's PARTIAL
-- (`WHERE regime_shift = true`) — built for a different query
-- (regime-shift alerts) and useless for this view's unconditional
-- "latest row per symbol" lateral join.
--
-- This migration adds the two missing (unconditional) composite indexes.
-- Nothing about v_talaria_latest_signals' definition changes — this is a
-- pure additive index change, same query, same results, faster plan.
-- CONCURRENTLY avoids locking either table (both receive writes on every
-- sweep tick) — cannot run inside a transaction block, so this migration
-- must be applied statement-by-statement, not as a single wrapped
-- transaction (standard Supabase migration runner already does this for
-- CONCURRENTLY statements).
--
-- Explicitly NOT touched: application code, nt_signal_sim's own elevated
-- seq_scan count (81,487 per pg_stat_user_tables — flagged separately as
-- needing further investigation, not yet root-caused), the new shadow
-- tables added this session (nt_markov_source_shadow/
-- nt_trend_probs_gate_shadow/nt_macro_regime — real but comparatively
-- minor contributors: ~127-160ms average, not the multi-second averages
-- driving this investigation).
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sweep_result_symbol_timestamp
  ON public.nt_sweep_result (symbol, sweep_timestamp DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_regime_log_symbol_detected_at
  ON public.nt_regime_log (symbol, detected_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   EXPLAIN ANALYZE SELECT * FROM v_talaria_latest_signals;
--   -- Expect the plan to show Index Scan / Index Only Scan on the two new
--   -- indexes rather than a Seq Scan or a Sort feeding the DISTINCT ON.
--   SELECT indexname FROM pg_indexes WHERE tablename IN ('nt_sweep_result','nt_regime_log')
--     AND indexname IN ('idx_sweep_result_symbol_timestamp','idx_regime_log_symbol_detected_at');
--   -- Re-run the query_logs aggregation a day later and confirm
--   -- v_talaria_latest_signals' avg response.origin_time has dropped
--   -- sharply from the ~1.57s baseline.
-- ═══════════════════════════════════════════════════════════════════════════════
