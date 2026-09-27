-- 175_dashboard_cache_refresh_guard_and_signal_sim_ts_index.sql  (2026-09-27)
--
-- Fixes two live incidents reported by the user ("extensive logging &
-- inefficient queries have caused write errors and IO issues"):
--
-- 1) WRITE ERRORS: cron job 924 (`SELECT public.refresh_talaria_dashboard_cache()`,
--    */2 * * * *) aggregates ~12 views/materialized views (including a
--    `REFRESH MATERIALIZED VIEW CONCURRENTLY`) into one JSONB payload.
--    A single run frequently takes 15-140+ seconds — well past the 120s
--    statement_timeout — and with no overlap guard, a slow run is still
--    executing when the next 2-minute tick fires. Confirmed live:
--    `cron.job_run_details` for jobid 924 shows 187/542 runs (24h) FAILED,
--    almost all `canceling statement due to statement timeout`, with a
--    second run's REFRESH MATERIALIZED VIEW CONCURRENTLY blocking on the
--    first's lock. The resulting backlog of stuck/slow backends also
--    starved pg_cron's limited worker pool, producing cascading
--    "job startup timeout" failures on unrelated jobs (886 health-check,
--    917 helio-subscription-sync, 920 backend health-check) that have
--    nothing to do with the dashboard cache.
--
--    Fix: wrap the refresh in `pg_try_advisory_xact_lock` so an overlapping
--    invocation skips instead of piling up (transaction-scoped lock —
--    released automatically when the SELECT's implicit transaction ends,
--    no unlock bookkeeping needed). Also widen the cron cadence from
--    */2 to */5 minutes — the dashboard does not need sub-2-minute
--    freshness, and this alone cuts overlap probability further.
--
-- 2) IO ISSUES: `nt_signal_sim` (79k live rows, 51MB) had indexes only on
--    (symbol, ts DESC) and several partials — nothing to support the very
--    common `ORDER BY ts ASC` (no symbol filter) pagination pattern used
--    by report/export queries (`WHERE outcome IS NOT NULL ORDER BY ts ASC
--    LIMIT ... OFFSET ...`). Confirmed live via pg_stat_statements: six of
--    these query shapes alone wrote ~27GB combined of temp_blks_written
--    (full-table sorts spilling to disk), out of the instance's 649GB
--    lifetime pg_stat_database.temp_bytes. Fix: add a plain (ts) index so
--    the planner can satisfy the bare ORDER BY ts ASC/DESC without a sort.

-- ── Fix 2: supporting index for unfiltered ts-ordered pagination ──────────
CREATE INDEX IF NOT EXISTS idx_signal_sim_ts
  ON public.nt_signal_sim USING btree (ts);

-- ── Fix 1: advisory-lock guard against overlapping cache refreshes ───────
CREATE OR REPLACE FUNCTION public.refresh_talaria_dashboard_cache()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  _payload jsonb;
  _lock_key constant bigint := 8823140019;  -- arbitrary fixed key for this function
BEGIN
  -- Skip this run entirely if a previous invocation is still executing
  -- (transaction-scoped: releases automatically at COMMIT/ABORT of the
  -- calling statement, no matching unlock call needed).
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
          SELECT *
          FROM public.v_talaria_renko br
          WHERE br.symbol = sym.symbol
          ORDER BY br.session_date DESC, br.brick_index DESC
          LIMIT 8
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
    -- Per-symbol audit. The view groups by (symbol, realized_pnl) and can
    -- emit many rows per symbol — keep one row per symbol for the UI table.
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
    -- Open book first, then a short closed tail for the UI table.
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
$$;

-- ── Widen cadence 2 min -> 5 min to further cut overlap probability ──────
SELECT cron.alter_job(job_id => 924, schedule => '*/5 * * * *');

-- POST-RUN VERIFICATION:
--   -- confirm no stuck/failed runs after the fix:
--   SELECT status, count(*) FROM cron.job_run_details
--   WHERE jobid = 924 AND start_time > now() - interval '30 minutes'
--   GROUP BY status;
--   -- confirm the ts index is used for unfiltered pagination:
--   EXPLAIN SELECT * FROM nt_signal_sim WHERE outcome IS NOT NULL
--   ORDER BY ts ASC LIMIT 1000 OFFSET 20000;
