-- ═══════════════════════════════════════════════════════════════════════════════
-- 036: Add TA indicator columns to nt_sweep_result + nt_regime_log
--
-- Background
-- ─────────
-- The multivariate HMM (TALARIA-HMM) consumes 5 TA indicators per symbol
-- per sweep tick — ADX, AO, BB Power, MACD, RSI — fetched concurrently
-- from TVDA /api/ta/{symbol}/indicators (see
-- regime_platform/services/hmm/ta_fetcher.py). These values feed the HMM's
-- 9-feature observation vector (features 4-8) and are the basis of the
-- p_markov source in the 4-source EV logit pool.
--
-- Until now the TA values were only stored in the in-memory FeatureBuffer
-- (regime_platform/services/hmm/feature_buffer.py) — they were never
-- persisted. This migration persists them per sweep tick so they are
-- queryable for:
--   • post-hoc HMM diagnostics (which TA regime produced which signal?)
--   • calibration analysis (TA state vs realized P_win)
--   • dashboard rendering (per-symbol TA strip)
--   • agent-side re-blending if p_pattern is unavailable
--
-- Column naming convention: ta_<indicator> (snake-case, ta_ prefix avoids
-- collision with future generic indicator columns). All columns are
-- nullable — the TA fetch is best-effort and may return 0.0 or be skipped
-- entirely on TVDA rate-limit (HTTP 429) or fetch failure.
--
-- Both nt_sweep_result (the fat per-sweep KPI table) and nt_regime_log
-- (the skinny per-sweep regime-transition table) get the 5 columns. This
-- duplicates the data but lets either table be queried standalone without
-- a JOIN on (symbol, created_at) — which matters because nt_regime_log is
-- the smaller, faster table for regime-shift analytics.
--
-- Numbering note: 035 is reserved for the future 035_drop_nt_pattern_stats.sql
-- referenced in migration 034. This migration uses 036 to avoid collision.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. nt_sweep_result: add 5 TA indicator columns ───────────────────────
ALTER TABLE nt_sweep_result
  ADD COLUMN IF NOT EXISTS ta_adx      DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_ao       DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_bb_power DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_macd     DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_rsi      DOUBLE PRECISION;

COMMENT ON COLUMN nt_sweep_result.ta_adx IS
  'Average Directional Index (TA) at sweep tick. Source: TVDA /api/ta/{symbol}/indicators via HMM ta_fetcher. Nullable — fetch is best-effort.';
COMMENT ON COLUMN nt_sweep_result.ta_ao IS
  'Awesome Oscillator (TA) at sweep tick. Source: TVDA /api/ta/{symbol}/indicators via HMM ta_fetcher. Nullable — fetch is best-effort.';
COMMENT ON COLUMN nt_sweep_result.ta_bb_power IS
  'Bollinger Band Power (TA) at sweep tick. Source: TVDA /api/ta/{symbol}/indicators via HMM ta_fetcher. Nullable — fetch is best-effort.';
COMMENT ON COLUMN nt_sweep_result.ta_macd IS
  'MACD line (TA) at sweep tick. Source: TVDA /api/ta/{symbol}/indicators via HMM ta_fetcher. Nullable — fetch is best-effort.';
COMMENT ON COLUMN nt_sweep_result.ta_rsi IS
  'Relative Strength Index (TA) at sweep tick. Source: TVDA /api/ta/{symbol}/indicators via HMM ta_fetcher. Nullable — fetch is best-effort.';

-- ─── 2. nt_regime_log: add the same 5 TA indicator columns ───────────────
ALTER TABLE nt_regime_log
  ADD COLUMN IF NOT EXISTS ta_adx      DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_ao       DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_bb_power DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_macd     DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS ta_rsi      DOUBLE PRECISION;

COMMENT ON COLUMN nt_regime_log.ta_adx IS
  'Average Directional Index (TA) at regime-log tick. Mirrors nt_sweep_result.ta_adx for join-free regime analytics. Nullable.';
COMMENT ON COLUMN nt_regime_log.ta_ao IS
  'Awesome Oscillator (TA) at regime-log tick. Mirrors nt_sweep_result.ta_ao. Nullable.';
COMMENT ON COLUMN nt_regime_log.ta_bb_power IS
  'Bollinger Band Power (TA) at regime-log tick. Mirrors nt_sweep_result.ta_bb_power. Nullable.';
COMMENT ON COLUMN nt_regime_log.ta_macd IS
  'MACD line (TA) at regime-log tick. Mirrors nt_sweep_result.ta_macd. Nullable.';
COMMENT ON COLUMN nt_regime_log.ta_rsi IS
  'Relative Strength Index (TA) at regime-log tick. Mirrors nt_sweep_result.ta_rsi. Nullable.';

-- ─── 3. Indexes (only on nt_sweep_result — nt_regime_log is small) ───────
-- Composite index on (symbol, ta_rsi) supports the most common query:
-- "show me sweeps for symbol X where RSI was oversold". The partial WHERE
-- clause keeps the index small (only rows where ta_rsi is non-null).
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_symbol_rsi
  ON nt_sweep_result(symbol, ta_rsi)
  WHERE ta_rsi IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_symbol_adx
  ON nt_sweep_result(symbol, ta_adx)
  WHERE ta_adx IS NOT NULL;

-- ─── 4. Verify ────────────────────────────────────────────────────────────
SELECT
  table_name,
  column_name,
  data_type,
  is_nullable
FROM information_schema.columns
WHERE table_name IN ('nt_sweep_result', 'nt_regime_log')
  AND column_name LIKE 'ta\_%' ESCAPE '\'
ORDER BY table_name, column_name;
