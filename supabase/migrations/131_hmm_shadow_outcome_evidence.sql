-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 131: Tier 1/2 outcome-linked evidence
--
-- worklog/20260831_SHADOW_TO_LIVE_ROADMAP.md. nt_markov_source_shadow
-- (Tier 1) / nt_trend_probs_gate_shadow (Tier 2) measure DISAGREEMENT
-- between the legacy and trend_hmm sources — they don't measure which one
-- is actually right, because neither links a comparison to what the
-- trade did. This closes that gap the smallest way possible: 4 nullable
-- columns on nt_signal_sim, captured at signal time (mirrors the atr/
-- macro_label precedent from migrations 120/124 exactly — no new fetch,
-- no new computation, just persisting values both sweep paths already
-- compute for the existing shadow tables), plus one new small table for
-- the weekly outcome-linked accuracy read.
--
-- Revives, in a much smaller form, the "outcome-validation-layer" idea
-- shelved earlier this session — that was parked because the root-cause
-- classifier wasn't fixed yet and scoring a known-broken read would have
-- measured the wrong thing. The root cause is fixed now (regime_engine.py
-- ._classify_trend_components, verified against real data). This is not
-- a new table needing its own capture pipeline — it's 4 columns on a
-- table that already exists, populated by values already computed.
--
-- Column-style follows migrations 120/124/125: nt_ prefix, snake_case,
-- DOUBLE PRECISION/TEXT, service-role-only RLS.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. nt_signal_sim capture columns ──────────────────────────────────────────

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS legacy_markov_state TEXT,
  ADD COLUMN IF NOT EXISTS trend_hmm_markov_state TEXT,
  ADD COLUMN IF NOT EXISTS raw_p_regime DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS gated_p_regime DOUBLE PRECISION;

COMMENT ON COLUMN nt_signal_sim.legacy_markov_state IS
  'Tier 1 (2026-08-31): legacy HmmModel''s current_state (UP/DOWN/FLAT) at signal-capture time, ONLY populated during the actual markov_source_mode=trend_hmm_shadow comparison — both sources genuinely available and uncorrupted at the same time. Feeds hmm_shadow_accuracy.py.';
COMMENT ON COLUMN nt_signal_sim.trend_hmm_markov_state IS
  'Tier 1: RegimeHMM''s own current_state (UP/DOWN/FLAT) at signal-capture time, same population as legacy_markov_state.';
COMMENT ON COLUMN nt_signal_sim.raw_p_regime IS
  'Tier 2 (2026-08-31): ungated p_regime at signal-capture time, ONLY populated during trend_probs_gate_mode=shadow. Feeds hmm_shadow_accuracy.py''s Brier-score comparison.';
COMMENT ON COLUMN nt_signal_sim.gated_p_regime IS
  'Tier 2: root-cause-classifier-gated p_regime at signal-capture time, same population as raw_p_regime.';

-- ─── 2. nt_hmm_shadow_accuracy ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_hmm_shadow_accuracy (
  symbol            TEXT NOT NULL,
  metric            TEXT NOT NULL,
  -- Tier 1 rows: metric IN ('legacy','trend_hmm') — n/coverage/accuracy populated, brier_score NULL.
  -- Tier 2 rows: metric IN ('raw','gated') — n/brier_score populated, coverage/accuracy NULL.
  n                 INTEGER NOT NULL,
  coverage          DOUBLE PRECISION,
  accuracy          DOUBLE PRECISION,
  brier_score       DOUBLE PRECISION,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, metric)
);

ALTER TABLE nt_hmm_shadow_accuracy
  DROP CONSTRAINT IF EXISTS nt_hmm_shadow_accuracy_metric_check;
ALTER TABLE nt_hmm_shadow_accuracy
  ADD CONSTRAINT nt_hmm_shadow_accuracy_metric_check
  CHECK (metric = ANY (ARRAY['legacy', 'trend_hmm', 'raw', 'gated']));

COMMENT ON TABLE nt_hmm_shadow_accuracy IS
  'Weekly outcome-linked accuracy read for Tier 1 (legacy vs trend_hmm markov source, worklog 20260831_SHADOW_TO_LIVE_ROADMAP.md) and Tier 2 (raw vs gated p_regime), computed from win/loss-resolved nt_signal_sim rows with legacy_markov_state/trend_hmm_markov_state/raw_p_regime/gated_p_regime populated. Tier 1: coverage = % of rows where that source called non-FLAT; accuracy = hit rate against realized direction on ONLY that source''s own non-FLAT calls (a FLAT call is "no answer," not a wrong answer). Tier 2: brier_score = mean (p_regime - realized)^2, realized in {0,1} (tp_hit=1, sl_hit=0) — same convention as eod_resolver.py''s calibration_brier. Diagnostic only — informs the Tier 1/2 live-mode decision reviews (2026-09-21 per the roadmap), does not itself change markov_source_mode/trend_probs_gate_mode.';

ALTER TABLE nt_hmm_shadow_accuracy ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_hmm_shadow_accuracy;
CREATE POLICY "Service role full access" ON nt_hmm_shadow_accuracy
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   SELECT column_name FROM information_schema.columns WHERE table_name = 'nt_signal_sim'
--     AND column_name IN ('legacy_markov_state','trend_hmm_markov_state','raw_p_regime','gated_p_regime');
--   -- Expect 4 rows.
--   SELECT * FROM nt_hmm_shadow_accuracy LIMIT 1;  -- Expect an empty result, not an error.
-- ═══════════════════════════════════════════════════════════════════════════════
