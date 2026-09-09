-- 2026-09-09: fixed nominal lot-size P/L convention (0.01 lot / micro-lot),
-- replacing the open "which P/L convention" question in
-- pnl_methodology_three_conventions with a concrete, built answer.
--
-- pnl_notional_1unit (migration 138/146) is P/L from holding a notional
-- 1 unit of the underlying, with NO position-sizing at all -- comparable
-- within a symbol over time, NOT across symbols (see that migration's
-- header for the full derivation). This migration adds pnl_001_lot: the
-- SAME underlying pnl_bricks, scaled by a real-world 0.01-lot (micro-lot)
-- contract size, so the dollar figures reflect what a realistic minimum
-- retail position would actually have made.
--
-- The multiplier is NOT uniform across symbols, and the direction isn't
-- even consistent -- forex/silver need SCALING UP (0.01 lot is thousands
-- of "1 units"), while indices/crypto need SCALING DOWN (a raw "1 unit"
-- of an index point or 1 BTC is already far larger than 0.01 lot). Each
-- symbol's multiplier is derived from what "1 unit" already means in the
-- CURRENT pnl_bricks*brick_size formula for that quoting convention:
--
--   Forex (AUDUSD/EURUSD/GBPUSD/USDCAD/USDCHF/USDJPY): brick_size is
--     quoted per 1 unit of base currency. 1.0 lot = 100,000 units
--     (universal FX industry standard) -> 0.01 lot = 1,000 units ->
--     multiplier = 1000.
--   Gold (XAUUSD/XAUEUR/XAUAUD): brick_size is quoted per 1 troy oz.
--     OANDA's own published gold CFD contract size is 1.0 lot = 100 oz
--     -> 0.01 lot = 1 oz -> multiplier = 1 (no change -- "1 unit"
--     already equals 0.01 lot for this specific instrument).
--   Silver (XAGUSD): brick_size per 1 troy oz. OANDA's published silver
--     CFD contract size is 1.0 lot = 5,000 oz -> 0.01 lot = 50 oz ->
--     multiplier = 50.
--   Indices (US30USD via OANDA, US500 via PEPPERSTONE): brick_size is
--     quoted in raw index points -- there is no per-point dollar
--     multiplier anywhere in the current formula, so "1 unit" already
--     IS the common retail default of $1/point (matching a 1.0-lot
--     contract). 0.01 lot = $0.01/point -> multiplier = 0.01.
--     LOW-MEDIUM CONFIDENCE: index CFD contract sizing genuinely varies
--     by broker; this is a defensible common default, not each broker's
--     verified live spec sheet.
--   Stock (AAPL): brick_size per 1 share. Common single-stock CFD
--     convention: 1.0 lot = 100 shares -> 0.01 lot = 1 share ->
--     multiplier = 1. MEDIUM CONFIDENCE: common but not universal.
--   Crypto (BTCUSD): brick_size per 1 BTC. Common CFD-style convention:
--     1.0 lot = 1 BTC -> 0.01 lot = 0.01 BTC -> multiplier = 0.01.
--     MEDIUM CONFIDENCE: BTCUSD's actual data source (Coinbase, a spot
--     exchange) has no native "lot" concept at all -- this overlays a
--     CFD-style convention for consistency with the other symbols.
--
-- A real per-symbol table (not a CASE expression buried in a view) so
-- adding a new symbol later means adding one row with an explicit,
-- reviewable multiplier -- not silently falling back to a guessed
-- default.

CREATE TABLE IF NOT EXISTS nt_symbol_lot_multiplier (
  symbol               TEXT PRIMARY KEY,
  asset_class_label    TEXT NOT NULL,
  lot_unit_description TEXT NOT NULL,
  multiplier           NUMERIC NOT NULL,
  confidence           TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low-medium')),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_symbol_lot_multiplier IS
  'Per-symbol multiplier converting pnl_notional_1unit (1-unit notional P&L) into pnl_001_lot (fixed nominal 0.01-lot P&L) -- see migration 148 header for the full derivation and confidence level per symbol. Not a CASE expression in the view on purpose: a real table means adding a symbol later requires an explicit, reviewable multiplier, not a silent fallback.';

INSERT INTO nt_symbol_lot_multiplier (symbol, asset_class_label, lot_unit_description, multiplier, confidence) VALUES
  ('AUDUSD',  'forex',   '0.01 lot = 1,000 units of base currency',        1000,  'high'),
  ('EURUSD',  'forex',   '0.01 lot = 1,000 units of base currency',        1000,  'high'),
  ('GBPUSD',  'forex',   '0.01 lot = 1,000 units of base currency',        1000,  'high'),
  ('USDCAD',  'forex',   '0.01 lot = 1,000 units of base currency',        1000,  'high'),
  ('USDCHF',  'forex',   '0.01 lot = 1,000 units of base currency',        1000,  'high'),
  ('USDJPY',  'forex',   '0.01 lot = 1,000 units of base currency',        1000,  'high'),
  ('XAUUSD',  'gold',    '0.01 lot = 1 troy oz (OANDA: 1.0 lot = 100 oz)', 1,     'high'),
  ('XAUEUR',  'gold',    '0.01 lot = 1 troy oz (OANDA: 1.0 lot = 100 oz)', 1,     'high'),
  ('XAUAUD',  'gold',    '0.01 lot = 1 troy oz (OANDA: 1.0 lot = 100 oz)', 1,     'high'),
  ('XAGUSD',  'silver',  '0.01 lot = 50 troy oz (OANDA: 1.0 lot = 5,000 oz)', 50, 'high'),
  ('US30USD', 'index',   '0.01 lot = $0.01/point (assumed $1/point at 1.0 lot)', 0.01, 'low-medium'),
  ('US500',   'index',   '0.01 lot = $0.01/point (assumed $1/point at 1.0 lot)', 0.01, 'low-medium'),
  ('AAPL',    'stock',   '0.01 lot = 1 share (1.0 lot = 100 shares)',      1,     'medium'),
  ('BTCUSD',  'crypto',  '0.01 lot = 0.01 BTC (1.0 lot = 1 BTC)',          0.01,  'medium')
ON CONFLICT (symbol) DO UPDATE SET
  asset_class_label = EXCLUDED.asset_class_label,
  lot_unit_description = EXCLUDED.lot_unit_description,
  multiplier = EXCLUDED.multiplier,
  confidence = EXCLUDED.confidence,
  updated_at = now();

ALTER TABLE nt_symbol_lot_multiplier ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_symbol_lot_multiplier;
CREATE POLICY "Service role full access" ON nt_symbol_lot_multiplier
  FOR ALL TO service_role USING (true) WITH CHECK (true);
GRANT SELECT ON nt_symbol_lot_multiplier TO anon, authenticated;
