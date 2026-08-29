-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 128: nt_heavy_sweep_reachability_shadow
--
-- Phase 3a of the calibration reframe (worklog/20260825_CALIBRATION_REFRAME_SCOPE.md,
-- "Phase 3 scope"). A live simulation against real price data (5 symbols)
-- found the heavy sweep's Sharpe-only grid pick sometimes lands in a
-- distance/ATR bucket with negative realized expectancy (nt_calibration_
-- bias_by_bucket), and for 2 of 5 symbols tested, the grid never even
-- generated a candidate in a positive-expectancy bucket — a pure post-hoc
-- filter would have nothing to fall back to. Phase 3a ships the filter as
-- a SHADOW computation only (heavy_sweep_reachability_mode=off|shadow|live,
-- config/feature_flags.yaml, default off): compute what a reachability/
-- expectancy-aware re-rank would have picked, persist the comparison here,
-- change nothing about the actual pick. Phase 3b (widening the grid itself
-- so there's something good to point the filter at) is gated on real data
-- from this table accumulating over a few weekly cycles — not built yet.
--
-- Column-style follows migration 122 (nt_markov_source_shadow) — the
-- closest analog (a running per-symbol shadow comparison, not a
-- per-observation table): nt_ prefix, snake_case, service-role-only RLS,
-- updated_at included in every upsert payload from the start (2026-08-27
-- lesson from that same migration's own follow-up fix).
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_heavy_sweep_reachability_shadow (
  symbol                TEXT PRIMARY KEY,
  n_compared            INTEGER NOT NULL DEFAULT 0,
  n_nothing_survived    INTEGER NOT NULL DEFAULT 0,
  n_pick_changed        INTEGER NOT NULL DEFAULT 0,
  last_baseline_bucket  TEXT,
  last_baseline_sharpe  DOUBLE PRECISION,
  last_filtered_bucket  TEXT,
  last_filtered_sharpe  DOUBLE PRECISION,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_heavy_sweep_reachability_shadow IS
  'Phase 3a shadow comparison (worklog 20260825_CALIBRATION_REFRAME_SCOPE.md): per symbol, how often the heavy sweep''s Sharpe-only grid pick would change (n_pick_changed) or have no reachable/positive-expectancy replacement at all (n_nothing_survived) under a distance/ATR-bucket-aware filter. Diagnostic only while heavy_sweep_reachability_mode=shadow; read manually before considering Phase 3b (grid widening) or a live-mode decision.';
COMMENT ON COLUMN nt_heavy_sweep_reachability_shadow.n_nothing_survived IS
  'Count of sweeps where NO grid candidate (after the existing MIN_RR/MAX_SHARPE/MIN_TRADES filter) landed in a bucket with expectancy_r > 0 or no calibration data yet — the "grid has nothing good to offer" case found live for XAUUSD/AAPL.';
COMMENT ON COLUMN nt_heavy_sweep_reachability_shadow.n_pick_changed IS
  'Count of sweeps where the reachability-aware pick differs from the baseline Sharpe-only pick (and at least one reachable candidate existed).';

ALTER TABLE nt_heavy_sweep_reachability_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_heavy_sweep_reachability_shadow;
CREATE POLICY "Service role full access" ON nt_heavy_sweep_reachability_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);
