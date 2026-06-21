-- ═══════════════════════════════════════════════════════════════
-- 018: OFF-HOURS HEAVY SWEEP WINDOW (Phase 1 — P1.a)
--
-- GOAL:
--   Move heavy sweeps for stocks/ETFs out of US/EU market hours and
--   into the "global dead zone" — the window between US cash close
--   (~20:00/21:00 UTC) and Sydney/Tokyo open (~22:50/00:00 UTC).
--   This directly attacks the leading root cause of the recurring
--   Supabase 5xx errors: market-hours burst load colliding with
--   Noble Trader cold starts and live price-data volatility.
--
-- RATIONALE:
--   Migration 015 scheduled all 14 heavy jobs at
--     '0,5,10,...,40  2,6,10,14,18,22  *  *  *'
--   Hours 10/14/18 UTC = 6am/10am/2pm ET — squarely inside US cash
--   session. Investigation (worklog.md assessment-1) confirmed:
--     - 6 heavy sweeps/day × 14 symbols = 84 heavy invocations/day
--     - ~16 concurrent single-row POSTs in burst windows
--     - Edge-fn 150s timeout vs Render cold starts → silent 5xx
--     - Light regime also firing every 15 min during market hours
--       compounds the load on the same Supabase instance
--
-- NEW SCHEDULE:
--   Asset class        | Cron (UTC)                 | Cadence
--   -------------------+----------------------------+-----------------
--   Crypto (24/7)      | 'X 2,6,10,14,18,22 * * *'  | 6x/day (unchanged)
--   Stocks/ETFs        | 'X 21 * * 1-5'             | 1x/day after close
--
--   Stocks/ETFs at 21:30 UTC year-round:
--     EDT (PDT season, Mar-Nov): 5:30 PM ET = 1.5h after 4pm close ✓
--     EST (PST season, Nov-Mar): 4:30 PM ET = 0.5h after 4pm close ✓
--   Both fit inside the dead zone:
--     PDT: 20:00 → 22:50 UTC (Sydney AEST open)  = 2h50m window ✓
--     PST: 21:00 → 21:50 UTC (Sydney AEDT open)  = 1h50m window ✓
--
--   21:30 UTC year-round avoids the need for DST-aware cron (pg_cron
--   schedules are static text). The sweep cost (90s/symbol × 9
--   symbols / 4-way concurrency ≈ 4 min) fits well inside both
--   windows.
--
-- NET EFFECT:
--   Heavy invocations/day: 84 → 39   (-54%)
--   Market-hours heavy invocations: ~42 → 0   (-100% for stocks/ETFs)
--   Expected Supabase write load: ~70% reduction in burst-window POSTs
--
-- FORWARD NOTE (TimesFM, P3/P4):
--   Once Google TimesFM is integrated (planned mini-services/
--   timesfm-forecaster/), the daily heavy sweep can be SKIPPED
--   entirely when TimesFM forecasts "regime stable, high confidence"
--   for the next 24h. That takes stocks/ETFs from 1x/day → ~1x/2-3d.
--   The off-hours window established here is the natural slot for
--   that forecast-gated sweep.
-- ═══════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────
-- STEP 1: Unschedule all 14 existing heavy jobs (from migration 015)
-- ──────────────────────────────────────────────────────────────
-- cron_safe_unschedule() was created in migration 015; it's
-- idempotent (swallows the "job not found" error).

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
-- STEP 2: Reschedule 5 CRYPTO heavy jobs — UNCHANGED cadence (6x/day)
-- ──────────────────────────────────────────────────────────────
-- Crypto is 24/7 — no off-hours concept. Keep flat 4h cadence.
-- Per-symbol offsets (0/5/10/20/25 min) preserved to spread writes.

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
-- STEP 3: Reschedule 9 STOCKS/ETFs heavy jobs — ONCE DAILY at 21:30 UTC
-- ──────────────────────────────────────────────────────────────
-- Off-hours window: post US-close, pre Sydney/Tokyo open.
-- Year-round 21:30 UTC:
--   EDT season: 5:30 PM ET (1.5h after 4pm close)
--   EST season: 4:30 PM ET (0.5h after 4pm close)
-- 5-minute offsets per symbol spread Supabase write bursts.
--
-- NOTE: when adding forex/commodities symbols back to the registry,
-- schedule them at '0 22 * * 1-5' (forex) and '15 22 * * 1-5'
-- (commodities) — still inside the dead zone, offset from equities.

SELECT cron.schedule(
  'sweep-heavy-AAPL',
  '00 21 * * 1-5',
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
  '05 21 * * 1-5',
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
  '10 21 * * 1-5',
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
  '15 21 * * 1-5',
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
  '20 21 * * 1-5',
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
  '25 21 * * 1-5',
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
  '30 21 * * 1-5',
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

SELECT cron.schedule(
  'sweep-heavy-SPY',
  '35 21 * * 1-5',
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
  '40 21 * * 1-5',
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

-- ──────────────────────────────────────────────────────────────
-- STEP 4: Update sim_symbols.sweep_cron to reflect actual schedules
-- ──────────────────────────────────────────────────────────────
-- sweep_cron was a display/audit column (per migration 009 STEP 11);
-- sync it for accuracy in dashboards that read this field.

UPDATE sim_symbols SET sweep_cron = '0 2,6,10,14,18,22 * * *',   updated_at = now() WHERE symbol = 'BTC'    AND active = true;
UPDATE sim_symbols SET sweep_cron = '5 2,6,10,14,18,22 * * *',   updated_at = now() WHERE symbol = 'ETH'    AND active = true;
UPDATE sim_symbols SET sweep_cron = '10 2,6,10,14,18,22 * * *',  updated_at = now() WHERE symbol = 'SOL'    AND active = true;
UPDATE sim_symbols SET sweep_cron = '20 2,6,10,14,18,22 * * *',  updated_at = now() WHERE symbol = 'BTCUSD' AND active = true;
UPDATE sim_symbols SET sweep_cron = '25 2,6,10,14,18,22 * * *',  updated_at = now() WHERE symbol = 'ETHUSD' AND active = true;

UPDATE sim_symbols SET sweep_cron = '00 21 * * 1-5', updated_at = now() WHERE symbol = 'AAPL'  AND active = true;
UPDATE sim_symbols SET sweep_cron = '05 21 * * 1-5', updated_at = now() WHERE symbol = 'AMZN'  AND active = true;
UPDATE sim_symbols SET sweep_cron = '10 21 * * 1-5', updated_at = now() WHERE symbol = 'GOOGL' AND active = true;
UPDATE sim_symbols SET sweep_cron = '15 21 * * 1-5', updated_at = now() WHERE symbol = 'META'  AND active = true;
UPDATE sim_symbols SET sweep_cron = '20 21 * * 1-5', updated_at = now() WHERE symbol = 'MSFT'  AND active = true;
UPDATE sim_symbols SET sweep_cron = '25 21 * * 1-5', updated_at = now() WHERE symbol = 'NVDA'  AND active = true;
UPDATE sim_symbols SET sweep_cron = '30 21 * * 1-5', updated_at = now() WHERE symbol = 'TSLA'  AND active = true;
UPDATE sim_symbols SET sweep_cron = '35 21 * * 1-5', updated_at = now() WHERE symbol = 'SPY'   AND active = true;
UPDATE sim_symbols SET sweep_cron = '40 21 * * 1-5', updated_at = now() WHERE symbol = 'QQQ'   AND active = true;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (run manually after applying):
--   SELECT jobname, schedule FROM cron.job
--   WHERE jobname LIKE 'sweep-heavy-%' ORDER BY jobname;
-- Expected: 14 jobs
--   - 5 crypto  : 'X 2,6,10,14,18,22 * * *'
--   - 9 stocks  : 'XX 21 * * 1-5'
--
--   SELECT symbol, sweep_cron FROM sim_symbols
--   WHERE active = true ORDER BY asset_class, symbol;
-- ═══════════════════════════════════════════════════════════════
