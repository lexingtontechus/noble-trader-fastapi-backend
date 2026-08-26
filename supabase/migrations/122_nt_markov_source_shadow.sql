-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 122: nt_markov_source_shadow
--
-- HMM consolidation (worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md,
-- "path forward" section, 2026-08-26). MARKOV_SOURCE_MODE (orchestrator.py)
-- already had a trend_hmm_shadow/live mode letting RegimeHMM's own transition
-- matrix replace the legacy, separately-refit HmmModel — added 2026-08-25 but
-- the comparison was log-line only (ephemeral, and this session found studio
-- logs aren't reachable from this environment). This table persists the
-- running per-symbol comparison so a live-mode decision can be made from real
-- data instead of grep'ing logs.
--
-- Column-style follows migrations 100/102/120/121: nt_ prefix, snake_case,
-- DOUBLE PRECISION, TIMESTAMPTZ, service-role-only RLS.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_markov_source_shadow (
  symbol               TEXT PRIMARY KEY,
  n_compared           INTEGER NOT NULL DEFAULT 0,
  n_state_disagree     INTEGER NOT NULL DEFAULT 0,
  sum_abs_p_up_diff    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  last_legacy_state    TEXT,
  last_trend_hmm_state TEXT,
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_markov_source_shadow IS
  'Running per-symbol comparison between the legacy HmmModel and RegimeHMM-derived transition matrices/current_state, accumulated whenever MARKOV_SOURCE_MODE=trend_hmm_shadow. n_state_disagree/n_compared = disagreement rate; sum_abs_p_up_diff/n_compared = avg |p_up difference|. Diagnostic only — read manually before deciding whether to flip MARKOV_SOURCE_MODE to trend_hmm_live.';
COMMENT ON COLUMN nt_markov_source_shadow.n_state_disagree IS
  'Count of ticks where legacy current_state != RegimeHMM-derived current_state.';
COMMENT ON COLUMN nt_markov_source_shadow.sum_abs_p_up_diff IS
  'Running sum of |legacy p_up - trend_hmm p_up| across compared ticks; divide by n_compared for the average.';

ALTER TABLE nt_markov_source_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_markov_source_shadow;
CREATE POLICY "Service role full access" ON nt_markov_source_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);
