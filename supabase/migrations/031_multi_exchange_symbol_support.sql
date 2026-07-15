-- ═══════════════════════════════════════════════════════════════════════════════
-- 031: Drop UNIQUE(symbol), add exchange column, migrate FKs to composite key
--
-- Allows multiple rows with the same symbol on different exchanges:
--   BTCUSD → COINBASE:BTCUSD (tradingview, active)
--   BTCUSD → BINANCE:BTCUSDT (tradingview, inactive)
--   BTCUSD → BTC/USD (alpaca, inactive)
--
-- Changes:
--   1. Add `exchange` column to nt_symbol (derived from tradingview_symbol prefix
--      or data_source for HL/Alpaca/Yahoo)
--   2. Drop UNIQUE(symbol) constraint
--   3. Add UNIQUE(symbol, exchange) composite constraint
--   4. Migrate FKs in nt_sweep_result, nt_regime_log, nt_calibration_records
--      from symbol-only to (symbol, exchange) — but since Postgres FKs to
--      composite keys require both columns, and the child tables don't have
--      an exchange column, we instead DROP the FK constraints entirely and
--      add `venue` column to each child table for referential integrity at
--      the application level.
--   5. Add `venue` column to nt_sweep_result, nt_regime_log, nt_calibration_records
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Add exchange column to nt_symbol ─────────────────────────────────
ALTER TABLE nt_symbol ADD COLUMN IF NOT EXISTS exchange TEXT;

-- Populate exchange from existing data
UPDATE nt_symbol SET exchange = split_part(tradingview_symbol, ':', 1)
  WHERE tradingview_symbol IS NOT NULL AND tradingview_symbol LIKE '%:%';

UPDATE nt_symbol SET exchange = 'HYPERLIQUID'
  WHERE hyperliquid_coin IS NOT NULL AND exchange IS NULL;

UPDATE nt_symbol SET exchange = 'ALPACA'
  WHERE alpaca_symbol IS NOT NULL AND exchange IS NULL;

UPDATE nt_symbol SET exchange = 'YAHOO'
  WHERE yahoo_ticker IS NOT NULL AND exchange IS NULL;

-- Default for any remaining NULLs
UPDATE nt_symbol SET exchange = 'UNKNOWN' WHERE exchange IS NULL;

-- ─── 2. Drop old UNIQUE(symbol) constraint ───────────────────────────────
ALTER TABLE nt_symbol DROP CONSTRAINT IF EXISTS sim_symbols_symbol_key;

-- ─── 3. Add new UNIQUE(symbol, exchange) composite constraint ────────────
ALTER TABLE nt_symbol
  ADD CONSTRAINT nt_symbol_symbol_exchange_key
  UNIQUE (symbol, exchange);

-- ─── 4. Migrate FK constraints on child tables ──────────────────────────
-- Postgres FKs require referencing a UNIQUE or PRIMARY KEY. Since we changed
-- from UNIQUE(symbol) to UNIQUE(symbol, exchange), any existing FK that
-- references symbol alone would be invalid. We need to:
--   a) Drop the old FK constraints
--   b) Add a `venue` column to each child table (stores the exchange name)
--   c) The application code enforces referential integrity by writing
--      (symbol, venue) pairs that exist in nt_symbol

-- ── nt_sweep_result (was sim_sweep_results) ──
ALTER TABLE nt_sweep_result DROP CONSTRAINT IF EXISTS sim_sweep_results_symbol_fkey;
ALTER TABLE nt_sweep_result DROP CONSTRAINT IF EXISTS nt_sweep_result_symbol_fkey;
ALTER TABLE nt_sweep_result ADD COLUMN IF NOT EXISTS venue TEXT;

-- ── nt_regime_log (was sim_regime_log) ──
ALTER TABLE nt_regime_log DROP CONSTRAINT IF EXISTS sim_regime_log_symbol_fkey;
ALTER TABLE nt_regime_log DROP CONSTRAINT IF EXISTS nt_regime_log_symbol_fkey;
ALTER TABLE nt_regime_log ADD COLUMN IF NOT EXISTS venue TEXT;

-- ── nt_calibration_records ──
ALTER TABLE nt_calibration_records DROP CONSTRAINT IF EXISTS nt_calibration_records_symbol_fkey;
ALTER TABLE nt_calibration_records ADD COLUMN IF NOT EXISTS venue TEXT;

-- ─── 5. Backfill venue in child tables from nt_symbol ───────────────────
-- For existing rows, set venue from the matching nt_symbol row
UPDATE nt_sweep_result sr
  SET venue = ns.exchange
  FROM nt_symbol ns
  WHERE sr.symbol = ns.symbol
    AND sr.venue IS NULL;

UPDATE nt_regime_log rl
  SET venue = ns.exchange
  FROM nt_symbol ns
  WHERE rl.symbol = ns.symbol
    AND rl.venue IS NULL;

UPDATE nt_calibration_records cr
  SET venue = ns.exchange
  FROM nt_symbol ns
  WHERE cr.symbol = ns.symbol
    AND cr.venue IS NULL;

-- ─── 6. Add indexes for the new venue column ────────────────────────────
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_venue
  ON nt_sweep_result(venue);

CREATE INDEX IF NOT EXISTS idx_nt_regime_log_venue
  ON nt_regime_log(venue);

CREATE INDEX IF NOT EXISTS idx_nt_calibration_records_venue
  ON nt_calibration_records(venue);

-- ─── 7. Add composite index for (symbol, venue) queries ─────────────────
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_symbol_venue
  ON nt_sweep_result(symbol, venue);

CREATE INDEX IF NOT EXISTS idx_nt_regime_log_symbol_venue
  ON nt_regime_log(symbol, venue);

CREATE INDEX IF NOT EXISTS idx_nt_calibration_records_symbol_venue
  ON nt_calibration_records(symbol, venue);

-- ─── 8. Verify ──────────────────────────────────────────────────────────
-- Show nt_symbol with exchange column
SELECT id, symbol, exchange, data_source, tradingview_symbol, alpaca_symbol, hyperliquid_coin, active
FROM nt_symbol
ORDER BY symbol, exchange;

-- Show constraints on nt_symbol
SELECT conname, contype, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'nt_symbol'::regclass
ORDER BY contype;
