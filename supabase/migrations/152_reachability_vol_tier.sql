-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 152: vol_tier column on nt_bracket_reachability,
-- nt_calibration_bias_by_bucket, nt_heavy_sweep_reachability_shadow
--
-- worklog/20260913_REACHABILITY_VOL_TIER_SCOPE.md. Reachability today buckets
-- by (symbol, distance_atr_bucket) only — ATR-scale itself is regime-
-- conditional (the HMM's volatility dimension is fit on the same price
-- history ATR is computed from), so the same "4+" bucket represents a
-- much smaller absolute distance in a low_vol regime than in a high_vol
-- regime of the same symbol. migration 151's regime-tiered scorecard
-- surfaced exactly this: XAUUSD sell/med_high_vol_bear = 60% win, XAUUSD
-- sell/med_low_vol_bear = 0% win, same symbol, same direction, same
-- "4+" bucket historically. Adding vol_tier to the bucket key is the same
-- fix Phase 1 (migration 120) already applied once — raw %-distance was
-- meaningless across symbols, so it became distance/ATR; this applies the
-- same argument one dimension further, within a symbol.
--
-- vol_tier is NOT NULL DEFAULT 'unknown' (concrete sentinel, matching the
-- existing atr_source='stored'/'proxy_current' convention) rather than
-- nullable — PostgREST's on_conflict upsert parameter requires a real
-- unique/PK constraint on the exact column list, which an expression
-- index (COALESCE(vol_tier,...)) cannot satisfy. Rows whose regime was
-- "unknown"/uncaptured bucket into 'unknown', not dropped.
--
-- No qualification, gate, or live-trading behavior changes with this
-- migration alone — nt_bracket_reachability/nt_calibration_bias_by_bucket
-- are read-only diagnostics; heavy_sweep_reachability_mode stays in
-- `shadow` (config/feature_flags.yaml, unchanged by this migration).
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. nt_bracket_reachability — widen PK to (symbol, vol_tier, bucket) ──────

ALTER TABLE nt_bracket_reachability
  ADD COLUMN IF NOT EXISTS vol_tier TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE nt_bracket_reachability
  DROP CONSTRAINT IF EXISTS nt_bracket_reachability_vol_tier_check;
ALTER TABLE nt_bracket_reachability
  ADD CONSTRAINT nt_bracket_reachability_vol_tier_check
  CHECK (vol_tier = ANY (ARRAY['low_vol', 'med_low_vol', 'med_high_vol', 'high_vol', 'unknown']));

ALTER TABLE nt_bracket_reachability
  DROP CONSTRAINT IF EXISTS nt_bracket_reachability_pkey;
ALTER TABLE nt_bracket_reachability
  ADD CONSTRAINT nt_bracket_reachability_pkey
  PRIMARY KEY (symbol, vol_tier, distance_atr_bucket);

COMMENT ON COLUMN nt_bracket_reachability.vol_tier IS
  'HMM volatility-dimension label at signal-capture time: low_vol | med_low_vol | med_high_vol | high_vol (parsed from nt_signal_sim.regime, e.g. "med_low_vol_strong_bear" -> "med_low_vol"). "unknown" for rows whose regime was "unknown"/uncaptured -- treated as its own bucket, not dropped.';

COMMENT ON TABLE nt_bracket_reachability IS
  'Read-only diagnostic (Phase 1, worklog 20260825_CALIBRATION_REFRAME_SCOPE.md; vol-tiered 2026-09-13, worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md): per-symbol, per-vol-tier, per-distance/ATR-bucket hit-rate and time-to-resolution stats computed from resolved nt_signal_sim rows. Consumed by heavy_sweep_reachability_mode (Phase 3a shadow filter) via nt_calibration_bias_by_bucket''s expectancy_r, keyed the same way.';


-- ─── 2. nt_calibration_bias_by_bucket — same widening ─────────────────────────

ALTER TABLE nt_calibration_bias_by_bucket
  ADD COLUMN IF NOT EXISTS vol_tier TEXT NOT NULL DEFAULT 'unknown';

ALTER TABLE nt_calibration_bias_by_bucket
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_bucket_vol_tier_check;
ALTER TABLE nt_calibration_bias_by_bucket
  ADD CONSTRAINT nt_calibration_bias_by_bucket_vol_tier_check
  CHECK (vol_tier = ANY (ARRAY['low_vol', 'med_low_vol', 'med_high_vol', 'high_vol', 'unknown']));

ALTER TABLE nt_calibration_bias_by_bucket
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_bucket_pkey;
ALTER TABLE nt_calibration_bias_by_bucket
  ADD CONSTRAINT nt_calibration_bias_by_bucket_pkey
  PRIMARY KEY (symbol, vol_tier, distance_atr_bucket);

COMMENT ON COLUMN nt_calibration_bias_by_bucket.vol_tier IS
  'Same semantics as nt_bracket_reachability.vol_tier -- HMM volatility-dimension label at signal-capture time, "unknown" = uncaptured regime treated as its own bucket.';

COMMENT ON TABLE nt_calibration_bias_by_bucket IS
  'Per (symbol, vol_tier, distance/ATR bucket) calibration bias + expectancy_r, computed from win/loss-resolved nt_signal_sim rows (migration 121; vol-tiered 2026-09-13, worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md). expectancy_r is what heavy_sweep_reachability_mode''s shadow/live filter reads.';


-- ─── 3. nt_heavy_sweep_reachability_shadow — record which vol tier compared ───
-- Visibility only (not part of the upsert key -- this table stays keyed
-- on symbol alone, one running row per symbol, same as today). Lets a
-- human query "is this symbol's nothing-survived pattern concentrated in
-- a specific vol tier" directly, without cross-referencing nt_signal_sim
-- by timestamp.

ALTER TABLE nt_heavy_sweep_reachability_shadow
  ADD COLUMN IF NOT EXISTS vol_tier TEXT;

ALTER TABLE nt_heavy_sweep_reachability_shadow
  DROP CONSTRAINT IF EXISTS nt_heavy_sweep_reachability_shadow_vol_tier_check;
ALTER TABLE nt_heavy_sweep_reachability_shadow
  ADD CONSTRAINT nt_heavy_sweep_reachability_shadow_vol_tier_check
  CHECK (vol_tier IS NULL OR vol_tier = ANY (ARRAY['low_vol', 'med_low_vol', 'med_high_vol', 'high_vol', 'unknown']));

COMMENT ON COLUMN nt_heavy_sweep_reachability_shadow.vol_tier IS
  'The live regime''s vol tier at the most recent comparison (2026-09-13, worklog 20260913_REACHABILITY_VOL_TIER_SCOPE.md) -- visibility only, not part of the upsert key (still keyed on symbol alone). NULL permitted here (unlike the bucket tables) since this column is not part of any uniqueness constraint.';
