-- ═══════════════════════════════════════════════════════════════
-- 017: F-Lite-1 enforcement in retry-stale-symbols + skip visibility
--
-- BACKGROUND:
--   Migration 016 rewrote sim_retry_stale_symbols() to call the unified
--   `sweep` function with {"mode":"light","symbol":...}. But retry-stale
--   fires UNCONDITIONALLY every 5 min — including during the 6 heavy
--   sweep hours (02,06,10,14,18,22 UTC). At 02:10 UTC on June 17, this
--   caused a collision:
--     - sweep-heavy-SOL cron fired at 02:10:00.116
--     - retry-stale-symbols fired mode=light for SOL at 02:10:00.117
--       (SOL was 69.8 min stale, > 20 min threshold)
--     - Per-symbol lock (SET NX EX) let only one win
--     - Light sweep (TTL 90s) acquired lock first → heavy sweep SKIPPED
--     - Heavy sweep returned 202 {skipped:1} — no DB write, no error
--     - SOL's sim_sweep_results stayed 7.4h stale (Redis stayed fresh
--       from the light sweep, so trading was unaffected)
--
-- FIX A (root cause): Make retry-stale-symbols heavy-hour-aware.
--   During the 6 heavy hours, SKIP light invocations entirely and defer
--   to the heavy sweep cron jobs (which fire at :00/:05/:10/etc). This
--   eliminates the collision at the source.
--
-- FIX C (visibility, SQL side): The skip is now logged via RAISE NOTICE
--   so it appears in pg_cron's return_message. The edge function side
--   (Fix C-TS) adds sim_errors entries for in-function skips.
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
  v_hour_utc   INTEGER;
  v_skipped    INTEGER := 0;
  v_invoked    INTEGER := 0;
BEGIN
  -- Resolve service_role key from vault
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  -- F-Lite-1: during the 6 heavy sweep hours, skip light invocations.
  -- The heavy sweep cron jobs (780-793) fire at :00/:05/:10/:15/:20/:25
  -- of these hours and will refresh the regime. retry-stale defers.
  v_hour_utc := EXTRACT(HOUR FROM now() AT TIME ZONE 'UTC')::INTEGER;

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
    ORDER BY s.symbol
  LOOP
    IF v_hour_utc IN (2, 6, 10, 14, 18, 22) THEN
      -- Heavy hour: defer to the heavy sweep cron jobs.
      -- Don't fire light — it would collide with the heavy sweep for
      -- the same symbol at the same minute.
      RAISE NOTICE '[retry-sweep] %: SKIP — heavy hour % UTC (F-Lite-1), deferring to heavy sweep cron',
        v_symbol, v_hour_utc;
      v_skipped := v_skipped + 1;
      CONTINUE;
    END IF;

    RAISE NOTICE '[retry-sweep] %: invoke mode=light (hour % UTC)',
      v_symbol, v_hour_utc;

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
    v_invoked := v_invoked + 1;

    -- Small delay between retries to respect global concurrency cap (4)
    PERFORM pg_sleep(2);
  END LOOP;

  RAISE NOTICE '[retry-sweep] done: invoked=%, skipped (heavy hour)=%',
    v_invoked, v_skipped;
END;
$$;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY: confirm the new function body is in place
-- (run manually after applying this migration)
-- ═══════════════════════════════════════════════════════════════
-- SELECT prosrc FROM pg_proc WHERE proname = 'sim_retry_stale_symbols';
