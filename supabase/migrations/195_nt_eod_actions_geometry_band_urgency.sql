-- Migration 195: add 'geometry_band_urgency' to nt_eod_actions.action CHECK
-- (2026-10-04, weekend investigation — geometry band urgency wired live).
--
-- geometry_band_urgency.py re-computes a bucket's reachability stats from
-- recent resolved signals when its calibration bias has drifted, and logs the
-- action to nt_eod_actions. That action name was NOT in the CHECK constraint
-- (last extended by migration 158), so every insert failed 23514 and the
-- re-computation was silently unlogged.
--
-- Live distinct actions as of migration 158 (Supabase):
--   emergency_heavy_sweep, bayesian_penalty, kelly_reduction,
--   parameter_decay_check, degeneracy_breaker, stagnation_kill_switch,
--   regime_shift_alert, geometry_tuning_sweep
-- This migration allows the full union plus geometry_band_urgency.
-- DROP then ADD (same pattern as 116/118/158) so the new list is authoritative.

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
    'geometry_tuning_sweep',
    'geometry_band_urgency'
  ]::text[]));

COMMENT ON TABLE nt_eod_actions IS
  'Audit ledger for EOD self-correction / geometry actions (emergency_heavy_sweep, bayesian_penalty, kelly_reduction, parameter_decay_check, degeneracy_breaker, stagnation_kill_switch, regime_shift_alert, geometry_tuning_sweep, geometry_band_urgency).';
