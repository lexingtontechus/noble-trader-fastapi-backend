-- 174_variable_kelly_shadow.sql  (2026-09-27)
-- Shadow stamps for variable binary Kelly (Kairos event-time tiers).
-- Additive only: qualification, delivery and live sizing never read these
-- until a separate promote decision (same rule as kairos_rank_score).
-- Historical-mu Kelly remains in kelly_f / effective_kelly for research;
-- it is no longer a publish gate (changelog 20260927_EV_ENGINE_KELLY_GATE).

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS kelly_binary_f double precision,
  ADD COLUMN IF NOT EXISTS kelly_context_mult double precision,
  ADD COLUMN IF NOT EXISTS kelly_effective_shadow double precision;

COMMENT ON COLUMN nt_signal_sim.kelly_binary_f IS
  'Shadow binary Kelly f* from p_win x reward_risk (fractional). Measurement only.';
COMMENT ON COLUMN nt_signal_sim.kelly_context_mult IS
  'Shadow size multiplier from Kairos transition type + event-time freshness (+ vol tier). Never regime age.';
COMMENT ON COLUMN nt_signal_sim.kelly_effective_shadow IS
  'Shadow size = kelly_binary_f * context_mult (clipped). Not live position size.';

-- POST-RUN VERIFICATION:
--   SELECT count(kelly_binary_f), count(kelly_effective_shadow), count(*)
--   FROM nt_signal_sim WHERE ts > now() - interval '1 day' AND source = 'sweep-light';
