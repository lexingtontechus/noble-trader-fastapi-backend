-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 191: HMM skill-vs-baseline columns
--
-- Phase 1a of worklog/20261001_HMM_FIRST_PASSAGE_AND_REGIME_TRANSITION.md
-- (parent scope: docs/scopes/20261001_BOLSTER_LEGACY_HMM_SCOPE.md §5.1).
--
-- WHY: the RM-1 evaluation (worklog/kairos/reports/20261001_rm1_markov_source_eval.md)
-- could only report `accuracy` because that is all nt_hmm_shadow_accuracy
-- stores. It took a bespoke query against nt_signal_sim to discover that the
-- legacy HMM's 0.5251 accuracy is BELOW the naive "always predict the majority
-- direction" baseline (0.5655) and well below a shrunk per-symbol base rate
-- (0.5898) — i.e. the model has NEGATIVE skill. A reviewer reading "0.527"
-- would reasonably conclude "marginally useful". The number that matters is
-- "−0.0404", and it belongs on the review table.
--
-- Precedent: kairos_rank.py exists because p_win's IC was not being measured,
-- and discovering that it did not rank changed the programme. The same
-- instrument was missing here.
--
-- Additive and nullable — existing rows keep NULL until the next weekly run
-- populates them. Column-style follows migration 131 (same table, same
-- conventions: nt_ prefix, snake_case, DOUBLE PRECISION, service-role RLS).
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_hmm_shadow_accuracy
  ADD COLUMN IF NOT EXISTS base_rate DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS skill     DOUBLE PRECISION;

COMMENT ON COLUMN nt_hmm_shadow_accuracy.base_rate IS
  'Tier 1 (2026-10-01): the symbol''s own majority-direction share over ALL decided rows (not just this source''s calls) — i.e. what a naive per-symbol always-predict-the-majority model would score. Populated for metric IN (''legacy'',''trend_hmm''); NULL for Tier 2 rows (''raw'',''gated''), whose comparison is a Brier score, not a hit rate.';

COMMENT ON COLUMN nt_hmm_shadow_accuracy.skill IS
  'Tier 1 (2026-10-01): accuracy − base_rate. THE number to read: negative means the source is worse than saying nothing. Legacy HMM measured −0.0404 weighted (0.5251 accuracy vs 0.5655 naive baseline) on 31,328 decided rows, 2026-05-31 → 2026-10-01 — which is why p_markov_hold_n was withdrawn from the EV blend (p_markov_hold_n_mode: "off"). NULL for Tier 2 rows.';

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION (paste into Supabase SQL editor):
--   SELECT column_name, data_type FROM information_schema.columns
--     WHERE table_name = 'nt_hmm_shadow_accuracy'
--       AND column_name IN ('base_rate','skill');
--   -- Expect 2 rows, both double precision.
--
--   -- After the next weekly job run, the legacy row should show negative skill:
--   SELECT symbol, metric, n, coverage, accuracy, base_rate, skill
--     FROM nt_hmm_shadow_accuracy WHERE metric = 'legacy' ORDER BY skill;
-- ═══════════════════════════════════════════════════════════════════════════════
