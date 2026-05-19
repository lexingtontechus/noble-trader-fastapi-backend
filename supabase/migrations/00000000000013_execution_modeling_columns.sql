-- Phase 7: Advanced Execution Modeling columns
ALTER TABLE ta_backtest_result
ADD COLUMN IF NOT EXISTS execution_modeling JSONB DEFAULT NULL;

COMMENT ON COLUMN ta_backtest_result.execution_modeling IS 'Phase 7: Execution modeling summary (market impact, fill probability, borrow/financing costs)';
