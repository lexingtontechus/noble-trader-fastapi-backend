-- 180_talaria_dashboard_cache_table_and_supporting_index.sql  (2026-09-27)
--
-- Backfills DDL that previously lived ONLY in nobletradingapp's migration
-- set (0021_optimize_talaria_views.sql, 0023_dashboard_bundle_cache.sql) —
-- those files are being removed from nobletradingapp per the 2026-09-27
-- decision to make noble-trader-fastapi-backend the single owner of
-- refresh_talaria_dashboard_cache() / v_talaria_latest_signals / the
-- talaria_dashboard_cache table (two repos independently redeploying the
-- same objects against the same live project was the root cause of the
-- 175/179 fixes silently regressing on the next cross-repo migration run).
--
-- Idempotent — safe to run whether or not the objects already exist live
-- (they do, on the current project; this migration exists so a fresh
-- project build from this repo's migrations alone is complete).

CREATE TABLE IF NOT EXISTS public.talaria_dashboard_cache (
  id           int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  payload      jsonb NOT NULL,
  refreshed_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON public.talaria_dashboard_cache TO service_role;

COMMENT ON TABLE public.talaria_dashboard_cache IS
  'Single-row JSONB snapshot of the dashboard-data fan-out + platform health. '
  'Refreshed by pg_cron job 924 via refresh_talaria_dashboard_cache() (see '
  'migration 175 for the function body, 178 for the cron schedule, 179 for '
  'the v_talaria_latest_signals matview it depends on).';

-- Supporting index for the (symbol, sweep_timestamp DESC) lookup pattern
-- used throughout the dashboard-cache queries and the v_talaria_latest_signals
-- LATERAL rewrite (migration 179).
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_symbol_ts
  ON public.nt_sweep_result (symbol, sweep_timestamp DESC);

-- POST-RUN VERIFICATION:
--   SELECT to_regclass('public.talaria_dashboard_cache');
--   SELECT indexname FROM pg_indexes
--   WHERE tablename = 'nt_sweep_result' AND indexname = 'idx_nt_sweep_result_symbol_ts';
