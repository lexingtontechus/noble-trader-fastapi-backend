-- Migration 004: Stagger light-regime pg_cron jobs into 3 batches
-- Problem: All 26 symbols fire at :00, :15, :30, :45 hitting Noble Trader's
--          10 req/min rate limit (each symbol makes 2 calls: regime + Kelly).
-- Solution: Split into 3 batches spread across the 15-min window:
--   Batch A (9 symbols): fires at :00 / :15 / :30 / :45  → 9 symbols × 2 calls = 18 req → OK over 5 min
--   Batch B (9 symbols): fires at :05 / :20 / :35 / :50
--   Batch C (8 symbols): fires at :10 / :25 / :40 / :55
-- This ensures no more than 9 symbols (18 API calls) fire within a 5-minute window,
-- well under Noble Trader's limit of 10 req/min = 50 req/5min.

-- ─── Safe unschedule helper ────────────────────────────────
-- cron.unschedule() throws if the job doesn't exist, so we wrap it

CREATE OR REPLACE FUNCTION cron_safe_unschedule(p_job_name TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM cron.unschedule(p_job_name);
  RAISE NOTICE 'Unscheduled job %', p_job_name;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'Job % not found (skipping)', p_job_name;
END;
$$;

-- ─── First, unschedule all existing light-regime jobs ───────

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
-- Legacy symbols that may still have cron jobs
SELECT cron_safe_unschedule('light-regime-DOT');
SELECT cron_safe_unschedule('light-regime-MATIC');
SELECT cron_safe_unschedule('light-regime-ADA');

-- ─── Batch A: Crypto — fires at :00, :15, :30, :45 ─────────
-- 9 symbols × 2 API calls = 18 requests spread over ~5 min → ~3.6 req/min

SELECT cron.schedule(
  'light-regime-BTC',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'BTC')
  ); $$
);

SELECT cron.schedule(
  'light-regime-ETH',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'ETH')
  ); $$
);

SELECT cron.schedule(
  'light-regime-SOL',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'SOL')
  ); $$
);

SELECT cron.schedule(
  'light-regime-AVAX',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'AVAX')
  ); $$
);

SELECT cron.schedule(
  'light-regime-ARB',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'ARB')
  ); $$
);

SELECT cron.schedule(
  'light-regime-DOGE',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'DOGE')
  ); $$
);

SELECT cron.schedule(
  'light-regime-LINK',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'LINK')
  ); $$
);

SELECT cron.schedule(
  'light-regime-XRP',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'XRP')
  ); $$
);

SELECT cron.schedule(
  'light-regime-WIF',
  '0,15,30,45 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'WIF')
  ); $$
);

-- ─── Batch B: Stocks + ETFs — fires at :05, :20, :35, :50 ──
-- 9 symbols × 2 API calls = 18 requests spread over ~5 min → ~3.6 req/min

SELECT cron.schedule(
  'light-regime-AAPL',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'AAPL')
  ); $$
);

SELECT cron.schedule(
  'light-regime-AMZN',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'AMZN')
  ); $$
);

SELECT cron.schedule(
  'light-regime-GOOGL',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'GOOGL')
  ); $$
);

SELECT cron.schedule(
  'light-regime-META',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'META')
  ); $$
);

SELECT cron.schedule(
  'light-regime-MSFT',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'MSFT')
  ); $$
);

SELECT cron.schedule(
  'light-regime-NVDA',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'NVDA')
  ); $$
);

SELECT cron.schedule(
  'light-regime-TSLA',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'TSLA')
  ); $$
);

SELECT cron.schedule(
  'light-regime-SPY',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'SPY')
  ); $$
);

SELECT cron.schedule(
  'light-regime-QQQ',
  '5,20,35,50 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'QQQ')
  ); $$
);

-- ─── Batch C: Forex + Commodities — fires at :10, :25, :40, :55
-- 8 symbols × 2 API calls = 16 requests spread over ~5 min → ~3.2 req/min

SELECT cron.schedule(
  'light-regime-EURUSD',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'EURUSD')
  ); $$
);

SELECT cron.schedule(
  'light-regime-GBPUSD',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'GBPUSD')
  ); $$
);

SELECT cron.schedule(
  'light-regime-USDJPY',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'USDJPY')
  ); $$
);

SELECT cron.schedule(
  'light-regime-AUDUSD',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'AUDUSD')
  ); $$
);

SELECT cron.schedule(
  'light-regime-USDCAD',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'USDCAD')
  ); $$
);

SELECT cron.schedule(
  'light-regime-GOLD',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'GOLD')
  ); $$
);

SELECT cron.schedule(
  'light-regime-SILVER',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'SILVER')
  ); $$
);

SELECT cron.schedule(
  'light-regime-OIL',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'OIL')
  ); $$
);

SELECT cron.schedule(
  'light-regime-COPPER',
  '10,25,40,55 * * * *',
  $$ SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'),
    body := jsonb_build_object('symbol', 'COPPER')
  ); $$
);

-- ─── Also update sim_symbols regime_cron values ─────────────
-- Keep the DB in sync with the actual cron schedules

-- Batch A (Crypto)
UPDATE sim_symbols SET regime_cron = '0,15,30,45 * * * *' WHERE symbol IN ('BTC','ETH','SOL','AVAX','ARB','DOGE','LINK','XRP','WIF');
-- Batch B (Stocks + ETFs)
UPDATE sim_symbols SET regime_cron = '5,20,35,50 * * * *' WHERE symbol IN ('AAPL','AMZN','GOOGL','META','MSFT','NVDA','TSLA','SPY','QQQ');
-- Batch C (Forex + Commodities)
UPDATE sim_symbols SET regime_cron = '10,25,40,55 * * * *' WHERE symbol IN ('EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','GOLD','SILVER','OIL');
UPDATE sim_symbols SET regime_cron = '10,25,40,55 * * * *' WHERE symbol = 'COPPER';

-- Clean up the helper function (no longer needed)
DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);
