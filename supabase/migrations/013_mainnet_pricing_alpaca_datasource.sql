-- Migration 013: Switch to HL Mainnet + Add Alpaca Data Source
-- ==============================================================================
-- PROBLEM: The edge function was hitting HyperLiquid TESTNET, getting fake prices
--          and 401 errors for coins (LINK, XRP) that don't exist on testnet.
--          Yahoo Finance scraping was unreliable (401s, UA rotation) for all
--          non-HL symbols.
--
-- CHANGES:
--   1. Switch LINK/XRP back to data_source='hyperliquid' (they exist on mainnet)
--   2. Add 'alpaca' as a data_source option for stocks/ETFs/crypto spot/commodities
--   3. Add alpaca_symbol column to sim_symbols
--   4. Update 14 Alpaca-venue symbols to use data_source='alpaca'
--   5. Forex pairs stay on data_source='yahoo' (not available on HL or Alpaca bars API)
--
-- DATA SOURCE ROUTING (after this migration):
--   "hyperliquid" → HL mainnet candleSnapshot (9 crypto perps)
--   "alpaca"      → Alpaca Market Data API (14 symbols: stocks, ETFs, crypto spot, commodities)
--   "yahoo"       → Yahoo Finance scraping (5 forex pairs — only option available)

-- ─── Step 1: Add alpaca_symbol column ──────────────────────
ALTER TABLE sim_symbols
ADD COLUMN IF NOT EXISTS alpaca_symbol text;

-- ─── Step 2: Switch LINK/XRP back to HyperLiquid (mainnet) ──
UPDATE sim_symbols
SET
  data_source = 'hyperliquid',
  yahoo_ticker = NULL,
  updated_at = now()
WHERE symbol IN ('LINK', 'XRP');

-- ─── Step 3: Update Alpaca-venue symbols to data_source='alpaca' ──
-- Crypto spot (Alpaca)
UPDATE sim_symbols
SET
  data_source = 'alpaca',
  alpaca_symbol = 'BTC/USD',
  updated_at = now()
WHERE symbol = 'BTCUSD';

UPDATE sim_symbols
SET
  data_source = 'alpaca',
  alpaca_symbol = 'ETH/USD',
  updated_at = now()
WHERE symbol = 'ETHUSD';

-- Equities (Alpaca)
UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'AAPL', updated_at = now()
WHERE symbol = 'AAPL';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'MSFT', updated_at = now()
WHERE symbol = 'MSFT';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'GOOGL', updated_at = now()
WHERE symbol = 'GOOGL';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'AMZN', updated_at = now()
WHERE symbol = 'AMZN';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'NVDA', updated_at = now()
WHERE symbol = 'NVDA';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'TSLA', updated_at = now()
WHERE symbol = 'TSLA';

-- ETFs (Alpaca)
UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'SPY', updated_at = now()
WHERE symbol = 'SPY';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'QQQ', updated_at = now()
WHERE symbol = 'QQQ';

-- Commodities ETFs (Alpaca — these trade as ETFs on exchanges)
UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'GLD', updated_at = now()
WHERE symbol = 'GOLD';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'SLV', updated_at = now()
WHERE symbol = 'SILVER';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'USO', updated_at = now()
WHERE symbol = 'OIL';

UPDATE sim_symbols
SET data_source = 'alpaca', alpaca_symbol = 'CPER', updated_at = now()
WHERE symbol = 'COPPER';

-- ─── Step 4: Verify forex stays on Yahoo ──────────────────
-- EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD → data_source='yahoo' (no change needed)
-- These forex pairs are not available on HL mainnet or Alpaca bars API

-- ─── Step 5: Summary ───────────────────────────────────────
-- Data source routing after migration:
--   HL Mainnet (9):  BTC, ETH, SOL, AVAX, ARB, DOGE, LINK, XRP, WIF
--   Alpaca Data (14): BTCUSD, ETHUSD, AAPL, MSFT, GOOGL, AMZN, NVDA, TSLA,
--                      SPY, QQQ, GOLD, SILVER, OIL, COPPER
--   Yahoo Finance (5): EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD
--
-- Edge function code changes (in _shared/utils.ts):
--   - fetchHyperLiquidCandles: testnet → mainnet URL
--   - fetchAlpacaCandles: NEW — Alpaca Market Data API
--   - fetchSweepCandles/fetchLatestCandles: routes "alpaca" data_source
--   - HL_PERP_SYMBOLS: added LINK, XRP back
--
-- Supabase secrets needed:
--   ALPACA_API_KEY, ALPACA_SECRET_KEY (falls back to Yahoo if not set)
