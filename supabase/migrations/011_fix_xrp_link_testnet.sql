-- Migration 011: Fix XRP and LINK for HyperLiquid testnet compatibility
-- ==============================================================================
-- PROBLEM: XRP is missing from sim_symbols entirely. LINK is configured with
--          data_source=hyperliquid but the HyperLiquid testnet does not have
--          LINK or XRP coins. Both heavy-sweep and light-regime fail for these
--          symbols (500 errors on candle fetch, market_closed fallback).
--
-- SOLUTION:
--   1. Add XRP to sim_symbols with data_source=yahoo (yahoo_ticker='XRP-USD')
--   2. Update LINK to data_source=yahoo (yahoo_ticker='LINK-USD')
--   3. Schedule cron jobs for XRP (light-regime + heavy-sweep)
--   4. Add XRP to the crypto batch cron jobs
--
-- NOTE: When switching back to mainnet, these can be reverted to
--       data_source=hyperliquid since both XRP and LINK exist on mainnet.

-- ─── Step 1: Add XRP to sim_symbols ────────────────────────
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
) VALUES (
  'XRP',
  'crypto',
  'yahoo',           -- HL testnet doesn't have XRP
  'XRP-USD',         -- Yahoo Finance crypto pair
  'XRP',             -- Keep for future mainnet switch
  true,
  900,
  '35 2,6,10,14,18,22 * * *',    -- Heavy sweep every 4 hours (offset 35)
  '2,17,32,47 * * * *'           -- Light regime every 15 min (crypto-3 batch)
)
ON CONFLICT (symbol) DO UPDATE SET
  data_source = EXCLUDED.data_source,
  yahoo_ticker = EXCLUDED.yahoo_ticker,
  hyperliquid_coin = EXCLUDED.hyperliquid_coin,
  active = EXCLUDED.active,
  sweep_cron = EXCLUDED.sweep_cron,
  regime_cron = EXCLUDED.regime_cron,
  updated_at = now();

-- ─── Step 2: Update LINK to use Yahoo Finance ──────────────
UPDATE sim_symbols
SET
  data_source = 'yahoo',
  yahoo_ticker = 'LINK-USD',
  updated_at = now()
WHERE symbol = 'LINK';

-- ─── Step 3: Update PRICE_TICK_DECIMALS in edge function shared utils ────
-- XRP on testnet via Yahoo: 4 decimals is fine (matches crypto default)
-- LINK via Yahoo: 4 decimals is fine
-- (This is a code change, not SQL — see utils.ts PRICE_TICK_DECIMALS)

-- ─── Step 4: Schedule heavy-sweep cron job for XRP ──────────
-- XRP gets the same schedule as the old XRP slot: 35 min offset

SELECT cron.schedule(
  'heavy-sweep-XRP',
  '35 2,6,10,14,18,22 * * *',
  $$ SELECT sim_invoke_edge_function('heavy-sweep', 'XRP'); $$
);

-- ─── Step 5: Reschedule crypto-3 batch to include XRP ───────
-- Current crypto-3 batch: ['LINK','XRP','WIF']
-- XRP is now in sim_symbols, so the batch will find it

-- ─── Step 6: Summary ────────────────────────────────────────
-- XRP: Added to sim_symbols with yahoo data source
-- LINK: Switched from hyperliquid to yahoo data source
-- Both will now fetch candles from Yahoo Finance instead of HL testnet
-- When switching back to mainnet, revert:
--   UPDATE sim_symbols SET data_source='hyperliquid', yahoo_ticker=NULL WHERE symbol IN ('XRP','LINK');
