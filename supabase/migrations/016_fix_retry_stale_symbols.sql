-- ═══════════════════════════════════════════════════════════════
-- 016: Fix retry-stale-symbols → call unified `sweep` function
--
-- BACKGROUND:
--   Migration 015 cut over all 24 active cron jobs (780–802) to call
--   the new unified `sweep` edge function with `{"mode":"heavy|light",...}`.
--   However job 777 (retry-stale-symbols, */5 * * * *) invokes the SQL
--   function `sim_retry_stale_symbols()`, which STILL calls the OLD
--   `light-regime` endpoint via `sim_invoke_verified_single('light-regime', ...)`.
--   This is why `light-regime` invocations still appear in logs every 5 min
--   for any symbol whose regime_log is >20 min stale.
--
-- FIX:
--   Rewrite `sim_retry_stale_symbols()` to call `sweep` directly with
--   `{"mode":"light","symbol":"<SYM>"}` body — same auth pattern as
--   the 24 migrated cron jobs (vault_read + set_config + net.http_post).
--   Drop the dependency on `sim_invoke_verified_single` so the old
--   function name `light-regime` is no longer referenced from any
--   active code path.
--
-- SIDE EFFECTS:
--   - The `sim_invoke_verified_single(p_function, p_symbol)` helper is
--     no longer called by any active code path. It is left in place
--     (not dropped) to avoid breaking manual admin calls, but its
--     `p_function` argument is now expected to be `sweep` if used.
--   - The old (fn_name, payload) overload of `sim_invoke_edge_function`
--     is also untouched — it is only referenced by inactive job 91
--     (`sweep-commodities`, active=false). Migrating it is deferred.
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION sim_retry_stale_symbols()
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  v_symbol     TEXT;
  v_url        TEXT := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep';
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
BEGIN
  -- Resolve service_role key from vault (same source as the 24 cron jobs)
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  FOR v_symbol IN
    SELECT s.symbol
    FROM sim_symbols s
    WHERE s.active = true
      AND NOT EXISTS (
        SELECT 1
        FROM sim_regime_log rl
        WHERE rl.symbol = s.symbol
          AND rl.detected_at > now() - interval '20 minutes'
      )
  LOOP
    RAISE NOTICE '[retry-sweep] Re-invoking stale symbol via sweep (mode=light): %', v_symbol;

    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := jsonb_build_object('mode', 'light', 'symbol', v_symbol)
    ) INTO v_request_id;

    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      v_status := NULL;
    END;

    RAISE NOTICE '[retry-sweep] %: status=%', v_symbol, v_status;

    -- Small delay between retries to respect global concurrency cap (4)
    PERFORM pg_sleep(2);
  END LOOP;
END;
$$;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY: confirm no remaining references to 'light-regime' or
-- 'heavy-sweep' string literals inside any active SQL function body
-- (other than the old sim_invoke_edge_function overload which we are
-- intentionally leaving untouched, and which is only referenced by
-- inactive job 91).
-- ═══════════════════════════════════════════════════════════════

-- Sanity query (run manually to verify):
-- SELECT proname, prosrc
-- FROM pg_proc
-- WHERE prosrc LIKE '%light-regime%'
--    OR prosrc LIKE '%heavy-sweep%';
-- Expected after this migration:
--   - sim_invoke_edge_function(fn_name text, payload jsonb) — old overload, OK to leave
--   - (nothing else should match)
