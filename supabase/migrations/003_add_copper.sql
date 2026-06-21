-- Migration: Add Copper (HG=F) to the Intelligence system
-- Asset class: commodities, data source: yahoo

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
    'COPPER',
    'commodities',
    'yahoo',
    'HG=F',
    NULL,
    true,
    900,          -- 15 min heartbeat TTL
    '0 2,6,10,14,18,22 * * 1-5',  -- Heavy sweep every 4 hours (weekdays)
    '*/15 * * * 1-5'                -- Light regime every 15 min (weekdays)
  )
ON CONFLICT (symbol) DO UPDATE SET
  active = EXCLUDED.active,
  data_source = EXCLUDED.data_source,
  yahoo_ticker = EXCLUDED.yahoo_ticker,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  sweep_cron = EXCLUDED.sweep_cron,
  regime_cron = EXCLUDED.regime_cron;

-- ─── Create pg_cron jobs for COPPER ────────────────────────

SELECT cron.schedule(
  'heavy-sweep-COPPER',
  '0 2,6,10,14,18,22 * * 1-5',
  $$
  SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/heavy-sweep',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'
    ),
    body := jsonb_build_object('symbol', 'COPPER')
  );
  $$
);

SELECT cron.schedule(
  'light-regime-COPPER',
  '*/15 * * * 1-5',
  $$
  SELECT net.http_post(
    url := '<SUPABASE_URL>/functions/v1/light-regime',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer <SUPABASE_SERVICE_ROLE>'
    ),
    body := jsonb_build_object('symbol', 'COPPER')
  );
  $$
);
