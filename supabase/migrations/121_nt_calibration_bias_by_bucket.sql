-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 121: Phase 2 of the calibration reframe
-- (worklog/20260825_CALIBRATION_REFRAME_SCOPE.md)
--
-- Phase 1 (migration 120) built a read-only reachability diagnostic. Phase 2
-- wires that into calibration bias and adds three user-facing trade-quality
-- metrics found to already be computable (expectancy-in-R, daily/edge-per-
-- timeframe) or cheaply capturable (post-entry drift) from existing data:
--
--   nt_calibration_records.signal_id — closes the missing join key between
--     the calibration population (win/loss only, no bracket geometry) and
--     nt_signal_sim (has the geometry + atr). Nullable, populated going
--     forward at eod_resolver.py::_write_calibration_record; historical rows
--     stay unlinked (same backfill trade-off migration 120 made for atr).
--
--   nt_signal_sim.drift_1h_pct / drift_4h_pct / drift_24h_pct — direction-
--     relative price drift at fixed post-entry horizons (positive = moved in
--     the signal's favor), computed by eod_resolver.py from the post-entry
--     candle path it already fetches. Answers "was the entry followed by a
--     favorable or adverse move, and how much does that vary" — independent
--     of whether the trade goes on to hit TP/SL/expire.
--
--   nt_calibration_bias_by_bucket — per (symbol, distance_atr_bucket): bias
--     (avg_p_win - win_rate, the existing calibration formula) alongside
--     pct_expired (joined from nt_bracket_reachability), win/loss expectancy
--     in R, and a hold-time-adjusted daily expectancy. One row shows all
--     three failure-mode lenses found during Phase 2 scoping (reachability,
--     directional, magnitude) side by side, without hardcoding a classifier.
--
--   nt_entry_drift — per symbol: mean + stdev of the three drift horizons
--     above, across ALL resolved signals (any outcome) — the aggregate
--     report for the drift columns.
--
-- Column-style follows migrations 100/102/120: nt_ prefix, snake_case,
-- DOUBLE PRECISION, TIMESTAMPTZ, CHECK via = ANY (ARRAY[...]),
-- DROP POLICY IF EXISTS before CREATE POLICY, service-role-only RLS.
--
-- Read-only diagnostic scope only (Phase 2, per the worklog): no change to
-- eod_correction.py's classification/actions, auto_tuner.py's health
-- metric, or the migration-119 SQL views.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. nt_calibration_records.signal_id — join key to nt_signal_sim ──────────

ALTER TABLE nt_calibration_records
  ADD COLUMN IF NOT EXISTS signal_id UUID REFERENCES nt_signal_sim(signal_id);

COMMENT ON COLUMN nt_calibration_records.signal_id IS
  'FK to nt_signal_sim.signal_id — the resolved signal this calibration record was written from. Nullable: rows written before 2026-08-25 predate this column and stay unlinked.';

-- ─── 2. nt_signal_sim drift columns + mfe/mae comment fix ─────────────────────

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS drift_1h_pct DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS drift_4h_pct DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS drift_24h_pct DOUBLE PRECISION;

COMMENT ON COLUMN nt_signal_sim.drift_1h_pct IS
  'Direction-relative price drift at 1h post-entry, as % of entry_price (positive = moved in the signal''s favor). Computed from the post-entry candle path regardless of eventual outcome. Nullable: null when fewer than 1 post-entry bar was available.';
COMMENT ON COLUMN nt_signal_sim.drift_4h_pct IS
  'Same as drift_1h_pct, at 4h post-entry.';
COMMENT ON COLUMN nt_signal_sim.drift_24h_pct IS
  'Same as drift_1h_pct, at 24h post-entry.';

-- Migration 102 documented mfe/mae as "in bricks" — eod_resolver.py actually
-- computes them as raw price-unit deltas (found during Phase 2 scoping).
-- Correcting the column comments; no data or code changes needed.
COMMENT ON COLUMN nt_signal_sim.mfe IS
  'Max favorable excursion over the walked path, in price units (NOT bricks — corrected 2026-08-25; see eod_resolver.py::resolve_signal_outcome).';
COMMENT ON COLUMN nt_signal_sim.mae IS
  'Max adverse excursion over the walked path, in price units (NOT bricks — corrected 2026-08-25; see eod_resolver.py::resolve_signal_outcome).';

-- ─── 3. nt_calibration_bias_by_bucket ──────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_calibration_bias_by_bucket (
  symbol                  TEXT NOT NULL,
  distance_atr_bucket     TEXT NOT NULL,
  n                       INTEGER NOT NULL,
  avg_p_win               DOUBLE PRECISION,
  win_rate                DOUBLE PRECISION,
  bias                    DOUBLE PRECISION,
  pct_expired             DOUBLE PRECISION,
  avg_rr_winners          DOUBLE PRECISION,
  expectancy_r            DOUBLE PRECISION,
  median_hours_to_resolve DOUBLE PRECISION,
  daily_expectancy_r      DOUBLE PRECISION,
  atr_source              TEXT NOT NULL,
  computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, distance_atr_bucket)
);

ALTER TABLE nt_calibration_bias_by_bucket
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_bucket_bucket_check;
ALTER TABLE nt_calibration_bias_by_bucket
  ADD CONSTRAINT nt_calibration_bias_by_bucket_bucket_check
  CHECK (distance_atr_bucket = ANY (ARRAY['<0.5', '0.5-1', '1-2', '2-4', '4+']));

ALTER TABLE nt_calibration_bias_by_bucket
  DROP CONSTRAINT IF EXISTS nt_calibration_bias_by_bucket_atr_source_check;
ALTER TABLE nt_calibration_bias_by_bucket
  ADD CONSTRAINT nt_calibration_bias_by_bucket_atr_source_check
  CHECK (atr_source = ANY (ARRAY['stored', 'proxy_current']));

COMMENT ON TABLE nt_calibration_bias_by_bucket IS
  'Read-only diagnostic (Phase 2, worklog 20260825_CALIBRATION_REFRAME_SCOPE.md): calibration bias, realized expectancy-in-R, and hold-time-adjusted daily expectancy, per (symbol, distance/ATR bucket), computed from win/loss-resolved nt_signal_sim rows. pct_expired is joined in from nt_bracket_reachability for the same bucket. Not yet consumed by eod_correction.py or any enforcement path.';
COMMENT ON COLUMN nt_calibration_bias_by_bucket.bias IS
  'avg_p_win - win_rate — the existing calibration-bias formula (fetch_calibration_bias_raw), recomputed per bucket instead of pooled per symbol.';
COMMENT ON COLUMN nt_calibration_bias_by_bucket.avg_rr_winners IS
  'Mean tp_bricks/sl_bricks among tp_hit rows in this bucket — the realized reward:risk actually attained by winners (a loss is always exactly -1R by construction).';
COMMENT ON COLUMN nt_calibration_bias_by_bucket.expectancy_r IS
  'win_rate * avg_rr_winners - (1 - win_rate) * 1 — expected R per trade in this bucket.';
COMMENT ON COLUMN nt_calibration_bias_by_bucket.daily_expectancy_r IS
  'expectancy_r / (median_hours_to_resolve / 24) — expectancy normalized by typical hold time; does not account for position concurrency/capital redeployment (out of scope by design, see worklog).';

-- ─── 4. nt_entry_drift ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_entry_drift (
  symbol            TEXT PRIMARY KEY,
  n                 INTEGER NOT NULL,
  avg_drift_1h_pct  DOUBLE PRECISION,
  stdev_drift_1h_pct DOUBLE PRECISION,
  avg_drift_4h_pct  DOUBLE PRECISION,
  stdev_drift_4h_pct DOUBLE PRECISION,
  avg_drift_24h_pct DOUBLE PRECISION,
  stdev_drift_24h_pct DOUBLE PRECISION,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_entry_drift IS
  'Read-only diagnostic (Phase 2): mean + stdev of post-entry price drift at 1h/4h/24h horizons, per symbol, across ALL resolved nt_signal_sim rows (any outcome). Mean answers "does price typically run in our favor or against us right after entry"; stdev is the noise/variance a trader should expect regardless of directional accuracy, and doubles as a live check on Phase 1''s ATR sizing assumption.';

-- ─── 5. Row Level Security ─────────────────────────────────────────────────────

ALTER TABLE nt_calibration_bias_by_bucket ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_calibration_bias_by_bucket;
CREATE POLICY "Service role full access" ON nt_calibration_bias_by_bucket
  FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE nt_entry_drift ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_entry_drift;
CREATE POLICY "Service role full access" ON nt_entry_drift
  FOR ALL TO service_role USING (true) WITH CHECK (true);
