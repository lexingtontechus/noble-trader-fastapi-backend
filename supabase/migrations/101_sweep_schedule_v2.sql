-- ═══════════════════════════════════════════════════════════════
-- Migration 101: Sweep Schedule v2 — Reduced Heavy Frequency
--
-- Changes:
--   1. Heavy sweeps: 6×/day → 1×/day at 20:00 UTC (off-hours)
--      Crypto 7 days/week, stocks weekdays only
--      Brick size range widened [2,5] → [3,7], lookback 45d → 90d
--   2. Light regime: 4 sub-batches → 3 sub-batches at :00/:05/:10
--      Wider stagger gaps (5 min) to eliminate pooler pileup
--   3. F-Lite-1: heavy hours reduced from [2,6,10,14,18,22] → [20]
--      Light suppressed only during hour 20 (1h vs 6h daily)
--
-- TimesFM handles non-stationarity 24/7, so heavy sweeps are
-- reduced to a daily fine-tuning + calibration check.
-- ═══════════════════════════════════════════════════════════════

-- ─── Helper: unschedule all old sweep cron jobs ─────────────
-- (Ensures idempotency — safe to run multiple times)

DO $$
DECLARE
  job_names TEXT[] := ARRAY[
    -- Old heavy (6×/day, migration 015)
    'sweep-heavy-BTC', 'sweep-heavy-ETH', 'sweep-heavy-SOL',
    'sweep-heavy-BTCUSD', 'sweep-heavy-ETHUSD',
    'sweep-heavy-AAPL', 'sweep-heavy-AMZN', 'sweep-heavy-GOOGL',
    'sweep-heavy-META', 'sweep-heavy-MSFT', 'sweep-heavy-NVDA',
    'sweep-heavy-TSLA', 'sweep-heavy-SPY', 'sweep-heavy-QQQ',
    -- Old light (4 sub-batches, migration 015)
    'sweep-light-crypto',
    'sweep-light-stocks-1-wk', 'sweep-light-stocks-2-wk', 'sweep-light-stocks-3-wk',
    'sweep-light-stocks-1-we', 'sweep-light-stocks-2-we', 'sweep-light-stocks-3-we'
  ];
  j TEXT;
BEGIN
  FOREACH j IN ARRAY job_names LOOP
    BEGIN
      PERFORM cron.unschedule(j);
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'cron.unschedule(%) failed: %', j, SQLERRM;
    END;
  END LOOP;
END;
$$;

-- ═══════════════════════════════════════════════════════════════
-- HEAVY SWEEP — Daily off-hours (20:00 UTC)
-- ═══════════════════════════════════════════════════════════════
-- Parameters (set in edge function, not migration):
--   brick_size range: [3, 7]
--   lookback: 90 days
--   min_trades: 10
--
-- Staggered every 5 minutes within the 20:00 hour.
-- Crypto: daily. Stocks: weekdays only.

-- Crypto perps (daily)
SELECT cron.schedule(
  'sweep-heavy-BTC',
  '0 20 * * *',
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

SELECT cron.schedule(
  'sweep-heavy-ETH',
  '5 20 * * *',
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

SELECT cron.schedule(
  'sweep-heavy-SOL',
  '10 20 * * *',
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

-- Alpaca crypto spot (daily)
SELECT cron.schedule(
  'sweep-heavy-BTCUSD',
  '15 20 * * *',
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

SELECT cron.schedule(
  'sweep-heavy-ETHUSD',
  '20 20 * * *',
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

-- Stocks/ETFs (weekdays only)
SELECT cron.schedule(
  'sweep-heavy-AAPL',
  '25 20 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-AMZN',
  '30 20 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-GOOGL',
  '35 20 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-META',
  '40 20 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-MSFT',
  '45 20 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-NVDA',
  '50 20 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-TSLA',
  '55 20 * * 1-5',
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

-- SPY and QQQ at start of next hour (still off-hours)
SELECT cron.schedule(
  'sweep-heavy-SPY',
  '0 21 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-QQQ',
  '5 21 * * 1-5',
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

-- ═══════════════════════════════════════════════════════════════
-- LIGHT REGIME — 3 sub-batches, 5-min gaps, 23 hours/day
-- ═══════════════════════════════════════════════════════════════
-- F-Lite-1: light suppressed during hour 20 only.
-- Light hours: all hours except 20 (23 hours = 92 cycles/day)
--
-- Batch 1 (crypto perps): :00, :15, :30, :45
-- Batch 2 (stocks A):     :05, :20, :35, :50
-- Batch 3 (stocks B):     :10, :25, :40, :55

-- Light crypto (BTC, ETH, SOL) — daily, every 15 min during 23 light hours
SELECT cron.schedule(
  'sweep-light-crypto',
  '0,15,30,45 0-19,21-23 * * *',
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

-- Light stocks batch 1 (AAPL, AMZN, GOOGL, META) — weekdays
SELECT cron.schedule(
  'sweep-light-stocks-1',
  '5,20,35,50 0-19,21-23 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["AAPL","AMZN","GOOGL","META"]'::jsonb)
  );
  $$
);

-- Light stocks batch 2 (MSFT, NVDA, TSLA, SPY, QQQ) — weekdays
SELECT cron.schedule(
  'sweep-light-stocks-2',
  '10,25,40,55 0-19,21-23 * * 1-5',
  $$
  select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
  select net.http_post(
    url := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key'),
      'Content-Type', 'application/json'
    ),
    body := jsonb_build_object('mode', 'light', 'batch', '["MSFT","NVDA","TSLA","SPY","QQQ"]'::jsonb)
  );
  $$
);
