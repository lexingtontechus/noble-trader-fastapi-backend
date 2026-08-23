-- ═══════════════════════════════════════════════════════════════
-- Migration 118: add 'regime_shift_alert' to nt_eod_actions.action CHECK
-- (2026-08-23, Layer 2 — worklog/PARAMETER_TUNING_PLAYBOOK.md §2.5.3)
--
-- Layer 2 wires EVEngine.detect_regime_shift() (previously computed
-- nowhere in the live codebase, confirmed via grep) into
-- SweepOrchestrator._process_symbol(), and writes a 'regime_shift_alert'
-- row to nt_eod_actions when the shift magnitude crosses
-- settings.REGIME_SHIFT_ALERT_THRESHOLD. Reuses nt_eod_actions (rather
-- than a new table) per the design's stated option — same audit-trail
-- shape, same RLS, no new table needed.
--
-- ``bias`` stores the shift magnitude (0-1 total-variation distance,
-- not a calibration bias) for sortability/consistency with the other
-- action types; the full shift detail (direction, confidence,
-- prev_label, curr_label) lives in ``detail`` JSONB.
--
-- This is a DETECTION-ONLY step: it flags the shift for a human/agent to
-- review Tier B/C parameters (EV blend weights, gate thresholds) against
-- fresh live evidence. It does NOT itself change any parameter — see
-- migration 116/117's lesson: mirror the same pattern of widening this
-- one CHECK constraint as new action types are added, rather than a
-- proliferation of near-identical tables.
-- ═══════════════════════════════════════════════════════════════

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
    'regime_shift_alert'
  ]));

COMMENT ON TABLE nt_eod_actions IS
  'Audit ledger for EOD self-correction actions (emergency_heavy_sweep, bayesian_penalty, kelly_reduction, parameter_decay_check, degeneracy_breaker, stagnation_kill_switch, regime_shift_alert), enabling traceability of system interventions per symbol over time.';
