-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 123: nt_trend_probs_gate_shadow
--
-- Tier 2 HMM fix (worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md,
-- 2026-08-27). Tier 1 (migration 122) fixed the same rank-only label-assignment
-- bug for the markov transition-matrix derivation only. Tier 2 applies the
-- identical magnitude/sign gate to trend_probs itself
-- (RegimeHMM.gated_trend_probs()), which feeds p_regime directly into the live
-- EV blend — a bigger, already-live surface, so this ships shadow-mode first
-- (trend_probs_gate_mode=shadow in config/feature_flags.yaml): both raw and
-- gated p_regime are computed, but only the running comparison is persisted
-- here — compute_ev still sees the RAW value until a human reviews this table
-- and flips the mode to live.
--
-- Column-style follows migration 122 (nt_markov_source_shadow): nt_ prefix,
-- snake_case, service-role-only RLS. Learned from that migration's own
-- follow-up fix: updated_at is included in every upsert payload from the
-- start this time (122's initial version omitted it from the payload, so it
-- silently never refreshed after the first insert).
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_trend_probs_gate_shadow (
  symbol               TEXT PRIMARY KEY,
  n_compared           INTEGER NOT NULL DEFAULT 0,
  sum_abs_p_regime_diff DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  last_raw_p_regime    DOUBLE PRECISION,
  last_gated_p_regime  DOUBLE PRECISION,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_trend_probs_gate_shadow IS
  'Running per-symbol comparison between raw and Tier-2-gated p_regime, accumulated whenever trend_probs_gate_mode=shadow. sum_abs_p_regime_diff/n_compared = avg |p_regime difference|. Diagnostic only — read manually before deciding whether to flip trend_probs_gate_mode to live.';
COMMENT ON COLUMN nt_trend_probs_gate_shadow.sum_abs_p_regime_diff IS
  'Running sum of |raw_p_regime - gated_p_regime| across compared ticks; divide by n_compared for the average.';

ALTER TABLE nt_trend_probs_gate_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_trend_probs_gate_shadow;
CREATE POLICY "Service role full access" ON nt_trend_probs_gate_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);
