-- ──────────────────────────────────────────────────────────────
-- Migration 015: Unified Sweep Function + SOLUSD Removal + F-Lite-1
-- ──────────────────────────────────────────────────────────────
-- GOALS:
--   1. Deactivate SOLUSD (Alpaca) — system down from 15 → 14 symbols
--   2. Sync Redis allowlist + venue_map to reflect 14-symbol set
--   3. Reschedule all 24 cron jobs to call the new `sweep` edge function:
--      - 14 heavy jobs (unchanged schedules, new command)
--      - 9  light jobs (F-Lite-1 schedules: suppress during 6 heavy hours)
--   4. Delete 2 SOLUSD cron jobs (778, 779)
--
-- CRON COMMAND PATTERN (per user NOTE):
--   Each job uses `set_config('app.settings.service_role_key', vault_read(...), true)`
--   followed by `net.http_post(...)` — explicit, no SQL helper indirection.
--
-- F-Lite-1: Light regime suppressed during hours 02,06,10,14,18,22 UTC
--   (the 6 heavy-sweep hours). Light hours list:
--   0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23
-- ──────────────────────────────────────────────────────────────

-- ──────────────────────────────────────────────────────────────
-- STEP 1: Deactivate SOLUSD in sim_symbols
-- ──────────────────────────────────────────────────────────────

UPDATE sim_symbols
SET active = false, updated_at = now()
WHERE symbol = 'SOLUSD' AND active = true;

-- ──────────────────────────────────────────────────────────────
-- STEP 2: Sync Redis allowlist + venue_map (deactivate SOLUSD)
-- ──────────────────────────────────────────────────────────────
-- The sim_sync_redis_allowlist() function (from migration 014) reads
-- sim_symbols and writes the allowlist + venue_map to Redis via
-- net.http_post. Calling it here ensures Redis is in sync immediately.

SELECT sim_sync_redis_allowlist();

-- ──────────────────────────────────────────────────────────────
-- STEP 3: Helper — safe unschedule (idempotent)
-- ──────────────────────────────────────────────────────────────

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

-- ──────────────────────────────────────────────────────────────
-- STEP 4: Delete SOLUSD cron jobs (778, 779)
-- ──────────────────────────────────────────────────────────────

SELECT cron_safe_unschedule('heavy-sweep-SOLUSD');
SELECT cron_safe_unschedule('light-regime-SOLUSD');

-- ──────────────────────────────────────────────────────────────
-- STEP 5: Reschedule 14 heavy sweep jobs → call `sweep` function
-- ──────────────────────────────────────────────────────────────
-- Schedules UNCHANGED — only the command changes.
-- New command uses set_config + net.http_post pattern with
-- {"mode":"heavy","symbol":"XXX"} body.

-- Crypto (every day)
SELECT cron_safe_unschedule('heavy-sweep-BTC');
SELECT cron.schedule(
  'sweep-heavy-BTC',
  '0 2,6,10,14,18,22 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'BTC')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-ETH');
SELECT cron.schedule(
  'sweep-heavy-ETH',
  '5 2,6,10,14,18,22 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'ETH')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-SOL');
SELECT cron.schedule(
  'sweep-heavy-SOL',
  '10 2,6,10,14,18,22 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'SOL')
  );
  $$
);

-- Stocks (weekdays only)
SELECT cron_safe_unschedule('heavy-sweep-AAPL');
SELECT cron.schedule(
  'sweep-heavy-AAPL',
  '0 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'AAPL')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-AMZN');
SELECT cron.schedule(
  'sweep-heavy-AMZN',
  '5 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'AMZN')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-GOOGL');
SELECT cron.schedule(
  'sweep-heavy-GOOGL',
  '10 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'GOOGL')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-META');
SELECT cron.schedule(
  'sweep-heavy-META',
  '15 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'META')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-MSFT');
SELECT cron.schedule(
  'sweep-heavy-MSFT',
  '20 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'MSFT')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-NVDA');
SELECT cron.schedule(
  'sweep-heavy-NVDA',
  '25 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'NVDA')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-TSLA');
SELECT cron.schedule(
  'sweep-heavy-TSLA',
  '30 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'TSLA')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-SPY');
SELECT cron.schedule(
  'sweep-heavy-SPY',
  '35 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'SPY')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-QQQ');
SELECT cron.schedule(
  'sweep-heavy-QQQ',
  '40 2,6,10,14,18,22 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'QQQ')
  );
  $$
);

-- Alpaca crypto spot (every day)
SELECT cron_safe_unschedule('heavy-sweep-BTCUSD');
SELECT cron.schedule(
  'sweep-heavy-BTCUSD',
  '20 2,6,10,14,18,22 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'BTCUSD')
  );
  $$
);

SELECT cron_safe_unschedule('heavy-sweep-ETHUSD');
SELECT cron.schedule(
  'sweep-heavy-ETHUSD',
  '25 2,6,10,14,18,22 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'heavy', 'symbol', 'ETHUSD')
  );
  $$
);

-- ──────────────────────────────────────────────────────────────
-- STEP 6: Reschedule 9 light regime jobs → call `sweep` function
-- ──────────────────────────────────────────────────────────────
-- F-Lite-1: Light suppressed during 6 heavy hours (02,06,10,14,18,22 UTC).
-- Light hours: 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23

-- Light crypto batch (BTC, ETH, SOL) — every day, 4 ticks/hr in 18 light hours
SELECT cron_safe_unschedule('light-regime-crypto-1');
SELECT cron.schedule(
  'sweep-light-crypto',
  '0,15,30,45 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["BTC","ETH","SOL"]'::jsonb)
  );
  $$
);

-- Light stocks batch 1 (AAPL, AMZN, GOOGL) — weekdays
SELECT cron_safe_unschedule('light-regime-stocks-1-wk');
SELECT cron.schedule(
  'sweep-light-stocks-1-wk',
  '5,20,35,50 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["AAPL","AMZN","GOOGL"]'::jsonb)
  );
  $$
);

-- Light stocks batch 2 (META, MSFT, NVDA) — weekdays
SELECT cron_safe_unschedule('light-regime-stocks-2-wk');
SELECT cron.schedule(
  'sweep-light-stocks-2-wk',
  '6,21,36,51 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["META","MSFT","NVDA"]'::jsonb)
  );
  $$
);

-- Light stocks batch 3 (TSLA, SPY, QQQ) — weekdays
SELECT cron_safe_unschedule('light-regime-stocks-3-wk');
SELECT cron.schedule(
  'sweep-light-stocks-3-wk',
  '7,22,37,52 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["TSLA","SPY","QQQ"]'::jsonb)
  );
  $$
);

-- Light stocks batch 1 (AAPL, AMZN, GOOGL) — weekends
SELECT cron_safe_unschedule('light-regime-stocks-1-we');
SELECT cron.schedule(
  'sweep-light-stocks-1-we',
  '5,35 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * 0,6',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["AAPL","AMZN","GOOGL"]'::jsonb)
  );
  $$
);

-- Light stocks batch 2 (META, MSFT, NVDA) — weekends
SELECT cron_safe_unschedule('light-regime-stocks-2-we');
SELECT cron.schedule(
  'sweep-light-stocks-2-we',
  '6,36 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * 0,6',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["META","MSFT","NVDA"]'::jsonb)
  );
  $$
);

-- Light stocks batch 3 (TSLA, SPY, QQQ) — weekends
SELECT cron_safe_unschedule('light-regime-stocks-3-we');
SELECT cron.schedule(
  'sweep-light-stocks-3-we',
  '7,37 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * 0,6',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["TSLA","SPY","QQQ"]'::jsonb)
  );
  $$
);

-- Light BTCUSD (single-symbol) — every day, 4 ticks/hr in 18 light hours
SELECT cron_safe_unschedule('light-regime-BTCUSD');
SELECT cron.schedule(
  'sweep-light-BTCUSD',
  '0,15,30,45 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'symbol', 'BTCUSD')
  );
  $$
);

-- Light ETHUSD (single-symbol) — every day, 4 ticks/hr in 18 light hours
SELECT cron_safe_unschedule('light-regime-ETHUSD');
SELECT cron.schedule(
  'sweep-light-ETHUSD',
  '0,15,30,45 0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23 * * *',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'symbol', 'ETHUSD')
  );
  $$
);

-- ──────────────────────────────────────────────────────────────
-- STEP 7: Verification queries (run manually after migration)
-- ──────────────────────────────────────────────────────────────
-- Confirm 24 active jobs total:
--   SELECT count(*) FROM cron.job WHERE active = true
--   AND jobname LIKE 'sweep-%' OR jobname = 'retry-stale-symbols';
--
-- Confirm SOLUSD jobs are gone:
--   SELECT * FROM cron.job WHERE jobname LIKE '%SOLUSD%';
--
-- Confirm SOLUSD deactivated in sim_symbols:
--   SELECT symbol, active FROM sim_symbols WHERE symbol = 'SOLUSD';
--
-- Confirm Redis allowlist (run from edge function or curl):
--   POST /functions/v1/sweep  {"action":"sync-allowlist"}
