-- 184_dashboard_cache_renko_live_grid.sql  (2026-09-28)
--
-- The renkoBricks slice of refresh_talaria_dashboard_cache() took the top-8
-- bricks per symbol by (session_date DESC, brick_index DESC) with NO
-- brick_size filter. Stale grids / orphan high brick_index rows therefore
-- won the window for almost every symbol (confirmed live: 12/14 symbols
-- had 0/8 rows on the sweep's live brick_size; only EURUSD was 8/8).
--
-- Downstream RenkoSection filters to the live grid and falls back to
-- generateBricks when it finds none — so most symbols rendered mock bricks
-- near entry, and the market-proxy line only appeared on EURUSD.
--
-- Fix: lateral-join each symbol's latest sweep brick_size and keep only
-- bricks in that grid (same 0.1% relative band as renko_retention /
-- log_renko_bricks orphan purge). Symbols with no live-grid rows get []
-- and fall back to generateBricks on the entry plane (correct plane).

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
    -- Live brick_size grid only (migration 184). See header.
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

COMMENT ON FUNCTION public.refresh_talaria_dashboard_cache() IS
  'Dashboard JSONB snapshot (talaria_dashboard_cache id=1). '
  'renkoBricks is limited to each symbol''s live brick_size grid (184). '
  'Advisory-lock guarded; refreshed by pg_cron job 924.';

-- POST-RUN VERIFICATION:
--   SELECT public.refresh_talaria_dashboard_cache();
--   SELECT r->>'symbol' AS symbol, r->>'brick_size' AS brick_size
--   FROM talaria_dashboard_cache,
--        jsonb_array_elements(payload->'renkoBricks') r
--   ORDER BY 1;
--   -- each symbol's brick_size must match v_talaria_latest_signals.brick_size
