-- Migration 193: Remove legacy sim_retry_stale_symbols + drop unused indexes
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
-- ───
-- 1. sim_retry_stale_symbols() is a legacy process — the sweep orchestrator
--    (regime_platform/sweep_orchestrator/__init__.py:16) has built-in
--    stale-symbol tracking. The cron job (jobid 777, */5 * * * *) and the
--    function are redundant. The function alone consumed 39% of total DB
--    time (50.3M ms over 3,155 calls) — not because of slow queries (the
--    symbol lookup is 0.192 ms) but because of 14 × pg_sleep(2) = 28 s
--    of sleep per invocation plus 14 blocking HTTP calls.
--
-- 2. Ten indexes across nt_sweep_result, nt_regime_log, nt_signal_sim, and
--    nt_renko_bricks have idx_scan = 0 — never used since creation. They
--    cost ~16.4 MB of storage and add write amplification on every INSERT.
--
-- 3. refresh_talaria_dashboard_cache() has no statement_timeout — a slow
--    matview refresh or view aggregation can run indefinitely and block
--    the pg_cron job 924 slot.
--
-- SAFE TO APPLY
-- ──────────────
-- - Dropping the cron job: the orchestrator already handles stale symbols.
-- - Dropping the function: no callers remain (the cron job was the only
--   caller; the sweep edge function only references it in log strings).
-- - Dropping indexes: all have idx_scan = 0, so no query plan changes.
-- - Adding statement_timeout: only affects the refresh function.
-- ═══════════════════════════════════════════════════════════════════════════

-- ─── 1. Drop the legacy cron job ────────────────────────────────────────────

SELECT cron.unschedule(777);

-- ─── 2. Drop the legacy function ────────────────────────────────────────────

DROP FUNCTION IF EXISTS public.sim_retry_stale_symbols();

-- ─── 3. Drop unused indexes (idx_scan = 0, verified 2026-10-03) ─────────────

-- nt_sweep_result: TA indicator indexes never used
DROP INDEX IF EXISTS public.idx_nt_sweep_result_symbol_adx;
DROP INDEX IF EXISTS public.idx_nt_sweep_result_symbol_rsi;

-- nt_sweep_result: regime_shift partial index never used
DROP INDEX IF EXISTS public.idx_nt_sweep_result_regime_shift;

-- nt_regime_log: venue + symbol_venue indexes never used
DROP INDEX IF EXISTS public.idx_nt_regime_log_venue;
DROP INDEX IF EXISTS public.idx_nt_regime_log_symbol_venue;

-- nt_regime_log: regime_shift partial index never used
DROP INDEX IF EXISTS public.idx_regime_shift;

-- nt_signal_sim: geometry_risk partial index never used
DROP INDEX IF EXISTS public.idx_signal_sim_geometry_risk;

-- nt_signal_sim: qualified_geometry_shadow partial index never used
DROP INDEX IF EXISTS public.idx_signal_sim_qualified_geometry_shadow;

-- nt_signal_sim: bare ts index never used (idx_signal_sim_symbol_ts covers it)
DROP INDEX IF EXISTS public.idx_signal_sim_ts;

-- nt_renko_bricks: label partial index never used
DROP INDEX IF EXISTS public.idx_nt_bricks_label;

-- nt_symbol: GIN index on plan_ids never used
DROP INDEX IF EXISTS public.idx_nt_symbol_plan_ids_gin;

-- ─── 4. Add statement_timeout to refresh_talaria_dashboard_cache() ──────────

-- Refresh runs every 5 min via pg_cron job 924. A 30 s timeout is generous
-- (typical run is ~11 s) but prevents a runaway matview refresh from
-- blocking the cron slot indefinitely.
CREATE OR REPLACE FUNCTION public.refresh_talaria_dashboard_cache()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public
SET statement_timeout = '30s'
AS $function$
DECLARE
  _payload jsonb;
  _lock_key constant bigint := 8823140019;
BEGIN
  IF NOT pg_try_advisory_xact_lock(_lock_key) THEN
    RAISE NOTICE '[dashboard-cache] refresh already in progress — skipping this tick';
    RETURN;
  END IF;

  BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.v_talaria_latest_signals;
  EXCEPTION WHEN OTHERS THEN
    BEGIN
      REFRESH MATERIALIZED VIEW public.v_talaria_latest_signals;
    EXCEPTION WHEN OTHERS THEN
      NULL;
    END;
  END;

  _payload := jsonb_build_object(
    'sweeps', (
      SELECT coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb)
      FROM public.v_talaria_latest_signals s
    ),
    'signalHealth', (
      SELECT coalesce(jsonb_agg(to_jsonb(h)), '[]'::jsonb)
      FROM public.v_talaria_signal_health h
    ),
    'calibration', (
      SELECT coalesce(jsonb_agg(to_jsonb(c)), '[]'::jsonb)
      FROM public.v_eod_calibration_bias_latest c
    ),
    'calibrationBoard', (
      SELECT coalesce(jsonb_agg(to_jsonb(b)), '[]'::jsonb)
      FROM public.v_calibration_confidence_board b
    ),
    'outcomeBySide', (
      SELECT coalesce(jsonb_agg(to_jsonb(o)), '[]'::jsonb)
      FROM public.v_signal_outcome_by_side o
      WHERE o.qualified
    ),
    'renkoBricks', (
      SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb)
      FROM (
        SELECT rb.*
        FROM public.v_talaria_symbols sym
        CROSS JOIN LATERAL (
          SELECT ls.brick_size AS live_brick_size
          FROM public.v_talaria_latest_signals ls
          WHERE ls.symbol = sym.symbol
          LIMIT 1
        ) sig
        CROSS JOIN LATERAL (
          SELECT *
          FROM public.v_talaria_renko br
          WHERE br.symbol = sym.symbol
            AND sig.live_brick_size > 0
            AND abs(br.brick_size - sig.live_brick_size)
                <= greatest(abs(sig.live_brick_size) * 0.001, 1e-9)
          ORDER BY br.session_date DESC, br.brick_index DESC
          LIMIT 10
        ) rb
      ) r
    ),
    'vsEqual', (
      SELECT coalesce(jsonb_agg(to_jsonb(v)), '[]'::jsonb)
      FROM public.v_paper_vs_optimized_daily v
    ),
    'vsEqualAligned', (
      SELECT coalesce(jsonb_agg(to_jsonb(v)), '[]'::jsonb)
      FROM public.v_paper_vs_equalwt_aligned v
    ),
    'vsEqualSymbol', (
      SELECT coalesce(jsonb_agg(to_jsonb(v)), '[]'::jsonb)
      FROM (
        SELECT DISTINCT ON (symbol) *
        FROM public.v_paper_vs_optimized_symbol
        ORDER BY symbol
      ) v
    ),
    'portfolioStats', (
      SELECT to_jsonb(p)
      FROM public.v_talaria_portfolio_stats p
      LIMIT 1
    ),
    'symbols', (
      SELECT coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb)
      FROM public.v_talaria_symbols s
      WHERE s.active
    ),
    'paperPositions', (
      SELECT coalesce(jsonb_agg(to_jsonb(p)), '[]'::jsonb)
      FROM (
        SELECT * FROM public.v_paper_positions
        ORDER BY (status = 'open') DESC, open_ts DESC
        LIMIT 80
      ) p
    ),
    'paperEquity', (
      SELECT coalesce(jsonb_agg(to_jsonb(e)), '[]'::jsonb)
      FROM public.v_paper_equity e
    ),
    'health', (
      SELECT to_jsonb(h)
      FROM public.v_platform_health_summary h
      LIMIT 1
    )
  );

  INSERT INTO public.talaria_dashboard_cache (id, payload, refreshed_at)
  VALUES (1, _payload, now())
  ON CONFLICT (id) DO UPDATE
    SET payload = EXCLUDED.payload,
        refreshed_at = EXCLUDED.refreshed_at;
END;
$function$;

-- ─── 5. Post-run verification ──────────────────────────────────────────────

-- Confirm the cron job is gone:
--   SELECT * FROM cron.job WHERE jobname = 'retry-stale-symbols';  -- 0 rows
--
-- Confirm the function is gone:
--   SELECT * FROM pg_proc WHERE proname = 'sim_retry_stale_symbols';  -- 0 rows
--
-- Confirm the unused indexes are gone:
--   SELECT indexrelname FROM pg_stat_user_indexes
--   WHERE indexrelname IN (
--     'idx_nt_sweep_result_symbol_adx', 'idx_nt_sweep_result_symbol_rsi',
--     'idx_nt_sweep_result_regime_shift', 'idx_nt_regime_log_venue',
--     'idx_nt_regime_log_symbol_venue', 'idx_regime_shift',
--     'idx_signal_sim_geometry_risk', 'idx_signal_sim_qualified_geometry_shadow',
--     'idx_signal_sim_ts', 'idx_nt_bricks_label', 'idx_nt_symbol_plan_ids_gin'
--   );  -- 0 rows
--
-- Confirm the refresh function has a 30 s timeout:
--   SELECT proconfig FROM pg_proc WHERE proname = 'refresh_talaria_dashboard_cache';
--   -- should include {statement_timeout=30s}
