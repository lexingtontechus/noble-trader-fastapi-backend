-- =============================================================================
-- MIGRATION: 027_reshape_light_stocks_week_schedule.sql
-- Description: Reshapes the Light Stocks Weekday schedule to align with the
--              US Market Cycle, reducing compute resources by ~50%.
--
-- CURRENT STATE (from 026):
--   sweep-light-stocks-1-wk: 5,20,35,50 * * * 1-5   (24/7, 4x/hr)
--   sweep-light-stocks-2-wk: 10,25,40,55 * * * 1-5  (24/7, 4x/hr)
--
-- RESHAPED LOGIC (3 schedules):
--   Pre-Market   (05:00-12:00 UTC = 12am ET - 7am ET)  → 1x per hour
--   Peak/Ramp    (12:00-21:00 UTC = 7am ET - 4pm ET)   → 4x per hour (15min)
--   Post-Market  (21:00-05:00 UTC = 4pm ET - 12am ET)  → 1x per hour
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- STEP 1: PURGE EXISTING WEEKDAY STOCK SCHEDULES
-- -----------------------------------------------------------------------------
SELECT cron.unschedule('sweep-light-stocks-1-wk');
SELECT cron.unschedule('sweep-light-stocks-2-wk');

-- -----------------------------------------------------------------------------
-- STEP 2: APPLY RESHAPED SCHEDULES
-- -----------------------------------------------------------------------------
-- Batch 1 (AAPL, AMZN, GOOGL, META) — minute offset :05/:20/:35/:50
-- Batch 2 (MSFT, NVDA, TSLA, SPY, QQQ) — minute offset :10/:25/:40/:55

-- ── PEAK/RAMP WINDOW (12:00-21:00 UTC) ──
-- Frequency: every 15 minutes

SELECT cron.schedule(
  'sweep-light-stocks-1-wk-peak',
  '5,20,35,50 12-21 * * 1-5',
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

SELECT cron.schedule(
  'sweep-light-stocks-2-wk-peak',
  '10,25,40,55 12-21 * * 1-5',
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

-- ── PRE-MARKET WINDOW (05:00-12:00 UTC) ──
-- Frequency: once per hour

SELECT cron.schedule(
  'sweep-light-stocks-1-wk-pre',
  '5 5-11 * * 1-5',
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

SELECT cron.schedule(
  'sweep-light-stocks-2-wk-pre',
  '10 5-11 * * 1-5',
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

-- ── POST-MARKET WINDOW (21:00-05:00 UTC) ──
-- Frequency: once per hour
-- Split into evening (21:00-23:00) and morning (00:00-05:00) to handle
-- the cron hour-range wrap-around (21-5 is invalid).

SELECT cron.schedule(
  'sweep-light-stocks-1-wk-post-evening',
  '5 21-23 * * 1-5',
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

SELECT cron.schedule(
  'sweep-light-stocks-2-wk-post-evening',
  '10 21-23 * * 1-5',
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

SELECT cron.schedule(
  'sweep-light-stocks-1-wk-post-morning',
  '5 0-4 * * 1-5',
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

SELECT cron.schedule(
  'sweep-light-stocks-2-wk-post-morning',
  '10 0-4 * * 1-5',
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

-- ── NOTE: WEEKEND STOCK SCHEDULES ARE UNCHANGED ──
-- sweep-light-stocks-1-we and sweep-light-stocks-2-we keep their
-- existing 23h schedule (0-19,21-23 * * 0,6) from migration 026.

COMMIT;
