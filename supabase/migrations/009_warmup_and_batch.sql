-- Migration 009: Warmup pings + multi-symbol batch invocation + retry sweep
-- ==============================================================================
-- PROBLEM: 27 separate cron jobs each invoke the edge function for a single
--          symbol. When 3+ symbols fire at the same minute, Supabase must
--          cold-start multiple Deno workers simultaneously, leading to
--          503 BOOT_ERROR responses. Migration 008 attempted to fix this
--          with net.http_collect_response() + retry loops, but that approach
--          BLOCKS the pg_cron worker for 30-130s per symbol, causing
--          statement-timeout kills and making the problem worse.
--
-- ARCHITECTURE:
--   1. FIRE-AND-FORGET: Revert sim_invoke_edge_function() to a simple
--      fire-and-forget (no response collection, no retry loop). pg_net
--      sends the HTTP request and returns immediately.
--
--   2. MULTI-SYMBOL BATCHES: Instead of 27 per-symbol cron jobs, use 9
--      batch jobs that each send {"symbols": ["A","B","C"]} to the edge
--      function. One HTTP request per batch = 1 cold start instead of 3.
--
--   3. WARMUP PINGS: Schedule {"warmup": true} requests ~3 minutes before
--      each batch to pre-boot the Deno worker. When the real batch arrives,
--      the worker is already warm — no cold start, no 503.
--
--   4. RETRY SWEEP: A cron job runs every 5 minutes, finds symbols whose
--      sim_regime_log hasn't been updated in 20 min, and re-invokes them
--      individually with a 2-second stagger to avoid another thundering herd.
--
-- RESULT: 27 per-symbol jobs → 9 batch jobs + 5 warmup jobs + 1 retry job = 15
--         total jobs. Cold starts drop from ~27/hour to ~5/hour (warmup pings).

-- ============================================================================
-- STEP 1: Helper — safe unschedule
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
-- STEP 2: Revert sim_invoke_edge_function() to fire-and-forget
-- ============================================================================
-- Migration 008 replaced this with a net.http_collect_response() + retry
-- version that BLOCKS for 30-130s. That causes pg_cron statement timeout
-- kills. Reverting to simple fire-and-forget — net.http_post() returns
-- immediately with a request_id, and we don't wait for the response.
-- Kept for manual invocations and the retry sweep (single-symbol path).

CREATE OR REPLACE FUNCTION sim_invoke_edge_function(
  p_function TEXT,
  p_symbol   TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url  TEXT;
  v_auth TEXT;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  PERFORM net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', v_auth
    ),
    body    := jsonb_build_object('symbol', p_symbol)
  );
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM authenticated;

-- ============================================================================
-- STEP 3: Drop old batch-by-asset-class function (from 005/008)
-- ============================================================================
-- The old sim_invoke_edge_function_batch(TEXT, TEXT) sent {"asset_class": ...}.
-- Replaced by sim_invoke_edge_function_batch_symbols (below) which sends
-- {"symbols": [...]} for explicit 3-symbol batches.

DROP FUNCTION IF EXISTS sim_invoke_edge_function_batch(TEXT, TEXT);

-- ============================================================================
-- STEP 4: Add batch invoker — multi-symbol symbols array
-- ============================================================================
-- Sends {"symbols": ["BTC","ETH","SOL"]} as the body. The edge function
-- processes each symbol sequentially within a single cold start.

CREATE OR REPLACE FUNCTION sim_invoke_edge_function_batch_symbols(
  p_function TEXT,
  p_symbols  TEXT[]
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url  TEXT;
  v_auth TEXT;
  v_body JSONB;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  -- Build symbols array as JSON
  v_body := jsonb_build_object('symbols', (SELECT jsonb_agg(s) FROM unnest(p_symbols) AS s));

  PERFORM net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', v_auth
    ),
    body    := v_body
  );
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) FROM authenticated;

-- ============================================================================
-- STEP 5: Add warmup invoker function
-- ============================================================================
-- Sends {"warmup": true} to the edge function. This triggers the Deno worker
-- to boot without doing any real work (no API calls, no Redis writes).
-- When the real batch request arrives ~3 minutes later, the worker is
-- already warm — no cold start, no 503 BOOT_ERROR.

CREATE OR REPLACE FUNCTION sim_invoke_edge_function_warmup(
  p_function TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url  TEXT;
  v_auth TEXT;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/' || p_function;
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  PERFORM net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', v_auth
    ),
    body    := '{"warmup": true}'::jsonb
  );
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_edge_function_warmup(TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_warmup(TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_warmup(TEXT) FROM authenticated;

-- ============================================================================
-- STEP 6: Add retry sweep function
-- ============================================================================
-- Finds active symbols whose sim_regime_log hasn't been updated in the
-- last 20 minutes and re-invokes them individually via light-regime.
-- Uses pg_sleep(2) between retries to avoid creating another thundering
-- herd of cold starts.

CREATE OR REPLACE FUNCTION sim_retry_stale_symbols()
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  v_url    TEXT;
  v_auth   TEXT;
  v_symbol TEXT;
BEGIN
  v_url  := vault_read('supabase_url') || '/functions/v1/light-regime';
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  -- Find symbols that haven't been updated in the last 20 minutes
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
    PERFORM net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := jsonb_build_object('symbol', v_symbol)
    );
    -- Small delay between retries to avoid another thundering herd
    PERFORM pg_sleep(2);
  END LOOP;
END;
$$;

REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM public;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM anon;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM authenticated;

-- ============================================================================
-- STEP 7: Unschedule ALL existing light-regime cron jobs
-- ============================================================================
-- Remove every per-symbol job from migrations 006 and 007, including the
-- -wk (weekday) and -we (weekend) variants.

-- Crypto (from 006 — no wk/we variants)
SELECT cron_safe_unschedule('light-regime-BTC');
SELECT cron_safe_unschedule('light-regime-ETH');
SELECT cron_safe_unschedule('light-regime-SOL');
SELECT cron_safe_unschedule('light-regime-AVAX');
SELECT cron_safe_unschedule('light-regime-ARB');
SELECT cron_safe_unschedule('light-regime-DOGE');
SELECT cron_safe_unschedule('light-regime-LINK');
SELECT cron_safe_unschedule('light-regime-XRP');
SELECT cron_safe_unschedule('light-regime-WIF');

-- Stocks (from 007 — has -wk and -we variants)
SELECT cron_safe_unschedule('light-regime-AAPL');
SELECT cron_safe_unschedule('light-regime-AAPL-wk');
SELECT cron_safe_unschedule('light-regime-AAPL-we');
SELECT cron_safe_unschedule('light-regime-AMZN');
SELECT cron_safe_unschedule('light-regime-AMZN-wk');
SELECT cron_safe_unschedule('light-regime-AMZN-we');
SELECT cron_safe_unschedule('light-regime-GOOGL');
SELECT cron_safe_unschedule('light-regime-GOOGL-wk');
SELECT cron_safe_unschedule('light-regime-GOOGL-we');
SELECT cron_safe_unschedule('light-regime-META');
SELECT cron_safe_unschedule('light-regime-META-wk');
SELECT cron_safe_unschedule('light-regime-META-we');
SELECT cron_safe_unschedule('light-regime-MSFT');
SELECT cron_safe_unschedule('light-regime-MSFT-wk');
SELECT cron_safe_unschedule('light-regime-MSFT-we');
SELECT cron_safe_unschedule('light-regime-NVDA');
SELECT cron_safe_unschedule('light-regime-NVDA-wk');
SELECT cron_safe_unschedule('light-regime-NVDA-we');
SELECT cron_safe_unschedule('light-regime-TSLA');
SELECT cron_safe_unschedule('light-regime-TSLA-wk');
SELECT cron_safe_unschedule('light-regime-TSLA-we');
SELECT cron_safe_unschedule('light-regime-SPY');
SELECT cron_safe_unschedule('light-regime-SPY-wk');
SELECT cron_safe_unschedule('light-regime-SPY-we');
SELECT cron_safe_unschedule('light-regime-QQQ');
SELECT cron_safe_unschedule('light-regime-QQQ-wk');
SELECT cron_safe_unschedule('light-regime-QQQ-we');

-- Forex/Commodities (from 007 — has -wk and -we variants)
SELECT cron_safe_unschedule('light-regime-EURUSD');
SELECT cron_safe_unschedule('light-regime-EURUSD-wk');
SELECT cron_safe_unschedule('light-regime-EURUSD-we');
SELECT cron_safe_unschedule('light-regime-GBPUSD');
SELECT cron_safe_unschedule('light-regime-GBPUSD-wk');
SELECT cron_safe_unschedule('light-regime-GBPUSD-we');
SELECT cron_safe_unschedule('light-regime-USDJPY');
SELECT cron_safe_unschedule('light-regime-USDJPY-wk');
SELECT cron_safe_unschedule('light-regime-USDJPY-we');
SELECT cron_safe_unschedule('light-regime-AUDUSD');
SELECT cron_safe_unschedule('light-regime-AUDUSD-wk');
SELECT cron_safe_unschedule('light-regime-AUDUSD-we');
SELECT cron_safe_unschedule('light-regime-USDCAD');
SELECT cron_safe_unschedule('light-regime-USDCAD-wk');
SELECT cron_safe_unschedule('light-regime-USDCAD-we');
SELECT cron_safe_unschedule('light-regime-GOLD');
SELECT cron_safe_unschedule('light-regime-GOLD-wk');
SELECT cron_safe_unschedule('light-regime-GOLD-we');
SELECT cron_safe_unschedule('light-regime-SILVER');
SELECT cron_safe_unschedule('light-regime-SILVER-wk');
SELECT cron_safe_unschedule('light-regime-SILVER-we');
SELECT cron_safe_unschedule('light-regime-OIL');
SELECT cron_safe_unschedule('light-regime-OIL-wk');
SELECT cron_safe_unschedule('light-regime-OIL-we');
SELECT cron_safe_unschedule('light-regime-COPPER');
SELECT cron_safe_unschedule('light-regime-COPPER-wk');
SELECT cron_safe_unschedule('light-regime-COPPER-we');

-- Legacy symbols (from earlier migrations — may still exist)
SELECT cron_safe_unschedule('light-regime-DOT');
SELECT cron_safe_unschedule('light-regime-MATIC');
SELECT cron_safe_unschedule('light-regime-ADA');

-- ============================================================================
-- STEP 8: Reschedule with multi-symbol batch cron jobs
-- ============================================================================
-- 9 batch jobs replace 27 per-symbol jobs. Each batch sends 3 symbols as
-- an array in a single HTTP request, so the edge function only cold-starts
-- once per batch instead of 3 separate cold starts.

-- ─── Crypto (24/7, every 15 min) ─────────────────────────────
-- No weekday/weekend split — crypto trades around the clock.
SELECT cron.schedule(
  'light-regime-crypto-1',
  '0,15,30,45 * * * *',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['BTC','ETH','SOL']); $$
);
SELECT cron.schedule(
  'light-regime-crypto-2',
  '1,16,31,46 * * * *',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['AVAX','ARB','DOGE']); $$
);
SELECT cron.schedule(
  'light-regime-crypto-3',
  '2,17,32,47 * * * *',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['LINK','XRP','WIF']); $$
);

-- ─── Stocks weekday (every 15 min, Mon-Fri) ──────────────────
SELECT cron.schedule(
  'light-regime-stocks-1-wk',
  '5,20,35,50 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['AAPL','AMZN','GOOGL']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-2-wk',
  '6,21,36,51 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['META','MSFT','NVDA']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-3-wk',
  '7,22,37,52 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['TSLA','SPY','QQQ']); $$
);

-- ─── Stocks weekend (every 30 min, Sat-Sun) ──────────────────
SELECT cron.schedule(
  'light-regime-stocks-1-we',
  '5,35 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['AAPL','AMZN','GOOGL']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-2-we',
  '6,36 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['META','MSFT','NVDA']); $$
);
SELECT cron.schedule(
  'light-regime-stocks-3-we',
  '7,37 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['TSLA','SPY','QQQ']); $$
);

-- ─── Forex/Commodities weekday (every 15 min, Mon-Fri) ───────
SELECT cron.schedule(
  'light-regime-forex-1-wk',
  '10,25,40,55 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['EURUSD','GBPUSD','USDJPY']); $$
);
SELECT cron.schedule(
  'light-regime-forex-2-wk',
  '11,26,41,56 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['AUDUSD','USDCAD','GOLD']); $$
);
SELECT cron.schedule(
  'light-regime-forex-3-wk',
  '12,27,42,57 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['SILVER','OIL','COPPER']); $$
);

-- ─── Forex/Commodities weekend (every 30 min, Sat-Sun) ───────
SELECT cron.schedule(
  'light-regime-forex-1-we',
  '10,40 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['EURUSD','GBPUSD','USDJPY']); $$
);
SELECT cron.schedule(
  'light-regime-forex-2-we',
  '11,41 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['AUDUSD','USDCAD','GOLD']); $$
);
SELECT cron.schedule(
  'light-regime-forex-3-we',
  '12,42 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_batch_symbols('light-regime', ARRAY['SILVER','OIL','COPPER']); $$
);

-- ============================================================================
-- STEP 9: Add warmup cron jobs
-- ============================================================================
-- Warmup pings fire ~3 minutes before each batch (pg_cron only supports
-- minute granularity, so "30 seconds before" becomes a 3-min lead time).
-- The edge function receives {"warmup": true}, boots the Deno worker,
-- and returns immediately. When the real batch arrives, the worker is warm.

-- ─── Crypto warmup (3 min before crypto batches) ─────────────
SELECT cron.schedule(
  'warmup-light-regime-crypto',
  '57,12,27,42 * * * *',
  $$ SELECT sim_invoke_edge_function_warmup('light-regime'); $$
);

-- ─── Stocks weekday warmup (3 min before stocks-wk batches) ──
SELECT cron.schedule(
  'warmup-light-regime-stocks-wk',
  '2,17,32,47 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_warmup('light-regime'); $$
);

-- ─── Stocks weekend warmup (3 min before stocks-we batches) ──
SELECT cron.schedule(
  'warmup-light-regime-stocks-we',
  '2,32 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_warmup('light-regime'); $$
);

-- ─── Forex/Commodities weekday warmup ────────────────────────
SELECT cron.schedule(
  'warmup-light-regime-forex-wk',
  '7,22,37,52 * * * 1-5',
  $$ SELECT sim_invoke_edge_function_warmup('light-regime'); $$
);

-- ─── Forex/Commodities weekend warmup ────────────────────────
SELECT cron.schedule(
  'warmup-light-regime-forex-we',
  '7,37 * * * 0,6',
  $$ SELECT sim_invoke_edge_function_warmup('light-regime'); $$
);

-- ============================================================================
-- STEP 10: Add retry sweep cron job
-- ============================================================================
-- Runs every 5 minutes. Finds active symbols with no sim_regime_log entry
-- in the last 20 minutes and re-invokes them with a 2-second stagger.

SELECT cron.schedule(
  'retry-stale-symbols',
  '*/5 * * * *',
  $$ SELECT sim_retry_stale_symbols(); $$
);

-- ============================================================================
-- STEP 11: Update sim_symbols regime_cron
-- ============================================================================
-- Reflect the new batch schedules in the regime_cron column for dashboard
-- display and auditing.

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
-- STEP 12: Revoke access from all functions
-- ============================================================================
-- Ensure non-admin roles cannot invoke these functions directly.

REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function(TEXT, TEXT) FROM authenticated;

REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch_symbols(TEXT, TEXT[]) FROM authenticated;

REVOKE ALL ON FUNCTION sim_invoke_edge_function_warmup(TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_warmup(TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_warmup(TEXT) FROM authenticated;

REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM public;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM anon;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM authenticated;

-- ============================================================================
-- STEP 13: Cleanup helper
-- ============================================================================

DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);

-- ============================================================================
-- SUMMARY
-- ============================================================================
-- Before:  27 per-symbol light-regime jobs + 27 heavy-sweep jobs = 54 total
-- After:    9 batch light-regime + 5 warmup + 1 retry + 27 heavy-sweep = 42 total
--
-- New functions:
--   sim_invoke_edge_function(p_function, p_symbol)        — fire-and-forget (reverted from 008)
--   sim_invoke_edge_function_batch_symbols(p_function, p_symbols[]) — multi-symbol batch
--   sim_invoke_edge_function_warmup(p_function)           — pre-boot Deno worker
--   sim_retry_stale_symbols()                             — re-invoke stale symbols
--
-- Heavy-sweep cron jobs are UNCHANGED (already have 5-minute stagger, run
-- infrequently). The single-symbol sim_invoke_edge_function() is kept for
-- manual invocations and the retry sweep.
