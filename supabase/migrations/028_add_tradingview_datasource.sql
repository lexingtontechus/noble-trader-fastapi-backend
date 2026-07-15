-- ═══════════════════════════════════════════════════════════════════════════════
-- 028: Add TradingView (RapidAPI) as a new data source
--
-- This migration:
--   1. Adds the `tradingview_symbol` column (stores EXCHANGE:SYMBOL format)
--   2. Updates the `data_source` CHECK constraint to allow 'tradingview'
--   3. Switches BTCUSD from Alpaca → TradingView (UPDATE existing row)
--   4. Adds 3 new symbols: USDJPY, EURUSD, XAUUSD
--
-- TradingView API (via RapidAPI) provides:
--   - Crypto (COINBASE:BTCUSD)
--   - Forex (OANDA:USDJPY, OANDA:EURUSD)
--   - Commodities (OANDA:XAUUSD — gold spot)
--
-- Symbols use TradingView's EXCHANGE:SYMBOL format in `tradingview_symbol`:
--   COINBASE:BTCUSD   — Coinbase BTC/USD spot
--   OANDA:USDJPY      — OANDA USD/JPY forex
--   OANDA:EURUSD      — OANDA EUR/USD forex
--   OANDA:XAUUSD      — OANDA gold spot
--
-- Cron schedule (TimesFM replaces the 4-hour heavy sweep):
--   regime_cron = '*/5 * * * *'           — light sweep every 5 minutes
--   sweep_cron  = '0 20 * * 0'            — heavy sweep at 20:00 UTC on Sunday
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Add tradingview_symbol column ─────────────────────────────────────
-- Stores the TradingView symbol in EXCHANGE:SYMBOL format (e.g., "COINBASE:BTCUSD")
ALTER TABLE nt_symbol
  ADD COLUMN IF NOT EXISTS tradingview_symbol TEXT;

COMMENT ON COLUMN nt_symbol.tradingview_symbol
  IS 'TradingView symbol in EXCHANGE:SYMBOL format (e.g., COINBASE:BTCUSD, OANDA:EURUSD)';

-- ─── 2. Update data_source CHECK constraint ───────────────────────────────
-- Drop the old constraint and add a new one that includes 'tradingview'
ALTER TABLE nt_symbol
  DROP CONSTRAINT IF EXISTS sim_symbols_data_source_check;

ALTER TABLE nt_symbol
  ADD CONSTRAINT sim_symbols_data_source_check
  CHECK (data_source = ANY (ARRAY[
    'hyperliquid'::text,
    'alpaca'::text,
    'yahoo'::text,
    'tradingview'::text
  ]));

-- ─── 3. Switch BTCUSD from Alpaca → TradingView ───────────────────────────
-- BTCUSD already exists (id=55, Alpaca, active=false). Update it in-place
-- to use TradingView with Coinbase as the exchange.
UPDATE nt_symbol SET
  data_source       = 'tradingview',
  tradingview_symbol = 'COINBASE:BTCUSD',
  alpaca_symbol     = NULL,
  yahoo_ticker      = NULL,
  regime_cron       = '*/5 * * * *',
  sweep_cron        = '0 20 * * 0',
  heartbeat_ttl_seconds = 900,
  active            = true,
  updated_at        = now()
WHERE symbol = 'BTCUSD';

-- ─── 4. Add new symbols ───────────────────────────────────────────────────
-- Forex pairs and gold spot, all via OANDA on TradingView

-- USD/JPY forex
INSERT INTO nt_symbol (
  symbol, asset_class, data_source, tradingview_symbol,
  regime_cron, sweep_cron, active, heartbeat_ttl_seconds
) VALUES (
  'USDJPY', 'forex', 'tradingview', 'OANDA:USDJPY',
  '*/5 * * * *', '0 20 * * 0', true, 900
)
ON CONFLICT (symbol) DO UPDATE SET
  data_source       = EXCLUDED.data_source,
  tradingview_symbol = EXCLUDED.tradingview_symbol,
  regime_cron       = EXCLUDED.regime_cron,
  sweep_cron        = EXCLUDED.sweep_cron,
  active            = EXCLUDED.active,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  updated_at        = now();

-- EUR/USD forex
INSERT INTO nt_symbol (
  symbol, asset_class, data_source, tradingview_symbol,
  regime_cron, sweep_cron, active, heartbeat_ttl_seconds
) VALUES (
  'EURUSD', 'forex', 'tradingview', 'OANDA:EURUSD',
  '*/5 * * * *', '0 20 * * 0', true, 900
)
ON CONFLICT (symbol) DO UPDATE SET
  data_source       = EXCLUDED.data_source,
  tradingview_symbol = EXCLUDED.tradingview_symbol,
  regime_cron       = EXCLUDED.regime_cron,
  sweep_cron        = EXCLUDED.sweep_cron,
  active            = EXCLUDED.active,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  updated_at        = now();

-- XAU/USD gold spot (commodities)
INSERT INTO nt_symbol (
  symbol, asset_class, data_source, tradingview_symbol,
  regime_cron, sweep_cron, active, heartbeat_ttl_seconds
) VALUES (
  'XAUUSD', 'commodities', 'tradingview', 'OANDA:XAUUSD',
  '*/5 * * * *', '0 20 * * 0', true, 900
)
ON CONFLICT (symbol) DO UPDATE SET
  data_source       = EXCLUDED.data_source,
  tradingview_symbol = EXCLUDED.tradingview_symbol,
  regime_cron       = EXCLUDED.regime_cron,
  sweep_cron        = EXCLUDED.sweep_cron,
  active            = EXCLUDED.active,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  updated_at        = now();

-- ─── 5. Verify ────────────────────────────────────────────────────────────
-- Show the TradingView symbols after migration
SELECT id, symbol, asset_class, data_source, tradingview_symbol, active
FROM nt_symbol
WHERE data_source = 'tradingview'
ORDER BY symbol;
