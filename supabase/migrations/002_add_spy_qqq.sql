-- Migration: Add SPY & QQQ ETF symbols to the Intelligence system
-- These are broad-market ETFs for regime context (S&P 500 & Nasdaq-100)
-- Asset class: stocks, data source: yahoo

INSERT INTO sim_symbols (
  symbol,
  asset_class,
  data_source,
  yahoo_ticker,
  hyperliquid_coin,
  active,
  heartbeat_ttl_seconds,
  sweep_cron,
  regime_cron
) VALUES
  (
    'SPY',
    'stocks',
    'yahoo',
    'SPY',
    NULL,
    true,
    900,          -- 15 min heartbeat TTL
    '0 2,6,10,14,18,22 * * *',  -- Heavy sweep every 4 hours
    '*/15 * * * *'                -- Light regime every 15 min
  ),
  (
    'QQQ',
    'stocks',
    'yahoo',
    'QQQ',
    NULL,
    true,
    900,
    '10 2,6,10,14,18,22 * * *',  -- Heavy sweep every 4 hours (offset 10 min)
    '*/15 * * * *'                -- Light regime every 15 min
  )
ON CONFLICT (symbol) DO UPDATE SET
  active = EXCLUDED.active,
  data_source = EXCLUDED.data_source,
  yahoo_ticker = EXCLUDED.yahoo_ticker,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  sweep_cron = EXCLUDED.sweep_cron,
  regime_cron = EXCLUDED.regime_cron;

-- ─── Create pg_cron jobs for SPY ───────────────────────────

SELECT cron.schedule(
  'heavy-sweep-SPY',
  '0 2,6,10,14,18,22 * * *',
  $$
  SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/heavy-sweep',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'
    ),
    body := jsonb_build_object('symbol', 'SPY')
  );
  $$
);

SELECT cron.schedule(
  'light-regime-SPY',
  '*/15 * * * *',
  $$
  SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'
    ),
    body := jsonb_build_object('symbol', 'SPY')
  );
  $$
);

-- ─── Create pg_cron jobs for QQQ ───────────────────────────

SELECT cron.schedule(
  'heavy-sweep-QQQ',
  '10 2,6,10,14,18,22 * * *',
  $$
  SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/heavy-sweep',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'
    ),
    body := jsonb_build_object('symbol', 'QQQ')
  );
  $$
);

SELECT cron.schedule(
  'light-regime-QQQ',
  '*/15 * * * *',
  $$
  SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'
    ),
    body := jsonb_build_object('symbol', 'QQQ')
  );
  $$
);

-- ─── Initialize Redis config entries ───────────────────────
-- Note: These can also be initialized by running a heavy-sweep manually:
--   curl -X POST <SUPABASE_URL>/functions/v1/heavy-sweep \
--     -H "Authorization: Bearer <SERVICE_ROLE>" \
--     -H "Content-Type: application/json" \
--     -d '{"symbol":"SPY"}'
--
-- The edge function will create the Redis hash automatically.
