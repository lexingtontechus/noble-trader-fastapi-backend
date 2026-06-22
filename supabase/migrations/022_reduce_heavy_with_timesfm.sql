-- ═══════════════════════════════════════════════════════════════
-- 022: Reduce heavy sweep frequency — TimesFM-driven confidence gating
--
-- TimesFM now provides 24h regime stability estimates, allowing
-- heavy sweeps to run less frequently. The heavy sweep handler
-- also has a TimesFM confidence gate: if the forecast shows high
-- confidence (p_timesfm far from 0.5), the heavy sweep is skipped
-- entirely and the cached regime is kept.
--
-- NEW SCHEDULE:
--   Crypto (5)    : 2x/day at hours 6, 18 UTC (down from 6x/day)
--   Stocks/ETFs (9): 3x/week Mon/Wed/Fri at 21 UTC (down from 5x/week)
--
-- NET EFFECT:
--   Heavy invocations/day: 39 → 16   (-59%)
--   Plus ~70% of those will be TimesFM-skipped → ~5 actual runs/day
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
-- STEP 2: Crypto heavy — 2x/day at hours 6, 18 UTC
-- ──────────────────────────────────────────────────────────────
SELECT cron.schedule('sweep-heavy-BTC',    '0  6,18 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','BTC'));$$);
SELECT cron.schedule('sweep-heavy-ETH',    '5  6,18 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','ETH'));$$);
SELECT cron.schedule('sweep-heavy-SOL',    '10 6,18 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','SOL'));$$);
SELECT cron.schedule('sweep-heavy-BTCUSD', '20 6,18 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','BTCUSD'));$$);
SELECT cron.schedule('sweep-heavy-ETHUSD', '25 6,18 * * *', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','ETHUSD'));$$);

-- ──────────────────────────────────────────────────────────────
-- STEP 3: Stocks/ETFs heavy — 3x/week Mon/Wed/Fri at 21 UTC
-- ──────────────────────────────────────────────────────────────
SELECT cron.schedule('sweep-heavy-AAPL',  '00 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','AAPL'));$$);
SELECT cron.schedule('sweep-heavy-AMZN',  '05 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','AMZN'));$$);
SELECT cron.schedule('sweep-heavy-GOOGL', '10 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','GOOGL'));$$);
SELECT cron.schedule('sweep-heavy-META',  '15 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','META'));$$);
SELECT cron.schedule('sweep-heavy-MSFT',  '20 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','MSFT'));$$);
SELECT cron.schedule('sweep-heavy-NVDA',  '25 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','NVDA'));$$);
SELECT cron.schedule('sweep-heavy-TSLA',  '30 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','TSLA'));$$);
SELECT cron.schedule('sweep-heavy-SPY',   '35 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','SPY'));$$);
SELECT cron.schedule('sweep-heavy-QQQ',   '40 21 * * 1,3,5', $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','QQQ'));$$);

-- ──────────────────────────────────────────────────────────────
-- STEP 4: Update nt_symbol sweep_cron for audit accuracy
-- ──────────────────────────────────────────────────────────────
UPDATE nt_symbol SET sweep_cron = '0  6,18 * * *',    updated_at = now() WHERE symbol = 'BTC'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '5  6,18 * * *',    updated_at = now() WHERE symbol = 'ETH'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '10 6,18 * * *',   updated_at = now() WHERE symbol = 'SOL'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '20 6,18 * * *',   updated_at = now() WHERE symbol = 'BTCUSD' AND active = true;
UPDATE nt_symbol SET sweep_cron = '25 6,18 * * *',   updated_at = now() WHERE symbol = 'ETHUSD' AND active = true;

UPDATE nt_symbol SET sweep_cron = '00 21 * * 1,3,5', updated_at = now() WHERE symbol = 'AAPL'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '05 21 * * 1,3,5', updated_at = now() WHERE symbol = 'AMZN'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '10 21 * * 1,3,5', updated_at = now() WHERE symbol = 'GOOGL' AND active = true;
UPDATE nt_symbol SET sweep_cron = '15 21 * * 1,3,5', updated_at = now() WHERE symbol = 'META'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '20 21 * * 1,3,5', updated_at = now() WHERE symbol = 'MSFT'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '25 21 * * 1,3,5', updated_at = now() WHERE symbol = 'NVDA'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '30 21 * * 1,3,5', updated_at = now() WHERE symbol = 'TSLA'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '35 21 * * 1,3,5', updated_at = now() WHERE symbol = 'SPY'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '40 21 * * 1,3,5', updated_at = now() WHERE symbol = 'QQQ'   AND active = true;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (run manually after applying):
--   SELECT jobname, schedule FROM cron.job WHERE jobname LIKE 'sweep-heavy-%' ORDER BY jobname;
-- Expected: crypto at * 6,18 * * *, stocks at * 21 * * 1,3,5
-- ═══════════════════════════════════════════════════════════════
