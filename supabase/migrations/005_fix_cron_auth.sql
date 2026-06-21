-- Migration 005: Fix pg_cron authentication using Supabase Vault
-- ================================================================
-- PROBLEM: Migration 004 hardcoded <SUPABASE_SERVICE_ROLE> placeholders
--          in every cron job, causing 401 INVALID_JWT_FORMAT errors.
--          Migration 005-GUC tried ALTER DATABASE SET app.* but the
--          SQL editor runner lacks superuser, so that also failed.
--
-- SOLUTION: Store the service role key in Supabase Vault (encrypted at
--           rest), read it at runtime via SECURITY DEFINER wrapper
--           functions.  Secrets never appear in cron job definitions
--           or SQL source files committed to git.
--
-- PREREQUISITES:
--   1. Run this migration first (steps 1-8).
--   2. Then run the ONE manual command in step 3 to insert your
--      service role key into Vault.
--   3. Verify with: SELECT name, decrypted_secret FROM vault.decrypted_secrets
--      WHERE name = 'supabase_service_role';

-- ──────────────────────────────────────────────────────────────
-- Step 1: Enable Vault extension
-- ──────────────────────────────────────────────────────────────
-- Vault uses pgsodium Transparent Column Encryption under the hood.
-- Available on all Supabase plans including free tier.

CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;

-- ──────────────────────────────────────────────────────────────
-- Step 2: Seed the Supabase URL into Vault
-- ──────────────────────────────────────────────────────────────
-- The URL is not secret (it's your project's public API endpoint),
-- but we store it in Vault alongside the key so the invoker
-- functions have a single, consistent way to read both values.

DO $$
BEGIN
  -- vault.create_secret(name, secret, description)
  -- If a secret with this name already exists, this will create a
  -- second entry (same name, different id).  To make the migration
  -- idempotent, we delete any existing entry first.
  DELETE FROM vault.secrets WHERE name = 'supabase_url';
  PERFORM vault.create_secret(
    'supabase_url',
    'https://pcvscowltlrxzgxjurcr.supabase.co',
    'Supabase project URL (not secret, stored for convenience)'
  );
  RAISE NOTICE '✅  Seeded supabase_url into Vault';
END $$;

-- ──────────────────────────────────────────────────────────────
-- Step 3: Service role key — ⚠️  MANUAL STEP ⚠️
-- ──────────────────────────────────────────────────────────────
-- The service role key bypasses RLS — do NOT commit it to git.
-- After running this migration, paste this command into the
-- Supabase SQL Editor with YOUR actual key:
--
--   SELECT vault.create_secret(
--     'supabase_service_role',
--     'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.PASTE_YOUR_KEY_HERE',
--     'Supabase service_role key — grants full DB access'
--   );
--
-- Find your key at:
--   Supabase Dashboard → Settings → API → service_role key (secret)
--
-- The cron jobs WILL FAIL with "Vault secret not found" until you
-- complete this step.

-- ──────────────────────────────────────────────────────────────
-- Step 4: Helper function — read from Vault with clear errors
-- ──────────────────────────────────────────────────────────────
-- Supabase Vault stores encrypted rows in vault.secrets.
-- The vault.decrypted_secrets view exposes a decrypted_secret
-- column that auto-decrypts via pgsodium.
--
-- NOTE: vault.read_secret() does NOT exist in many Supabase
-- versions — the decrypted_secrets view is the reliable API.

CREATE OR REPLACE FUNCTION vault_read(p_name TEXT)
RETURNS TEXT
LANGUAGE plpgsql
STABLE
STRICT
AS $$
DECLARE
  v_value TEXT;
BEGIN
  SELECT decrypted_secret INTO v_value
  FROM vault.decrypted_secrets
  WHERE name = p_name
  LIMIT 1;

  IF v_value IS NULL THEN
    RAISE EXCEPTION
      'Vault secret "%" not found. Create it with: SELECT vault.create_secret(''%'', ''your-value'');',
      p_name, p_name;
  END IF;

  RETURN v_value;
END;
$$;

-- Restrict execution to postgres role only (this function returns secrets)
REVOKE ALL ON FUNCTION vault_read(TEXT) FROM public;
REVOKE ALL ON FUNCTION vault_read(TEXT) FROM anon;
REVOKE ALL ON FUNCTION vault_read(TEXT) FROM authenticated;

-- ──────────────────────────────────────────────────────────────
-- Step 5: Edge function invoker — single symbol
-- ──────────────────────────────────────────────────────────────
-- Called by pg_cron jobs.  SECURITY DEFINER runs as the function
-- owner (postgres), which has Vault access.  The cron job itself
-- never touches the service role key — it just calls this function.

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

-- ──────────────────────────────────────────────────────────────
-- Step 6: Edge function invoker — batch by asset class
-- ──────────────────────────────────────────────────────────────
-- Alternative: invoke an edge function for an entire asset class
-- at once.  The edge function processes symbols sequentially with
-- rate-limit delays built in.
-- Currently unused by cron jobs but available for manual invocations
-- or future batch scheduling.

CREATE OR REPLACE FUNCTION sim_invoke_edge_function_batch(
  p_function    TEXT,
  p_asset_class TEXT
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
    body    := jsonb_build_object('asset_class', p_asset_class)
  );
END;
$$;

REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch(TEXT, TEXT) FROM public;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION sim_invoke_edge_function_batch(TEXT, TEXT) FROM authenticated;

-- ──────────────────────────────────────────────────────────────
-- Step 7: Cleanup — remove old broken approaches
-- ──────────────────────────────────────────────────────────────
-- Drop the plaintext app_secrets table if it exists from a
-- previous run of the GUC-based migration.

DROP TABLE IF EXISTS app_secrets;
DROP FUNCTION IF EXISTS get_app_secret(TEXT);

-- ──────────────────────────────────────────────────────────────
-- Step 8: Deactivate retired symbols
-- ──────────────────────────────────────────────────────────────
-- DOT, MATIC, ADA were in migration 001 but replaced by ARB, DOGE,
-- XRP, WIF in migration 004.  Mark them inactive so the edge
-- functions skip them.

UPDATE sim_symbols
SET active = false, updated_at = now()
WHERE symbol IN ('DOT', 'MATIC', 'ADA')
  AND active = true;

-- ──────────────────────────────────────────────────────────────
-- Step 9: Unschedule ALL existing cron jobs
-- ──────────────────────────────────────────────────────────────
-- Every job from migration 004 has broken auth (hardcoded
-- <SUPABASE_SERVICE_ROLE> placeholder).  Nuke them all and
-- reschedule with the Vault-based invoker.

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

-- Light-regime jobs (current + legacy symbols)
SELECT cron_safe_unschedule('light-regime-BTC');
SELECT cron_safe_unschedule('light-regime-ETH');
SELECT cron_safe_unschedule('light-regime-SOL');
SELECT cron_safe_unschedule('light-regime-AVAX');
SELECT cron_safe_unschedule('light-regime-ARB');
SELECT cron_safe_unschedule('light-regime-DOGE');
SELECT cron_safe_unschedule('light-regime-LINK');
SELECT cron_safe_unschedule('light-regime-XRP');
SELECT cron_safe_unschedule('light-regime-WIF');
SELECT cron_safe_unschedule('light-regime-AAPL');
SELECT cron_safe_unschedule('light-regime-AMZN');
SELECT cron_safe_unschedule('light-regime-GOOGL');
SELECT cron_safe_unschedule('light-regime-META');
SELECT cron_safe_unschedule('light-regime-MSFT');
SELECT cron_safe_unschedule('light-regime-NVDA');
SELECT cron_safe_unschedule('light-regime-TSLA');
SELECT cron_safe_unschedule('light-regime-SPY');
SELECT cron_safe_unschedule('light-regime-QQQ');
SELECT cron_safe_unschedule('light-regime-EURUSD');
SELECT cron_safe_unschedule('light-regime-GBPUSD');
SELECT cron_safe_unschedule('light-regime-USDJPY');
SELECT cron_safe_unschedule('light-regime-AUDUSD');
SELECT cron_safe_unschedule('light-regime-USDCAD');
SELECT cron_safe_unschedule('light-regime-GOLD');
SELECT cron_safe_unschedule('light-regime-SILVER');
SELECT cron_safe_unschedule('light-regime-OIL');
SELECT cron_safe_unschedule('light-regime-COPPER');
-- Legacy
SELECT cron_safe_unschedule('light-regime-DOT');
SELECT cron_safe_unschedule('light-regime-MATIC');
SELECT cron_safe_unschedule('light-regime-ADA');

-- Heavy-sweep jobs
SELECT cron_safe_unschedule('heavy-sweep-BTC');
SELECT cron_safe_unschedule('heavy-sweep-ETH');
SELECT cron_safe_unschedule('heavy-sweep-SOL');
SELECT cron_safe_unschedule('heavy-sweep-AVAX');
SELECT cron_safe_unschedule('heavy-sweep-ARB');
SELECT cron_safe_unschedule('heavy-sweep-DOGE');
SELECT cron_safe_unschedule('heavy-sweep-LINK');
SELECT cron_safe_unschedule('heavy-sweep-XRP');
SELECT cron_safe_unschedule('heavy-sweep-WIF');
SELECT cron_safe_unschedule('heavy-sweep-AAPL');
SELECT cron_safe_unschedule('heavy-sweep-AMZN');
SELECT cron_safe_unschedule('heavy-sweep-GOOGL');
SELECT cron_safe_unschedule('heavy-sweep-META');
SELECT cron_safe_unschedule('heavy-sweep-MSFT');
SELECT cron_safe_unschedule('heavy-sweep-NVDA');
SELECT cron_safe_unschedule('heavy-sweep-TSLA');
SELECT cron_safe_unschedule('heavy-sweep-SPY');
SELECT cron_safe_unschedule('heavy-sweep-QQQ');
SELECT cron_safe_unschedule('heavy-sweep-EURUSD');
SELECT cron_safe_unschedule('heavy-sweep-GBPUSD');
SELECT cron_safe_unschedule('heavy-sweep-USDJPY');
SELECT cron_safe_unschedule('heavy-sweep-AUDUSD');
SELECT cron_safe_unschedule('heavy-sweep-USDCAD');
SELECT cron_safe_unschedule('heavy-sweep-GOLD');
SELECT cron_safe_unschedule('heavy-sweep-SILVER');
SELECT cron_safe_unschedule('heavy-sweep-OIL');
SELECT cron_safe_unschedule('heavy-sweep-COPPER');

-- ──────────────────────────────────────────────────────────────
-- Step 10: Reschedule light-regime cron jobs
-- ──────────────────────────────────────────────────────────────
-- Batch A: Crypto — fires at :00, :15, :30, :45
-- 9 symbols × 2 API calls (regime + Kelly) = 18 req over ~5 min ≈ 3.6 req/min
-- Well under Noble Trader's 10 req/min rate limit.

SELECT cron.schedule('light-regime-BTC',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'BTC');  $$);
SELECT cron.schedule('light-regime-ETH',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'ETH');  $$);
SELECT cron.schedule('light-regime-SOL',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'SOL');  $$);
SELECT cron.schedule('light-regime-AVAX', '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AVAX'); $$);
SELECT cron.schedule('light-regime-ARB',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'ARB');  $$);
SELECT cron.schedule('light-regime-DOGE', '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'DOGE'); $$);
SELECT cron.schedule('light-regime-LINK', '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'LINK'); $$);
SELECT cron.schedule('light-regime-XRP',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'XRP');  $$);
SELECT cron.schedule('light-regime-WIF',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'WIF');  $$);

-- Batch B: Stocks + ETFs — fires at :05, :20, :35, :50

SELECT cron.schedule('light-regime-AAPL', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AAPL');  $$);
SELECT cron.schedule('light-regime-AMZN', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AMZN');  $$);
SELECT cron.schedule('light-regime-GOOGL','5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'GOOGL'); $$);
SELECT cron.schedule('light-regime-META', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'META');  $$);
SELECT cron.schedule('light-regime-MSFT', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'MSFT');  $$);
SELECT cron.schedule('light-regime-NVDA', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'NVDA');  $$);
SELECT cron.schedule('light-regime-TSLA', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'TSLA');  $$);
SELECT cron.schedule('light-regime-SPY',  '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'SPY');   $$);
SELECT cron.schedule('light-regime-QQQ',  '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'QQQ');   $$);

-- Batch C: Forex + Commodities — fires at :10, :25, :40, :55

SELECT cron.schedule('light-regime-EURUSD','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'EURUSD'); $$);
SELECT cron.schedule('light-regime-GBPUSD','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'GBPUSD'); $$);
SELECT cron.schedule('light-regime-USDJPY','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'USDJPY'); $$);
SELECT cron.schedule('light-regime-AUDUSD','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AUDUSD'); $$);
SELECT cron.schedule('light-regime-USDCAD','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'USDCAD'); $$);
SELECT cron.schedule('light-regime-GOLD',  '10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'GOLD');   $$);
SELECT cron.schedule('light-regime-SILVER','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'SILVER'); $$);
SELECT cron.schedule('light-regime-OIL',   '10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'OIL');    $$);
SELECT cron.schedule('light-regime-COPPER','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'COPPER'); $$);

-- ──────────────────────────────────────────────────────────────
-- Step 11: Reschedule heavy-sweep cron jobs
-- ──────────────────────────────────────────────────────────────
-- Runs every 4 hours with 5-min offsets between symbols.
-- Stocks/commodities restricted to weekdays (1-5).

-- Crypto (7×24)
SELECT cron.schedule('heavy-sweep-BTC',   '0 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'BTC');   $$);
SELECT cron.schedule('heavy-sweep-ETH',   '5 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'ETH');   $$);
SELECT cron.schedule('heavy-sweep-SOL',   '10 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'SOL');   $$);
SELECT cron.schedule('heavy-sweep-AVAX',  '15 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'AVAX');  $$);
SELECT cron.schedule('heavy-sweep-ARB',   '20 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'ARB');   $$);
SELECT cron.schedule('heavy-sweep-DOGE',  '25 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'DOGE');  $$);
SELECT cron.schedule('heavy-sweep-LINK',  '30 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'LINK');  $$);
SELECT cron.schedule('heavy-sweep-XRP',   '35 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'XRP');   $$);
SELECT cron.schedule('heavy-sweep-WIF',   '40 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'WIF');   $$);

-- Stocks + ETFs (weekdays only)
SELECT cron.schedule('heavy-sweep-AAPL',  '0 2,6,10,14,18,22 * * 1-5',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'AAPL');  $$);
SELECT cron.schedule('heavy-sweep-AMZN',  '5 2,6,10,14,18,22 * * 1-5',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'AMZN');  $$);
SELECT cron.schedule('heavy-sweep-GOOGL', '10 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'GOOGL'); $$);
SELECT cron.schedule('heavy-sweep-META',  '15 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'META');  $$);
SELECT cron.schedule('heavy-sweep-MSFT',  '20 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'MSFT');  $$);
SELECT cron.schedule('heavy-sweep-NVDA',  '25 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'NVDA');  $$);
SELECT cron.schedule('heavy-sweep-TSLA',  '30 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'TSLA');  $$);
SELECT cron.schedule('heavy-sweep-SPY',   '35 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'SPY');   $$);
SELECT cron.schedule('heavy-sweep-QQQ',   '40 2,6,10,14,18,22 * * 1-5', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'QQQ');   $$);

-- Forex (7×24 — forex data updates around the clock)
SELECT cron.schedule('heavy-sweep-EURUSD','0 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'EURUSD'); $$);
SELECT cron.schedule('heavy-sweep-GBPUSD','5 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'GBPUSD'); $$);
SELECT cron.schedule('heavy-sweep-USDJPY','10 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'USDJPY'); $$);
SELECT cron.schedule('heavy-sweep-AUDUSD','15 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'AUDUSD'); $$);
SELECT cron.schedule('heavy-sweep-USDCAD','20 2,6,10,14,18,22 * * *', $$ SELECT sim_invoke_edge_function('heavy-sweep', 'USDCAD'); $$);

-- Commodities (COPPER weekday only; others 7×24)
SELECT cron.schedule('heavy-sweep-GOLD',   '25 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'GOLD');   $$);
SELECT cron.schedule('heavy-sweep-SILVER', '30 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'SILVER'); $$);
SELECT cron.schedule('heavy-sweep-OIL',    '35 2,6,10,14,18,22 * * *',  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'OIL');    $$);
SELECT cron.schedule('heavy-sweep-COPPER', '40 2,6,10,14,18,22 * * 1-5',$$ SELECT sim_invoke_edge_function('heavy-sweep', 'COPPER'); $$);

-- ──────────────────────────────────────────────────────────────
-- Step 12: Cleanup helpers
-- ──────────────────────────────────────────────────────────────

DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);

-- ──────────────────────────────────────────────────────────────
-- Post-migration checklist
-- ──────────────────────────────────────────────────────────────
--
-- 1. Run step 3 (the manual INSERT of your service role key).
--
-- 2. Verify Vault has both secrets:
--      SELECT name, decrypted_secret FROM vault.decrypted_secrets
--      WHERE name IN ('supabase_url', 'supabase_service_role');
--
-- 3. Test a single invocation:
--      SELECT sim_invoke_edge_function('light-regime', 'BTC');
--
-- 4. Check Supabase Dashboard → Edge Functions → Logs for the
--    light-regime function.  You should see a 200 response
--    instead of 401 or 500.
--
-- 5. If you see a 500 from the edge function itself (not auth),
--    check that these env vars are set in:
--    Supabase Dashboard → Edge Functions → Secrets
--      - UPSTASH_REDIS_REST_URL
--      - UPSTASH_REDIS_REST_TOKEN
--      - SB_URL
--      - SB_SERVICE_ROLE_KEY
