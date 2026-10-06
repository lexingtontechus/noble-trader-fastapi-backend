-- =============================================================================
-- 199: add Kairos geometry fields to nt_sweep_result
-- =============================================================================
-- Adds base_brick_size, brick_vol_scale, vol_ratio to nt_sweep_result.
-- These fields expose the ATR-scaling pipeline that produces brick_size,
-- so the frontend can display the scaling factor and base value.
--
-- No backfill: existing rows have NULL for these columns. New sweeps
-- populate all three fields going forward.
-- =============================================================================

ALTER TABLE nt_sweep_result
  ADD COLUMN IF NOT EXISTS base_brick_size FLOAT,
  ADD COLUMN IF NOT EXISTS brick_vol_scale FLOAT,
  ADD COLUMN IF NOT EXISTS vol_ratio FLOAT;

COMMENT ON COLUMN nt_sweep_result.base_brick_size IS
  'Base brick_size from YAML before ATR scaling (geometry_risk.brick_vol_scale applied)';
COMMENT ON COLUMN nt_sweep_result.brick_vol_scale IS
  'ATR scaling factor applied to base_brick_size (1.0 = no scaling)';
COMMENT ON COLUMN nt_sweep_result.vol_ratio IS
  'Short/long realized vol ratio at signal time (tiered_atr.compute_vol_ratio)';
