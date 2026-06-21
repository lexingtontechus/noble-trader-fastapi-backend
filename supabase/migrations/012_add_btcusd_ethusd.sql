-- Migration: Add BTCUSD & ETHUSD (Alpaca crypto spot) to the Intelligence system
-- These are BTC/USD and ETH/USD spot pairs traded on Alpaca (not HyperLiquid perps)
-- Asset class: crypto, data source: yahoo, venue: alpaca
--
-- Note: BTC and ETH already exist as HyperLiquid perps. BTCUSD/ETHUSD are SEPARATE
-- instruments on a different venue with different tick sizes, hours, and execution.

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
    'BTCUSD',
    'crypto',
    'yahoo',
    'BTC-USD',    -- Yahoo Finance ticker for BTC/USD spot
    NULL,
    true,
    900,          -- 15 min heartbeat TTL
    '20 2,6,10,14,18,22 * * *',  -- Heavy sweep every 4 hours (offset 20 min)
    '*/15 * * * *'                -- Light regime every 15 min
  ),
  (
    'ETHUSD',
    'crypto',
    'yahoo',
    'ETH-USD',    -- Yahoo Finance ticker for ETH/USD spot
    NULL,
    true,
    900,
    '25 2,6,10,14,18,22 * * *',  -- Heavy sweep every 4 hours (offset 25 min)
    '*/15 * * * *'                -- Light regime every 15 min
  )
ON CONFLICT (symbol) DO UPDATE SET
  active = EXCLUDED.active,
  data_source = EXCLUDED.data_source,
  yahoo_ticker = EXCLUDED.yahoo_ticker,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  sweep_cron = EXCLUDED.sweep_cron,
  regime_cron = EXCLUDED.regime_cron;

-- ─── Create pg_cron jobs for BTCUSD ────────────────────────

SELECT cron.schedule(
  'heavy-sweep-BTCUSD',
  '20 2,6,10,14,18,22 * * *',
  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'BTCUSD'); $$
);

SELECT cron.schedule(
  'light-regime-BTCUSD',
  '*/15 * * * *',
  $$ SELECT sim_invoke_edge_function('light-regime', 'BTCUSD'); $$
);

-- ─── Create pg_cron jobs for ETHUSD ────────────────────────

SELECT cron.schedule(
  'heavy-sweep-ETHUSD',
  '25 2,6,10,14,18,22 * * *',
  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'ETHUSD'); $$
);

SELECT cron.schedule(
  'light-regime-ETHUSD',
  '*/15 * * * *',
  $$ SELECT sim_invoke_edge_function('light-regime', 'ETHUSD'); $$
);

-- ─── Verify ────────────────────────────────────────────────
-- The edge function will create Redis config entries automatically on first run.
-- Trigger manually to test:
--   SELECT sim_invoke_edge_function('heavy-sweep', 'BTCUSD');
--   SELECT sim_invoke_edge_function('heavy-sweep', 'ETHUSD');
