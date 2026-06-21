-- Migration 010: Verified warmup + batch in single function
-- ==============================================================================
-- PROBLEM: Migration 009's warmup pings are fire-and-forget via net.http_post().
--          If the warmup itself gets 503 BOOT_ERROR, we can't tell — the batch
--          request still hits a cold worker and also gets 503. The 3-minute gap
--          between warmup and batch is also too long — Supabase recycles idle
--          Deno workers within 1-2 minutes, so by the time the batch arrives,
--          the worker from the warmup has already been recycled.
--
-- SOLUTION: Combine warmup + batch into a SINGLE SQL function that:
--   1. Sends warmup ping via net.http_post()
--   2. Uses net.http_collect_response() to WAIT for warmup result (fast, ~2s)
--      - Warmup just boots the worker and returns 200 immediately
--      - If 503, retries the warmup up to 3 times with 10s delay
--   3. Once warmup confirms worker is warm, sends the batch fire-and-forget
--
-- TIMING BUDGET:
--   Best case:  warmup 2s + batch fire 1s = 3s total
--   Retry x1:   2s + 10s + 2s + 1s = 15s
--   Retry x2:   2s + 10s + 2s + 10s + 2s + 1s = 27s
--   Worst case: 3 retries all fail = 27s (still well within pg_cron limits)
--
-- WHY THIS WORKS:
--   - net.http_collect_response() for warmup is FAST (2-5s, not 30-130s)
--   - Warmup returns immediately after boot — no heavy computation
--   - Once warm, the batch request goes to a warm worker → no BOOT_ERROR
--   - pg_cron blocked for max 27s — totally fine

-- ============================================================================
-- STEP 1: Create verified warmup + batch function
-- ============================================================================

CREATE OR REPLACE FUNCTION sim_invoke_verified_batch(
  p_function TEXT,
  p_symbols  TEXT[]
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url        TEXT;
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
  v_body       JSONB;
  v_attempt    INTEGER := 1;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  -- ── PHASE 1: Verified warmup (blocking, fast ~2s per attempt) ──
  WHILE v_attempt <= 3 LOOP
    -- Send warmup ping
    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := '{"warmup": true}'::jsonb
    ) INTO v_request_id;

    -- Wait for warmup response (fast — warmup returns in ~2s)
    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      -- If collect fails (timeout, network error), treat as unknown
      v_status := NULL;
    END;

    -- Warmup succeeded — worker is warm!
    IF v_status = 200 THEN
      RAISE NOTICE '[verified-batch] %: Warmup OK (attempt %), worker is warm', p_function, v_attempt;
      EXIT;  -- Proceed to batch
    END IF;

    -- Warmup got 503 BOOT_ERROR — retry after delay
    IF v_status = 503 AND v_attempt < 3 THEN
      RAISE NOTICE '[verified-batch] %: Warmup 503 BOOT_ERROR (attempt %), retrying in 10s...',
        p_function, v_attempt;
      PERFORM pg_sleep(10);
      v_attempt := v_attempt + 1;
      CONTINUE;
    END IF;

    -- Warmup failed (non-503 error or max retries exhausted)
    -- Still try the batch — maybe the worker pool freed up
    RAISE NOTICE '[verified-batch] %: Warmup failed (status=%, attempts=%), sending batch anyway',
      p_function, v_status, v_attempt;
    EXIT;
  END LOOP;

  -- ── PHASE 2: Send batch request (fire-and-forget) ──
  -- Worker should be warm now, so no BOOT_ERROR expected.
  -- If it still fails, the retry sweep (*/5 min) will catch stale symbols.

  v_body := jsonb_build_object('symbols', (SELECT jsonb_agg(s) FROM unnest(p_symbols) AS s));

  PERFORM net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', v_auth
    ),
    body    := v_body
  );

  RAISE NOTICE '[verified-batch] %: Batch sent for [%]', p_function, array_to_string(p_symbols, ',');
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_verified_batch(TEXT, TEXT[]) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_verified_batch(TEXT, TEXT[]) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_verified_batch(TEXT, TEXT[]) FROM authenticated;

-- ============================================================================
-- STEP 2: Create single-symbol verified invocation (for retry sweep + manual)
-- ============================================================================

CREATE OR REPLACE FUNCTION sim_invoke_verified_single(
  p_function TEXT,
  p_symbol   TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url        TEXT;
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
  v_attempt    INTEGER := 1;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  -- ── PHASE 1: Verified warmup ──
  WHILE v_attempt <= 3 LOOP
    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := '{"warmup": true}'::jsonb
    ) INTO v_request_id;

    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      v_status := NULL;
    END;

    IF v_status = 200 THEN
      RAISE NOTICE '[verified-single] %/%: Warmup OK (attempt %)', p_function, p_symbol, v_attempt;
      EXIT;
    END IF;

    IF v_status = 503 AND v_attempt < 3 THEN
      RAISE NOTICE '[verified-single] %/%: Warmup 503 (attempt %), retrying in 10s...',
        p_function, p_symbol, v_attempt;
      PERFORM pg_sleep(10);
      v_attempt := v_attempt + 1;
      CONTINUE;
    END IF;

    RAISE NOTICE '[verified-single] %/%: Warmup failed (status=%, attempts=%), trying anyway',
      p_function, p_symbol, v_status, v_attempt;
    EXIT;
  END LOOP;

  -- ── PHASE 2: Send single-symbol request (fire-and-forget) ──
  PERFORM net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', v_auth
    ),
    body    := jsonb_build_object('symbol', p_symbol)
  );

  RAISE NOTICE '[verified-single] %/%: Request sent', p_function, p_symbol;
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_verified_single(TEXT, TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_verified_single(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_verified_single(TEXT, TEXT) FROM authenticated;

-- ============================================================================
-- STEP 3: Update retry sweep to use verified invocation
-- ============================================================================

CREATE OR REPLACE FUNCTION sim_retry_stale_symbols()
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_symbol TEXT;
BEGIN
  FOR v_symbol IN
    SELECT s.symbol FROM sim_symbols s
    WHERE s.active = true
    AND NOT EXISTS (
      SELECT 1 FROM sim_regime_log rl
      WHERE rl.symbol = s.symbol
      AND rl.detected_at > now() - interval '20 minutes'
    )
  LOOP
    RAISE NOTICE '[retry-sweep] Re-invoking stale symbol: %', v_symbol;
    PERFORM sim_invoke_verified_single('light-regime', v_symbol);
    -- Small delay between retries
    PERFORM pg_sleep(2);
  END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM public;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM anon;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM authenticated;

-- ============================================================================
-- STEP 4: Helper — safe unschedule
-- ============================================================================

CREATE OR REPLACE FUNCTION cron_safe_unschedule(p_job_name TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM cron.unschedule(p_job_name);
  RAISE NOTICE 'Unscheduled %', p_job_name;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'Job % not found (skipping)', p_job_name;
END;
$$;

-- ============================================================================
-- STEP 5: Unscheduling
-- ============================================================================
-- Remove ALL light-regime batch + warmup jobs from migration 009
-- (keep heavy-sweep jobs unchanged)

-- Light-regime batch jobs
SELECT cron_safe_unschedule('light-regime-crypto-1');
SELECT cron_safe_unschedule('light-regime-crypto-2');
SELECT cron_safe_unschedule('light-regime-crypto-3');
SELECT cron_safe_unschedule('light-regime-stocks-1-wk');
SELECT cron_safe_unschedule('light-regime-stocks-2-wk');
SELECT cron_safe_unschedule('light-regime-stocks-3-wk');
SELECT cron_safe_unschedule('light-regime-stocks-1-we');
SELECT cron_safe_unschedule('light-regime-stocks-2-we');
SELECT cron_safe_unschedule('light-regime-stocks-3-we');
SELECT cron_safe_unschedule('light-regime-forex-1-wk');
SELECT cron_safe_unschedule('light-regime-forex-2-wk');
SELECT cron_safe_unschedule('light-regime-forex-3-wk');
SELECT cron_safe_unschedule('light-regime-forex-1-we');
SELECT cron_safe_unschedule('light-regime-forex-2-we');
SELECT cron_safe_unschedule('light-regime-forex-3-we');

-- Warmup jobs (no longer needed — warmup is now inline)
SELECT cron_safe_unschedule('warmup-light-regime-crypto');
SELECT cron_safe_unschedule('warmup-light-regime-stocks-wk');
SELECT cron_safe_unschedule('warmup-light-regime-stocks-we');
SELECT cron_safe_unschedule('warmup-light-regime-forex-wk');
SELECT cron_safe_unschedule('warmup-light-regime-forex-we');

-- Retry sweep (will be rescheduled with updated function)
SELECT cron_safe_unschedule('retry-stale-symbols');

-- ============================================================================
-- STEP 6: Reschedule with verified batch + single-symbol verified retry
-- ============================================================================

-- ─── Crypto (24/7, every 15 min) ─────────────────────────────
SELECT cron.schedule(
  'light-regime-crypto-1',
  '0,15,30,45 * * * *',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['BTC','ETH','SOL']); $$
);
SELECT cron.schedule(
  'light-regime-crypto-2',
  '1,16,31,46 * * * *',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['AVAX','ARB','DOGE']); $$
);
SELECT cron.schedule(
  'light-regime-crypto-3',
  '2,17,32,47 * * * *',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['LINK','XRP','WIF']); $$
);

-- ─── Stocks weekday (every 15 min, Mon-Fri) ──────────────────
SELECT cron.schedule(
  'light-regime-stocks-1-wk',
  '5,20,35,50 * * * 1-5',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['AAPL','AMZN','GOOGL']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-2-wk',
  '6,21,36,51 * * * 1-5',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['META','MSFT','NVDA']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-3-wk',
  '7,22,37,52 * * * 1-5',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['TSLA','SPY','QQQ']); $$
);

-- ─── Stocks weekend (every 30 min, Sat-Sun) ──────────────────
SELECT cron.schedule(
  'light-regime-stocks-1-we',
  '5,35 * * * 0,6',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['AAPL','AMZN','GOOGL']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-2-we',
  '6,36 * * * 0,6',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['META','MSFT','NVDA']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-3-we',
  '7,37 * * * 0,6',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['TSLA','SPY','QQQ']); $$
);

-- ─── Forex/Commodities weekday (every 15 min, Mon-Fri) ───────
SELECT cron.schedule(
  'light-regime-forex-1-wk',
  '10,25,40,55 * * * 1-5',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['EURUSD','GBPUSD','USDJPY']); $$
);
SELECT cron.schedule(
  'light-regime-forex-2-wk',
  '11,26,41,56 * * * 1-5',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['AUDUSD','USDCAD','GOLD']); $$
);
SELECT cron.schedule(
  'light-regime-forex-3-wk',
  '12,27,42,57 * * * 1-5',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['SILVER','OIL','COPPER']); $$
);

-- ─── Forex/Commodities weekend (every 30 min, Sat-Sun) ───────
SELECT cron.schedule(
  'light-regime-forex-1-we',
  '10,40 * * * 0,6',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['EURUSD','GBPUSD','USDJPY']); $$
);
SELECT cron.schedule(
  'light-regime-forex-2-we',
  '11,41 * * * 0,6',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['AUDUSD','USDCAD','GOLD']); $$
);
SELECT cron.schedule(
  'light-regime-forex-3-we',
  '12,42 * * * 0,6',
  $$ SELECT sim_invoke_verified_batch('light-regime', ARRAY['SILVER','OIL','COPPER']); $$
);

-- ─── Retry sweep (every 5 min, uses verified single) ─────────
SELECT cron.schedule(
  'retry-stale-symbols',
  '*/5 * * * *',
  $$ SELECT sim_retry_stale_symbols(); $$
);

-- ============================================================================
-- STEP 7: Update sim_symbols regime_cron (unchanged from 009)
-- ============================================================================

-- Crypto (24/7)
UPDATE sim_symbols SET regime_cron = '0,15,30,45 * * * *'       WHERE symbol IN ('BTC','ETH','SOL');
UPDATE sim_symbols SET regime_cron = '1,16,31,46 * * * *'       WHERE symbol IN ('AVAX','ARB','DOGE');
UPDATE sim_symbols SET regime_cron = '2,17,32,47 * * * *'       WHERE symbol IN ('LINK','XRP','WIF');

-- Stocks (weekday / weekend)
UPDATE sim_symbols SET regime_cron = '5,20,35,50 * * * 1-5 / 5,35 * * * 0,6'   WHERE symbol IN ('AAPL','AMZN','GOOGL');
UPDATE sim_symbols SET regime_cron = '6,21,36,51 * * * 1-5 / 6,36 * * * 0,6'   WHERE symbol IN ('META','MSFT','NVDA');
UPDATE sim_symbols SET regime_cron = '7,22,37,52 * * * 1-5 / 7,37 * * * 0,6'   WHERE symbol IN ('TSLA','SPY','QQQ');

-- Forex/Commodities (weekday / weekend)
UPDATE sim_symbols SET regime_cron = '10,25,40,55 * * * 1-5 / 10,40 * * * 0,6'  WHERE symbol IN ('EURUSD','GBPUSD','USDJPY');
UPDATE sim_symbols SET regime_cron = '11,26,41,56 * * * 1-5 / 11,41 * * * 0,6'  WHERE symbol IN ('AUDUSD','USDCAD','GOLD');
UPDATE sim_symbols SET regime_cron = '12,27,42,57 * * * 1-5 / 12,42 * * * 0,6'  WHERE symbol IN ('SILVER','OIL','COPPER');

-- ============================================================================
-- STEP 8: Cleanup
-- ============================================================================

DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);

-- ============================================================================
-- SUMMARY
-- ============================================================================
-- Migration 009: warmup (fire-and-forget, 3 min gap) → batch (fire-and-forget)
--   Problem: warmup might also get 503 and we can't tell. 3 min gap too long.
--
-- Migration 010: warmup (verified with net.http_collect_response, ~2s)
--              → batch (fire-and-forget, same second)
--   Key change: warmup is VERIFIED before batch is sent.
--   If warmup gets 503, we retry it (up to 3x with 10s delay).
--   Once warm, the batch goes to a warm worker → no BOOT_ERROR.
--
-- New functions:
--   sim_invoke_verified_batch(TEXT, TEXT[]) — verified warmup + batch
--   sim_invoke_verified_single(TEXT, TEXT)  — verified warmup + single symbol
--   sim_retry_stale_symbols()              — uses verified_single
--
-- Old functions (still exist but unscheduled):
--   sim_invoke_edge_function(TEXT, TEXT)           — fire-and-forget (from 009)
--   sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) — fire-and-forget (from 009)
--   sim_invoke_edge_function_warmup(TEXT)          — fire-and-forget (from 009, now unused)
--
-- Job count: 9 batch + 1 retry = 10 (down from 009's 9+5+1=15)
-- Warmup jobs eliminated — warmup is now inline in the batch/retry functions
