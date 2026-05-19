-- Phase 5C: Data Versioning & Lineage
-- Adds data_hash, data_source, and price_adjustment columns to ta_backtest_result
-- so every backtest result records exactly which data produced it.

-- Data hash: SHA-256 of the input price data (for reproducibility checking)
ALTER TABLE ta_backtest_result
    ADD COLUMN IF NOT EXISTS data_hash TEXT;

-- Data source provenance: where the data came from
ALTER TABLE ta_backtest_result
    ADD COLUMN IF NOT EXISTS data_source JSONB DEFAULT '{}';

-- Price adjustment mode used: 'raw', 'split_adjusted', or 'fully_adjusted'
ALTER TABLE ta_backtest_result
    ADD COLUMN IF NOT EXISTS price_adjustment TEXT DEFAULT 'raw';

-- Universe mode used: 'current_constituents' or 'pit_constituents'
ALTER TABLE ta_backtest_result
    ADD COLUMN IF NOT EXISTS universe_mode TEXT DEFAULT 'current_constituents';

-- Look-ahead audit result: JSONB with warnings or "no look-ahead detected"
ALTER TABLE ta_backtest_result
    ADD COLUMN IF NOT EXISTS look_ahead_audit JSONB DEFAULT '{}';

-- Index for finding backtests by data hash (for reproducibility checks)
CREATE INDEX IF NOT EXISTS idx_backtest_data_hash
    ON ta_backtest_result (data_hash)
    WHERE data_hash IS NOT NULL;

COMMENT ON COLUMN ta_backtest_result.data_hash IS
    'SHA-256 hash of input price data for reproducibility verification (Phase 5C)';
COMMENT ON COLUMN ta_backtest_result.data_source IS
    'Provenance metadata: source, fetch_date, adjustment_level (Phase 5C)';
COMMENT ON COLUMN ta_backtest_result.price_adjustment IS
    'Price adjustment mode used: raw, split_adjusted, fully_adjusted (Phase 5B)';
COMMENT ON COLUMN ta_backtest_result.universe_mode IS
    'Universe filtering mode: current_constituents (default) or pit_constituents (Phase 5A)';
COMMENT ON COLUMN ta_backtest_result.look_ahead_audit IS
    'Look-ahead bias audit results: warnings or clean status (Phase 5D)';
