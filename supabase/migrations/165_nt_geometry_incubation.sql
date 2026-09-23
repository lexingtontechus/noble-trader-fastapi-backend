-- Migration 165: geometry forward-test / incubation gate
--
-- 2026-09-23 (user decision): heavy sweep picks a brick/SL/TP combo from a
-- single in-sample 90-day grid backtest and writes it straight to
-- config/brick_params.yaml for the very next light-sweep tick to trade live
-- (write_brick_params() in regime_platform/config/brick_params.py — "the
-- SOLE writer"). There is no forward/out-of-sample check before a new
-- geometry goes live; tests/test_renko_backtest.py still references a
-- walk_forward() method removed in v7.3 and never rebuilt.
--
-- nt_geometry_incubation tracks one CANDIDATE combo per symbol at a time
-- (partial unique index enforces this). geometry_incubation.py's
-- reconcile_incubations() re-evaluates each incubating row against the
-- ACTUAL forward-resolved nt_signal_sim rows generated under that exact
-- (brick_size, sl_bricks, tp_bricks) since started_at, and promotes /
-- rejects / holds per config/feature_flags.yaml's
-- geometry_incubation_min_days / geometry_incubation_min_resolved gates.
--
-- Mode-gated (geometry_incubation_mode: off | shadow | live), same
-- convention as every other flag in this file:
--   off:    byte-identical to pre-migration behavior — heavy sweep writes
--           straight to YAML, this table is never touched.
--   shadow: heavy sweep writes straight to YAML AS BEFORE (zero live
--           behavior change) but every pick is ALSO tracked here so the
--           promote/reject verdict can be reviewed before anything gates.
--   live:   a brand-new candidate is held in `incubating` (YAML keeps the
--           previous live combo) until reconcile_incubations() promotes it;
--           only then does the write actually happen.

CREATE TABLE IF NOT EXISTS nt_geometry_incubation (
  id                BIGSERIAL PRIMARY KEY,
  symbol            TEXT NOT NULL,
  brick_size        DOUBLE PRECISION NOT NULL,
  sl_bricks         INTEGER NOT NULL,
  tp_bricks         INTEGER NOT NULL,
  asset_class       TEXT,
  source            TEXT NOT NULL DEFAULT 'heavy_sweep',
  status            TEXT NOT NULL DEFAULT 'incubating',
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at        TIMESTAMPTZ,
  last_checked_at   TIMESTAMPTZ,
  n_resolved        INTEGER NOT NULL DEFAULT 0,
  wins              INTEGER NOT NULL DEFAULT 0,
  losses            INTEGER NOT NULL DEFAULT 0,
  expired           INTEGER NOT NULL DEFAULT 0,
  expectancy_r      DOUBLE PRECISION,
  pct_expired       DOUBLE PRECISION,
  baseline_pct_expired DOUBLE PRECISION,
  reason            TEXT
);

ALTER TABLE nt_geometry_incubation
  DROP CONSTRAINT IF EXISTS nt_geometry_incubation_status_check;
ALTER TABLE nt_geometry_incubation
  ADD CONSTRAINT nt_geometry_incubation_status_check
  CHECK (status = ANY (ARRAY['incubating', 'promoted', 'rejected', 'superseded']));

-- Only one ACTIVE (incubating) candidate per symbol at a time — a new pick
-- for the same symbol while one is already incubating must first mark the
-- old row 'superseded' (geometry_incubation.start_incubation does this).
CREATE UNIQUE INDEX IF NOT EXISTS idx_geometry_incubation_active_per_symbol
  ON nt_geometry_incubation (symbol)
  WHERE status = 'incubating';

CREATE INDEX IF NOT EXISTS idx_geometry_incubation_symbol_started
  ON nt_geometry_incubation (symbol, started_at DESC);

COMMENT ON TABLE nt_geometry_incubation IS
  'Forward-test / incubation gate for heavy-sweep brick geometry picks (2026-09-23). A candidate (symbol, brick_size, sl_bricks, tp_bricks) sits in status=incubating until reconcile_incubations() has enough forward-resolved nt_signal_sim evidence (min days + min resolved trades) to promote or reject it. status=off mode never writes here; shadow tracks without gating; live gates the actual config/brick_params.yaml write.';
COMMENT ON COLUMN nt_geometry_incubation.reason IS
  'Human-readable promote/reject/hold reason from geometry_incubation.evaluate_promotion (e.g. insufficient_samples, insufficient_days, expectancy_r_negative, pct_expired_regression, promoted).';
COMMENT ON COLUMN nt_geometry_incubation.baseline_pct_expired IS
  'pct_expired of the symbol''s PREVIOUS live combo at incubation start (from nt_brick_params_state), for the regression check — a candidate should not go live if it resolves worse than what it is replacing.';

ALTER TABLE nt_geometry_incubation ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS nt_geometry_incubation_service ON nt_geometry_incubation;
CREATE POLICY nt_geometry_incubation_service ON nt_geometry_incubation
  FOR ALL USING (true) WITH CHECK (true);

GRANT SELECT ON nt_geometry_incubation TO anon, authenticated;
GRANT INSERT, UPDATE ON nt_geometry_incubation TO anon, authenticated;
