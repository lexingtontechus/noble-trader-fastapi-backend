-- 172_kairos_rank_score.sql  (2026-09-26)
-- Shadow composite rank score (worklog/kairos/20260926_DIRECTIONAL_EDGE_PWIN_RANKING_AND_TRANSITIONS.md §9).
-- Additive only: nullable columns on nt_signal_sim. No behaviour change —
-- qualification, sizing, SL/TP and delivery never read these (shadow stamp).
--   kairos_rank_score       0-1 composite: freshness + transition type +
--                           recent TP rate + shrunk cell P(TP). Written at
--                           capture by orchestrator._kairos_rank_stamp when
--                           kairos_mode != off. NULL until enough trailing
--                           decided outcomes exist for the symbol.
--   kairos_rank_gate_shadow would-publish if score >= trailing P60 threshold
--                           (shadow_entry_gate). Never enforced live.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS kairos_rank_score double precision,
  ADD COLUMN IF NOT EXISTS kairos_rank_gate_shadow boolean;

COMMENT ON COLUMN nt_signal_sim.kairos_rank_score IS
  'Kairos rank_score shadow stamp (0-1). Measurement only; not a gate input.';
COMMENT ON COLUMN nt_signal_sim.kairos_rank_gate_shadow IS
  'Shadow would-publish from rank_score >= threshold. Never live-enforced.';

-- POST-RUN VERIFICATION:
--   SELECT count(kairos_rank_score), count(*) FROM nt_signal_sim
--   WHERE ts > now() - interval '1 day' AND source = 'sweep-light';
