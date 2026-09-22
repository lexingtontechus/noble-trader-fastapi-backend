-- Migration 162: cell-level calibration bias (R3)
-- ─────────────────────────────────────────────────────────────────────
-- R3 (worklog 20260922_L1_CONFIDENT_CALIBRATION_RECOMMENDATIONS.md):
-- bias per (symbol, side, vol_tier, distance_atr_bucket) so enforcement
-- never shrinks one symbol/side because another cell is overconfident.
-- Existing nt_calibration_bias_by_bucket stays pooled (no side) for
-- reachability joins; this table is the enforcement input.
--
-- Cell bias uses the same Beta(α=β=2) shrink toward 0.5 as p_win_bracket
-- (bias_shrunk = avg_p_win − beta_shrunk_win_rate(wins, losses)).

CREATE TABLE IF NOT EXISTS nt_calibration_bias_by_cell (
  symbol                  TEXT NOT NULL,
  side                    TEXT NOT NULL,
  vol_tier                TEXT NOT NULL,
  distance_atr_bucket     TEXT NOT NULL,
  n                       INTEGER NOT NULL,
  wins                    INTEGER NOT NULL DEFAULT 0,
  losses                  INTEGER NOT NULL DEFAULT 0,
  avg_p_win               DOUBLE PRECISION,
  win_rate                DOUBLE PRECISION,
  bias                    DOUBLE PRECISION,
  bias_shrunk             DOUBLE PRECISION,
  expectancy_r            DOUBLE PRECISION,
  computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, side, vol_tier, distance_atr_bucket)
);

ALTER TABLE nt_calibration_bias_by_cell
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_cell_side_check;
ALTER TABLE nt_calibration_bias_by_cell
  ADD CONSTRAINT nt_calibration_bias_by_cell_side_check
  CHECK (side = ANY (ARRAY['buy', 'sell', 'all']));

ALTER TABLE nt_calibration_bias_by_cell
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_cell_vol_tier_check;
ALTER TABLE nt_calibration_bias_by_cell
  ADD CONSTRAINT nt_calibration_bias_by_cell_vol_tier_check
  CHECK (vol_tier = ANY (ARRAY['unknown', 'low_vol', 'med_low_vol', 'med_high_vol', 'high_vol']));

ALTER TABLE nt_calibration_bias_by_cell
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_cell_bucket_check;
ALTER TABLE nt_calibration_bias_by_cell
  ADD CONSTRAINT nt_calibration_bias_by_cell_bucket_check
  CHECK (distance_atr_bucket = ANY (ARRAY['<0.5', '0.5-1', '1-2', '2-4', '4+']));

COMMENT ON TABLE nt_calibration_bias_by_cell IS
  'R3 cell-level calibration bias (migration 162). Per (symbol, side, vol_tier, distance_atr_bucket) from win/loss-resolved nt_signal_sim. bias_shrunk applies Beta(α=β=2) shrink of win_rate toward 0.5 (same prior as p_win_bracket). Enforcement (apply_calibration_adjustment) reads the cell when n >= CELL_MIN_N, else parent symbol state, else system (no-op). Pooled nt_calibration_bias_by_bucket is unchanged for reachability joins.';
COMMENT ON COLUMN nt_calibration_bias_by_cell.bias IS
  'avg_p_win - win_rate — raw calibration bias (same formula as fetch_calibration_bias_raw).';
COMMENT ON COLUMN nt_calibration_bias_by_cell.bias_shrunk IS
  'avg_p_win - Beta(α=β=2) shrunk win_rate. Thin cells pull toward the model being only mildly wrong; used by enforcement when n is sufficient.';
COMMENT ON COLUMN nt_calibration_bias_by_cell.side IS
  'buy | sell = trade side (nt_signal_sim.signal); all = pooled fallback row.';

ALTER TABLE nt_calibration_bias_by_cell ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_calibration_bias_by_cell;
CREATE POLICY "Service role full access" ON nt_calibration_bias_by_cell
  USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "anon read nt_calibration_bias_by_cell" ON nt_calibration_bias_by_cell;
CREATE POLICY "anon read nt_calibration_bias_by_cell" ON nt_calibration_bias_by_cell
  FOR SELECT USING (true);
GRANT SELECT ON nt_calibration_bias_by_cell TO anon;
GRANT ALL ON nt_calibration_bias_by_cell TO service_role;
