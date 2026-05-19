-- Phase 6: Statistical Rigor columns for ta_backtest_result
-- Adds: bootstrap_cis, deflated_sharpe_result, multiple_testing_results, significance_test_results

-- 6B: Bootstrap confidence intervals (JSONB — contains CIs for Sharpe, win_rate, etc.)
ALTER TABLE ta_backtest_result
ADD COLUMN IF NOT EXISTS bootstrap_cis JSONB DEFAULT '{}';

-- 6A: Deflated Sharpe Ratio result (JSONB — DSR probability, raw Sharpe, threshold, etc.)
ALTER TABLE ta_backtest_result
ADD COLUMN IF NOT EXISTS deflated_sharpe_result JSONB DEFAULT '{}';

-- 6C: Multiple testing correction results (JSONB — Bonferroni, Holm-Bonferroni, BH FDR)
ALTER TABLE ta_backtest_result
ADD COLUMN IF NOT EXISTS multiple_testing_results JSONB DEFAULT '{}';

-- 6D: Strategy significance test results (JSONB — White's Reality Check, Hansen's SPA)
ALTER TABLE ta_backtest_result
ADD COLUMN IF NOT EXISTS significance_test_results JSONB DEFAULT '{}';

-- Add comments for documentation
COMMENT ON COLUMN ta_backtest_result.bootstrap_cis IS 'Phase 6B: Bootstrap confidence intervals for key metrics (Sharpe, win rate, profit factor, max drawdown, mean return)';
COMMENT ON COLUMN ta_backtest_result.deflated_sharpe_result IS 'Phase 6A: Deflated Sharpe Ratio result — DSR probability, raw Sharpe, n_trials, threshold, interpretation';
COMMENT ON COLUMN ta_backtest_result.multiple_testing_results IS 'Phase 6C: Multiple testing correction results — Bonferroni, Holm-Bonferroni, Benjamini-Hochberg FDR';
COMMENT ON COLUMN ta_backtest_result.significance_test_results IS 'Phase 6D: Strategy significance test results — White Reality Check, Hansen SPA';
