-- Phase 5B: Corporate Action Adjustments
-- Stores stock splits, dividends, and spinoffs so the backtest pipeline
-- can apply split/dividend-adjusted prices.

CREATE TABLE IF NOT EXISTS nt_corporate_action (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker      TEXT    NOT NULL,
    action_type TEXT    NOT NULL,        -- 'split' | 'dividend' | 'spinoff'
    ex_date     DATE    NOT NULL,        -- Ex-dividend / ex-split date
    record_date DATE,                     -- Record date (nullable, not all actions have one)
    factor      DOUBLE PRECISION NOT NULL DEFAULT 1.0,  -- Split ratio (e.g. 0.25 for 4:1 split), or dividend amount
    amount      DOUBLE PRECISION,         -- Cash amount per share (for dividends)
    description TEXT,                      -- Human-readable: "4:1 stock split", "$0.82 quarterly dividend"
    source      TEXT    NOT NULL DEFAULT 'manual',  -- 'yfinance' | 'sharadar' | 'manual'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique constraint: one action per ticker per type per ex-date
CREATE UNIQUE INDEX IF NOT EXISTS idx_corp_action_unique
    ON nt_corporate_action (ticker, action_type, ex_date);

-- Fast lookup: "what actions happened on or before date X?"
CREATE INDEX IF NOT EXISTS idx_corp_action_lookup
    ON nt_corporate_action (ticker, ex_date);

-- Row-level security
ALTER TABLE nt_corporate_action ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role can do everything" ON nt_corporate_action
    FOR ALL USING (true) WITH CHECK (true);

COMMENT ON TABLE nt_corporate_action IS
    'Corporate actions (splits, dividends, spinoffs) for price adjustment in backtests (Phase 5B)';
