-- ═══════════════════════════════════════════════════════════════
-- 025: Fix broken light sweep cron bodies
--
-- Migration 021 accidentally introduced malformed jsonb_build_object
-- calls in the light sweep cron jobs:
--
--   jsonb_build_object('mode','light','crypto')
--      → 3 args → ERROR: "argument list must have even number of elements"
--
--   jsonb_build_object('mode','light','stocks','offset',1)
--   jsonb_build_object('mode','light','stocks','offset',2)
--      → 5 args → same error
--
-- Additionally, the sweep edge function does NOT accept 'stocks',
-- 'offset', or 'crypto' parameters. It accepts 'batch' (array of
-- symbols) or 'symbol' (single symbol).
--
-- Fix: Replace with correct batch syntax (matching migration 015).
-- ═══════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────
-- STEP 1: Unschedule all three broken light cron jobs
-- ──────────────────────────────────────────────────────────────
SELECT cron_safe_unschedule('sweep-light-crypto');
SELECT cron_safe_unschedule('sweep-light-stocks-1');
SELECT cron_safe_unschedule('sweep-light-stocks-2');

-- ──────────────────────────────────────────────────────────────
-- STEP 2: Re-schedule with correct body syntax
-- ──────────────────────────────────────────────────────────────

-- Light crypto batch (BTC, ETH, SOL) — every day, 4x/hr in light hours
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

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (optional):
--   SELECT jobname, schedule FROM cron.job WHERE jobname LIKE 'sweep-light-%' ORDER BY jobname;
-- ═══════════════════════════════════════════════════════════════
