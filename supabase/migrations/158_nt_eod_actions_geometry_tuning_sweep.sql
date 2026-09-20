-- 2026-09-20: add geometry_tuning_sweep to nt_eod_actions.action CHECK
-- (worklog 20260919_INTRADAY_GEOMETRY_TUNING_SCOPE.md +
--  20260920_QUALIFIED_QUALITY_GATES_MACRO_HYGIENE.md)
--
-- First attempt (158) FAILED 23514: ADD CONSTRAINT rejected existing rows
-- because the new list omitted `stagnation_kill_switch` (present in DB
-- from migration 116). Live distinct actions (Supabase 2026-09-20):
--   regime_shift_alert, parameter_decay_check, emergency_heavy_sweep,
--   kelly_reduction, stagnation_kill_switch
-- Code also writes: bayesian_penalty, degeneracy_breaker,
--   geometry_tuning_sweep
-- This migration allows the full union. DROP then ADD (same as 116/118).

ALTER TABLE nt_eod_actions
  DROP CONSTRAINT IF EXISTS nt_eod_actions_action_check;

ALTER TABLE nt_eod_actions
  ADD CONSTRAINT nt_eod_actions_action_check
  CHECK (action = ANY (ARRAY[
    'emergency_heavy_sweep',
    'bayesian_penalty',
    'kelly_reduction',
    'parameter_decay_check',
    'degeneracy_breaker',
    'stagnation_kill_switch',
    'regime_shift_alert',
    'geometry_tuning_sweep'
  ]::text[]));

COMMENT ON TABLE nt_eod_actions IS
  'Audit ledger for EOD self-correction / geometry tuning actions (emergency_heavy_sweep, bayesian_penalty, kelly_reduction, parameter_decay_check, degeneracy_breaker, stagnation_kill_switch, regime_shift_alert, geometry_tuning_sweep).';
