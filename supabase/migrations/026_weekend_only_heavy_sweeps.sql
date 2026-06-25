-- ═══════════════════════════════════════════════════════════════
-- 026: Weekend-Only Heavy Sweeps — Light Mode Runs 24/7 Weekdays
--
-- PROBLEM:
--   Heavy sweeps run at hours 2,6,10,14,18,22 UTC (6×/day, crypto)
--   and 21 UTC (stocks weekdays). During US peak hours (14 UTC=10am ET,
--   18 UTC=2pm ET), heavy jobs collide with light sweeps via Redis
--   per-symbol lock. Heavy (360s TTL) wins over light (90s TTL),
--   causing light mode to skip cycles during peak trading.
--
--   The F-Lite-1 guard also unnecessarily blocks light at 20 UTC
--   (4pm ET) on weekdays when no heavy sweeps run.
--
-- SOLUTION:
--   Move ALL heavy sweeps to weekends (Saturday + Sunday) only.
--   Light sweeps split into weekday (24h, no suppression) and
--   weekend (23h, suppressed during hour 20 when heavy runs).
--   sim_retry_stale_symbols() F-Lite-1 check removed entirely.
--
-- WHAT CHANGES:
--   1. Heavy sweeps → only on 0,6 (Sat/Sun) at 20 UTC
--   2. Light sweeps → 24h on weekdays (* * * 1-5)
--   3. Light sweeps → 23h on weekends (0-19,21-23 * * 0,6)
--   4. Retry-stale no longer skips any hour (no heavy on weekdays)
--   5. HEAVY_HOURS_UTC updated in sweep/index.ts separately
-- ═══════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────
-- HELPER: Safe unschedule (idempotent)
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

-- ═══════════════════════════════════════════════════════════════
-- STEP 1: Unschedule ALL existing heavy sweep cron jobs
-- ═══════════════════════════════════════════════════════════════

-- Crypto
SELECT cron_safe_unschedule('sweep-heavy-BTC');
SELECT cron_safe_unschedule('sweep-heavy-ETH');
SELECT cron_safe_unschedule('sweep-heavy-SOL');
SELECT cron_safe_unschedule('sweep-heavy-BTCUSD');
SELECT cron_safe_unschedule('sweep-heavy-ETHUSD');

-- Stocks/ETFs
SELECT cron_safe_unschedule('sweep-heavy-AAPL');
SELECT cron_safe_unschedule('sweep-heavy-AMZN');
SELECT cron_safe_unschedule('sweep-heavy-GOOGL');
SELECT cron_safe_unschedule('sweep-heavy-META');
SELECT cron_safe_unschedule('sweep-heavy-MSFT');
SELECT cron_safe_unschedule('sweep-heavy-NVDA');
SELECT cron_safe_unschedule('sweep-heavy-TSLA');
SELECT cron_safe_unschedule('sweep-heavy-SPY');
SELECT cron_safe_unschedule('sweep-heavy-QQQ');

-- ═══════════════════════════════════════════════════════════════
-- STEP 2: Reschedule heavy sweeps — weekends only (0,6 = Sun,Sat)
--         Staggered 5 min apart starting at 20:00 UTC
-- ═══════════════════════════════════════════════════════════════

-- Crypto (daily → weekends only)
SELECT cron.schedule(
  'sweep-heavy-BTC',  '0 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','BTC'));$$
);
SELECT cron.schedule(
  'sweep-heavy-ETH',  '5 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','ETH'));$$
);
SELECT cron.schedule(
  'sweep-heavy-SOL',  '10 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','SOL'));$$
);
SELECT cron.schedule(
  'sweep-heavy-BTCUSD', '15 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','BTCUSD'));$$
);
SELECT cron.schedule(
  'sweep-heavy-ETHUSD', '20 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','ETHUSD'));$$
);

-- Stocks/ETFs (weekdays only → weekends only)
SELECT cron.schedule(
  'sweep-heavy-AAPL',  '30 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','AAPL'));$$
);
SELECT cron.schedule(
  'sweep-heavy-AMZN',  '35 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','AMZN'));$$
);
SELECT cron.schedule(
  'sweep-heavy-GOOGL', '40 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','GOOGL'));$$
);
SELECT cron.schedule(
  'sweep-heavy-META',  '45 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','META'));$$
);
SELECT cron.schedule(
  'sweep-heavy-MSFT',  '50 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','MSFT'));$$
);
SELECT cron.schedule(
  'sweep-heavy-NVDA',  '55 20 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','NVDA'));$$
);
SELECT cron.schedule(
  'sweep-heavy-TSLA',  '0 21 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','TSLA'));$$
);
SELECT cron.schedule(
  'sweep-heavy-SPY',   '5 21 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','SPY'));$$
);
SELECT cron.schedule(
  'sweep-heavy-QQQ',   '10 21 * * 0,6',
  $$select set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true); select net.http_post(url:='https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep', headers:=jsonb_build_object('Authorization','Bearer '||current_setting('app.settings.service_role_key'),'Content-Type','application/json'), body:=jsonb_build_object('mode','heavy','symbol','QQQ'));$$
);

-- ═══════════════════════════════════════════════════════════════
-- STEP 3: Unschedule ALL existing light sweep cron jobs
-- ═══════════════════════════════════════════════════════════════

SELECT cron_safe_unschedule('sweep-light-crypto');
SELECT cron_safe_unschedule('sweep-light-stocks-1');
SELECT cron_safe_unschedule('sweep-light-stocks-2');

-- ═══════════════════════════════════════════════════════════════
-- STEP 4: Reschedule light sweeps — split weekday/weekend
--
-- Weekdays (1-5): 24 hours, no suppression (heavy never runs)
-- Weekends (0,6): 23 hours, suppressed at hour 20 (heavy window)
-- ═══════════════════════════════════════════════════════════════

-- Light crypto — weekdays: 24h
SELECT cron.schedule(
  'sweep-light-crypto-wk',
  '0,15,30,45 * * * 1-5',
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

-- Light crypto — weekends: 23h (suppressed at hour 20)
SELECT cron.schedule(
  'sweep-light-crypto-we',
  '0,15,30,45 0-19,21-23 * * 0,6',
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

-- Light stocks batch 1 (AAPL, AMZN, GOOGL, META) — weekdays: 24h
SELECT cron.schedule(
  'sweep-light-stocks-1-wk',
  '5,20,35,50 * * * 1-5',
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

-- Light stocks batch 1 — weekends: 23h
SELECT cron.schedule(
  'sweep-light-stocks-1-we',
  '5,20,35,50 0-19,21-23 * * 0,6',
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

-- Light stocks batch 2 (MSFT, NVDA, TSLA, SPY, QQQ) — weekdays: 24h
SELECT cron.schedule(
  'sweep-light-stocks-2-wk',
  '10,25,40,55 * * * 1-5',
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

-- Light stocks batch 2 — weekends: 23h
SELECT cron.schedule(
  'sweep-light-stocks-2-we',
  '10,25,40,55 0-19,21-23 * * 0,6',
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
-- STEP 5: Update sim_retry_stale_symbols() — remove F-Lite-1 check
--
-- The old check blocked retries during hours 2,6,10,14,18,22 UTC.
-- Since heavy sweeps now run ONLY on weekends, retry-stale should
-- never skip on weekdays. The edge function's per-symbol lock
-- handles any weekend collision between retry and heavy sweep.
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION sim_retry_stale_symbols()
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  v_symbol     TEXT;
  v_url        TEXT := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep';
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
  v_invoked    INTEGER := 0;
BEGIN
  -- Resolve service_role key from vault
  v_auth := 'Bearer ' || vault_read('supabase_service_role');

  FOR v_symbol IN
    SELECT s.symbol
    FROM nt_symbol s
    WHERE s.active = true
      AND NOT EXISTS (
        SELECT 1
        FROM nt_regime_log rl
        WHERE rl.symbol = s.symbol
          AND rl.detected_at > now() - interval '20 minutes'
      )
    ORDER BY s.symbol
  LOOP
    RAISE NOTICE '[retry-sweep] %: invoke mode=light', v_symbol;

    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := jsonb_build_object('mode', 'light', 'symbol', v_symbol)
    ) INTO v_request_id;

    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      v_status := NULL;
    END;

    RAISE NOTICE '[retry-sweep] %: status=%', v_symbol, v_status;
    v_invoked := v_invoked + 1;

    -- Small delay between retries to respect global concurrency cap (4)
    PERFORM pg_sleep(2);
  END LOOP;

  RAISE NOTICE '[retry-sweep] done: invoked=%', v_invoked;
END;
$$;

REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM public;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM anon;
REVOKE ALL ON FUNCTION sim_retry_stale_symbols() FROM authenticated;

-- ═══════════════════════════════════════════════════════════════
-- STEP 6: Update nt_symbol.sweep_cron values for audit accuracy
-- ═══════════════════════════════════════════════════════════════

UPDATE nt_symbol SET sweep_cron = '0 20 * * 0,6',   updated_at = now() WHERE symbol = 'BTC'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '5 20 * * 0,6',   updated_at = now() WHERE symbol = 'ETH'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '10 20 * * 0,6',  updated_at = now() WHERE symbol = 'SOL'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '15 20 * * 0,6',  updated_at = now() WHERE symbol = 'BTCUSD' AND active = true;
UPDATE nt_symbol SET sweep_cron = '20 20 * * 0,6',  updated_at = now() WHERE symbol = 'ETHUSD' AND active = true;
UPDATE nt_symbol SET sweep_cron = '30 20 * * 0,6',  updated_at = now() WHERE symbol = 'AAPL'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '35 20 * * 0,6',  updated_at = now() WHERE symbol = 'AMZN'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '40 20 * * 0,6',  updated_at = now() WHERE symbol = 'GOOGL'  AND active = true;
UPDATE nt_symbol SET sweep_cron = '45 20 * * 0,6',  updated_at = now() WHERE symbol = 'META'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '50 20 * * 0,6',  updated_at = now() WHERE symbol = 'MSFT'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '55 20 * * 0,6',  updated_at = now() WHERE symbol = 'NVDA'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '0 21 * * 0,6',   updated_at = now() WHERE symbol = 'TSLA'   AND active = true;
UPDATE nt_symbol SET sweep_cron = '5 21 * * 0,6',   updated_at = now() WHERE symbol = 'SPY'    AND active = true;
UPDATE nt_symbol SET sweep_cron = '10 21 * * 0,6',  updated_at = now() WHERE symbol = 'QQQ'    AND active = true;

-- ═══════════════════════════════════════════════════════════════
-- CLEANUP
-- ═══════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (run manually after applying):
--   SELECT jobname, schedule, job.what::TEXT FROM cron.job
--   WHERE jobname LIKE 'sweep-%' ORDER BY jobname;
--
-- Expected:
--   sweep-heavy-* → 0,6 day-of-week (Sat/Sun), N 20 or N 21
--   sweep-light-*-wk → * * * 1-5 (weekdays, all hours)
--   sweep-light-*-we → 0-19,21-23 * * 0,6 (weekends, no hour 20)
--   (no sweep-heavy-* with 1-5 or * day-of-week)
-- ═══════════════════════════════════════════════════════════════
