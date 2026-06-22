-- ═══════════════════════════════════════════════════════════════
-- 021: Fix cron schedules — crypto 6x/day, light hours corrected
--
-- Fixes three issues:
--   1. Crypto heavy sweeps: restore 6x/day (currently 1x/day)
--   2. Light sweep exclusion hours: remove old 015 remnants (2,6,10,14,18,22)
--      → only suppress light during hour 20 when heavy runs
--   3. Remove redundant per-symbol light jobs (BTCUSD, ETHUSD)
--      → sweep-light-crypto already covers them
-- ═══════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────
-- STEP 1: Unschedule ALL existing heavy jobs
-- ──────────────────────────────────────────────────────────────
SELECT cron_safe_unschedule('sweep-heavy-BTC');
SELECT cron_safe_unschedule('sweep-heavy-ETH');
SELECT cron_safe_unschedule('sweep-heavy-SOL');
SELECT cron_safe_unschedule('sweep-heavy-BTCUSD');
SELECT cron_safe_unschedule('sweep-heavy-ETHUSD');
SELECT cron_safe_unschedule('sweep-heavy-AAPL');
SELECT cron_safe_unschedule('sweep-heavy-AMZN');
SELECT cron_safe_unschedule('sweep-heavy-GOOGL');
SELECT cron_safe_unschedule('sweep-heavy-META');
SELECT cron_safe_unschedule('sweep-heavy-MSFT');
SELECT cron_safe_unschedule('sweep-heavy-NVDA');
SELECT cron_safe_unschedule('sweep-heavy-TSLA');
SELECT cron_safe_unschedule('sweep-heavy-SPY');
SELECT cron_safe_unschedule('sweep-heavy-QQQ');

-- ──────────────────────────────────────────────────────────────
-- STEP 2: Reschedule crypto heavy — 6x/day at hours 2,6,10,14,18,22
-- ──────────────────────────────────────────────────────────────
SELECT cron.schedule('sweep-heavy-BTC',    '0  2,6,10,14,18,22 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','BTC'));$$);
SELECT cron.schedule('sweep-heavy-ETH',    '5  2,6,10,14,18,22 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','ETH'));$$);
SELECT cron.schedule('sweep-heavy-SOL',    '10 2,6,10,14,18,22 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','SOL'));$$);
SELECT cron.schedule('sweep-heavy-BTCUSD', '20 2,6,10,14,18,22 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','BTCUSD'));$$);
SELECT cron.schedule('sweep-heavy-ETHUSD', '25 2,6,10,14,18,22 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','ETHUSD'));$$);

-- ──────────────────────────────────────────────────────────────
-- STEP 3: Reschedule stocks/ETFs heavy — 1x/day at 21 UTC weekdays
-- ──────────────────────────────────────────────────────────────
SELECT cron.schedule('sweep-heavy-AAPL',  '00 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','AAPL'));$$);
SELECT cron.schedule('sweep-heavy-AMZN',  '05 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','AMZN'));$$);
SELECT cron.schedule('sweep-heavy-GOOGL', '10 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','GOOGL'));$$);
SELECT cron.schedule('sweep-heavy-META',  '15 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','META'));$$);
SELECT cron.schedule('sweep-heavy-MSFT',  '20 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','MSFT'));$$);
SELECT cron.schedule('sweep-heavy-NVDA',  '25 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','NVDA'));$$);
SELECT cron.schedule('sweep-heavy-TSLA',  '30 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','TSLA'));$$);
SELECT cron.schedule('sweep-heavy-SPY',   '35 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','SPY'));$$);
SELECT cron.schedule('sweep-heavy-QQQ',   '40 21 * * 1-5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','QQQ'));$$);

-- ──────────────────────────────────────────────────────────────
-- STEP 4: Remove redundant per-symbol light sweeps
-- (sweep-light-crypto already covers BTCUSD, ETHUSD)
-- ──────────────────────────────────────────────────────────────
SELECT cron_safe_unschedule('sweep-light-BTCUSD');
SELECT cron_safe_unschedule('sweep-light-ETHUSD');

-- ──────────────────────────────────────────────────────────────
-- STEP 5: Update light sweep cron — exclude only hour 20 (heavy hour)
-- Stop excluding hours 2,6,10,14,18,22 (old 015 remnants)
-- ──────────────────────────────────────────────────────────────
SELECT cron_safe_unschedule('sweep-light-crypto');
SELECT cron.schedule(
  'sweep-light-crypto',
  '0,15,30,45 0-19,21-23 * * *',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','light','crypto'));$$
);

SELECT cron_safe_unschedule('sweep-light-stocks-1');
SELECT cron.schedule(
  'sweep-light-stocks-1',
  '5,20,35,50 0-19,21-23 * * 1-5',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','light','stocks','offset',1));$$
);

SELECT cron_safe_unschedule('sweep-light-stocks-2');
SELECT cron.schedule(
  'sweep-light-stocks-2',
  '10,25,40,55 0-19,21-23 * * 1-5',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','light','stocks','offset',2));$$
);

-- ──────────────────────────────────────────────────────────────
-- STEP 6: Update nt_symbol sweep_cron for audit accuracy
-- ──────────────────────────────────────────────────────────────
UPDATE nt_symbol SET sweep_cron = '0  2,6,10,14,18,22 * * *',   updated_at = now() WHERE symbol = 'BTC'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '5  2,6,10,14,18,22 * * *',   updated_at = now() WHERE symbol = 'ETH'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '10 2,6,10,14,18,22 * * *',  updated_at = now() WHERE symbol = 'SOL'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '20 2,6,10,14,18,22 * * *',  updated_at = now() WHERE symbol = 'BTCUSD' AND active = true;
UPDATE nt_symbol SET sweep_cron = '25 2,6,10,14,18,22 * * *',  updated_at = now() WHERE symbol = 'ETHUSD' AND active = true;

UPDATE nt_symbol SET sweep_cron = '00 21 * * 1-5', updated_at = now() WHERE symbol = 'AAPL'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '05 21 * * 1-5', updated_at = now() WHERE symbol = 'AMZN'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '10 21 * * 1-5', updated_at = now() WHERE symbol = 'GOOGL' AND active = true;
UPDATE nt_symbol SET sweep_cron = '15 21 * * 1-5', updated_at = now() WHERE symbol = 'META'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '20 21 * * 1-5', updated_at = now() WHERE symbol = 'MSFT'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '25 21 * * 1-5', updated_at = now() WHERE symbol = 'NVDA'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '30 21 * * 1-5', updated_at = now() WHERE symbol = 'TSLA'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '35 21 * * 1-5', updated_at = now() WHERE symbol = 'SPY'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '40 21 * * 1-5', updated_at = now() WHERE symbol = 'QQQ'   AND active = true;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (run manually after applying):
--   SELECT jobname, schedule FROM cron.job WHERE jobname LIKE 'sweep-%' ORDER BY jobname;
--
-- Expected after migration:
--   sweep-heavy-BTC    → 0  2,6,10,14,18,22 * * *
--   sweep-heavy-ETH    → 5  2,6,10,14,18,22 * * *
--   sweep-heavy-SOL    → 10 2,6,10,14,18,22 * * *
--   sweep-heavy-BTCUSD → 20 2,6,10,14,18,22 * * *
--   sweep-heavy-ETHUSD → 25 2,6,10,14,18,22 * * *
--   sweep-heavy-AAPL   → 00 21 * * 1-5
--   ... (9 stocks at XX 21 * * 1-5)
--   sweep-light-crypto  → 0,15,30,45 0-19,21-23 * * *
--   sweep-light-stocks-1 → 5,20,35,50 0-19,21-23 * * 1-5
--   sweep-light-stocks-2 → 10,25,40,55 0-19,21-23 * * 1-5
--   sweep-light-BTCUSD  → NOT present (removed)
--   sweep-light-ETHUSD  → NOT present (removed)
--   sweep-commodities   → 30 7 * * 1 (unchanged)
-- ═══════════════════════════════════════════════════════════════
