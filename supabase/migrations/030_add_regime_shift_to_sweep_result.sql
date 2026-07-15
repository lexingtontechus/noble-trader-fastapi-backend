-- ═══════════════════════════════════════════════════════════════════════════════
-- 030: Add regime_shift, prev_regime, size_mult to nt_sweep_result
--
-- These fields were in the Redis agent_payload but missing from the
-- nt_sweep_result table. They provide critical context:
--   regime_shift  — whether the regime changed from the previous sweep
--   prev_regime   — the previous regime label (empty if first sweep)
--   size_mult     — TimesFM confidence-based position size multiplier (0.6/1.0/1.2)
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_sweep_result
  ADD COLUMN IF NOT EXISTS regime_shift BOOLEAN DEFAULT false,
  ADD COLUMN IF NOT EXISTS prev_regime TEXT DEFAULT '',
  ADD COLUMN IF NOT EXISTS size_mult DOUBLE PRECISION DEFAULT 1.0;

-- Index for filtering regime shifts (useful for analysis)
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_regime_shift
  ON nt_sweep_result(regime_shift)
  WHERE regime_shift = true;

-- Verify
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'nt_sweep_result'
  AND column_name IN ('regime_shift', 'prev_regime', 'size_mult')
ORDER BY ordinal_position;
