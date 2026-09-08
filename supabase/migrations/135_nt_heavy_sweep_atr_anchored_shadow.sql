-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 135: nt_heavy_sweep_atr_anchored_shadow
--
-- Phase A of worklog
-- 20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md.
-- heavy_sweep_atr_anchored_bricks_mode (off|shadow|live, config/feature_flags.yaml,
-- default off) adds 3 ATR-anchored brick-size candidates (0.5x/1x/2x ATR)
-- to the heavy-sweep grid search, alongside the existing 5 price-%-based
-- ones. In shadow mode the real (persisted) pick is unaffected — this
-- table is where the comparison against what the widened grid WOULD have
-- picked gets persisted, so the flip-to-live decision can be made from a
-- query instead of grepping logs. Same gap this session found and closed
-- for heavy_sweep_reachability_mode's own shadow table (migration 128,
-- nt_heavy_sweep_reachability_shadow) — this is its direct sibling.
--
-- Column-style mirrors migration 128 exactly — same running per-symbol
-- shadow comparison shape, nt_ prefix, snake_case, service-role-only RLS,
-- updated_at included in every upsert payload from the start.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_heavy_sweep_atr_anchored_shadow (
  symbol                TEXT PRIMARY KEY,
  n_compared            INTEGER NOT NULL DEFAULT 0,
  n_changed             INTEGER NOT NULL DEFAULT 0,
  last_baseline_bucket  TEXT,
  last_baseline_sharpe  DOUBLE PRECISION,
  last_widened_bucket   TEXT,
  last_widened_sharpe   DOUBLE PRECISION,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_heavy_sweep_atr_anchored_shadow IS
  'Phase A shadow comparison (worklog 20260907_REACHABILITY_LIVE_RESOLUTION_AND_SIGNAL_QUALITY_REPORT_SCOPE.md): per symbol, how often adding 3 ATR-anchored brick-size candidates (0.5x/1x/2x ATR) to the heavy-sweep grid would change the Sharpe-only pick (n_changed), and what distance/ATR bucket the baseline vs. widened pick land in. Diagnostic only while heavy_sweep_atr_anchored_bricks_mode=shadow; read manually (joined against nt_calibration_bias_by_bucket by bucket) before flipping to live — the flag is global across all symbols, not per-symbol, so this table is reviewed for ALL symbols together before any flip, not one at a time.';
COMMENT ON COLUMN nt_heavy_sweep_atr_anchored_shadow.n_changed IS
  'Count of sweeps where the widened (original + ATR-anchored) grid''s Sharpe-only pick differs from the original-5-candidate-only pick — including cases where the original grid found nothing at all and the widened grid rescued it.';
COMMENT ON COLUMN nt_heavy_sweep_atr_anchored_shadow.last_baseline_bucket IS
  'distance/ATR bucket (bracket_reachability.ATR_BUCKETS) of the most recent baseline (original-5-candidate-only) pick, or NULL if none passed the quality filters.';
COMMENT ON COLUMN nt_heavy_sweep_atr_anchored_shadow.last_widened_bucket IS
  'distance/ATR bucket of the most recent widened (original + ATR-anchored) pick, or NULL if none passed the quality filters.';

ALTER TABLE nt_heavy_sweep_atr_anchored_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_heavy_sweep_atr_anchored_shadow;
CREATE POLICY "Service role full access" ON nt_heavy_sweep_atr_anchored_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);
