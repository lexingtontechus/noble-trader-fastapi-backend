-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 120: nt_bracket_reachability + nt_signal_sim.atr
--
-- Phase 1 of the calibration reframe (worklog/20260825_CALIBRATION_REFRAME_SCOPE.md):
-- most "expired" outcomes in nt_signal_sim are not a directional-calibration
-- failure — they're a bracket placed wider than the symbol reliably moves within
-- the live deadline. Raw %-distance is meaningless across symbols (a 4% target is
-- trivial for XAGUSD, unreachable for XAUUSD in the same window), so reachability
-- has to be measured as distance/ATR, per symbol, per bucket.
--
-- This migration is READ-ONLY-DIAGNOSTIC scope (Phase 1 only): it adds a place to
-- persist ATR at signal-capture time and a table to hold the computed reachability
-- buckets. It does not change calibration bias, qualification, or bracket
-- selection — see the worklog for Phase 2/3.
--
-- Column-style follows migrations 029/036/102: snake_case, DOUBLE PRECISION for
-- all prob/price fields, TIMESTAMPTZ for time fields, CHECK constraints via
-- = ANY (ARRAY[...]), nt_ prefix.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. nt_signal_sim.atr — ATR(14) at signal-capture time ────────────────────
-- Absolute price units (mean close-to-close True Range over the last 14 hourly
-- bars — see regime_platform/sweep_orchestrator/heavy_sweep.py:compute_atr).
-- Nullable: existing rows and any caller that omits it are unaffected.

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS atr DOUBLE PRECISION;

COMMENT ON COLUMN nt_signal_sim.atr IS
  'ATR(14) at signal-capture time, absolute price units (mean close-to-close True Range over the last 14 hourly bars). Nullable — rows captured before 2026-08-25 have no value.';

-- ─── 2. nt_bracket_reachability — per-symbol, per-bucket reachability table ───
-- One row per (symbol, distance_atr_bucket), refreshed in place on the same
-- weekly cadence as the heavy sweep (upsert on the composite key — see
-- bracket_reachability.py / supabase_client.upsert_bracket_reachability).

CREATE TABLE IF NOT EXISTS nt_bracket_reachability (
  symbol                  TEXT NOT NULL,
  distance_atr_bucket     TEXT NOT NULL,
  n                       INTEGER NOT NULL,
  pct_hit_tp              DOUBLE PRECISION,
  pct_hit_sl              DOUBLE PRECISION,
  pct_expired             DOUBLE PRECISION,
  median_hours_to_resolve DOUBLE PRECISION,
  p75_hours               DOUBLE PRECISION,
  p90_hours               DOUBLE PRECISION,
  atr_source              TEXT NOT NULL,
  computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, distance_atr_bucket)
);

ALTER TABLE nt_bracket_reachability
  DROP CONSTRAINT IF EXISTS nt_bracket_reachability_bucket_check;
ALTER TABLE nt_bracket_reachability
  ADD CONSTRAINT nt_bracket_reachability_bucket_check
  CHECK (distance_atr_bucket = ANY (ARRAY['<0.5', '0.5-1', '1-2', '2-4', '4+']));

ALTER TABLE nt_bracket_reachability
  DROP CONSTRAINT IF EXISTS nt_bracket_reachability_atr_source_check;
ALTER TABLE nt_bracket_reachability
  ADD CONSTRAINT nt_bracket_reachability_atr_source_check
  CHECK (atr_source = ANY (ARRAY['stored', 'proxy_current']));

COMMENT ON TABLE nt_bracket_reachability IS
  'Read-only diagnostic (Phase 1, worklog 20260825_CALIBRATION_REFRAME_SCOPE.md): per-symbol, per-distance/ATR-bucket hit-rate and time-to-resolution stats computed from resolved nt_signal_sim rows. Not yet consumed by calibration bias or bracket selection.';
COMMENT ON COLUMN nt_bracket_reachability.distance_atr_bucket IS
  'abs(take_profit - entry_price) / atr, bucketed: <0.5 | 0.5-1 | 1-2 | 2-4 | 4+.';
COMMENT ON COLUMN nt_bracket_reachability.atr_source IS
  'stored: row carried its own nt_signal_sim.atr. proxy_current: backfilled from the symbol''s current ATR(14) because the row predates ATR capture.';

-- ─── 3. Row Level Security ─────────────────────────────────────────────────
-- Application-layer table, written by the service role only (same policy
-- shape as migration 102).

ALTER TABLE nt_bracket_reachability ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_bracket_reachability;
CREATE POLICY "Service role full access" ON nt_bracket_reachability
  FOR ALL TO service_role USING (true) WITH CHECK (true);
