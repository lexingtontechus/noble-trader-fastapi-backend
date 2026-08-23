-- ═══════════════════════════════════════════════════════════════
-- Migration 116: widen nt_eod_actions.action CHECK constraint
-- (2026-08-23, discovered while building the tuning registry)
--
-- BUG: the CHECK constraint added in migration 102/104 only allows
--   'emergency_heavy_sweep', 'bayesian_penalty', 'kelly_reduction',
--   'parameter_decay_check'
-- Two action types added to the Python code SINCE that constraint were
-- never added to it, so every insert of them has been silently failing
-- (nt_eod_actions writes are best-effort — the exception is caught and
-- logged, never raised — so nothing surfaced this until it was checked
-- directly against live data):
--
--   - 'degeneracy_breaker' (eod_correction.py, L3 fix, 2026-08-21) — the
--     circuit-breaker for the "p_win=0 -> no records -> no correction ->
--     stuck forever" deadlock. CONFIRMED via live Supabase query
--     (2026-08-23): 0 rows with this action exist, despite the code path
--     having run for 2+ days. The mechanism it exists to fix has never
--     actually fired.
--   - 'stagnation_kill_switch' (eod_correction.py / calibration_enforcement.py,
--     fix #3c, 2026-08-22 audit, commit 3b37e89) — would hit the same wall
--     once deployed to Studio.
--
-- Purely additive (widens the allowed set) — no data loss, no rewrite of
-- existing rows.
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
    'stagnation_kill_switch'
  ]));

COMMENT ON TABLE nt_eod_actions IS
  'Audit ledger for EOD self-correction actions (emergency_heavy_sweep, bayesian_penalty, kelly_reduction, parameter_decay_check, degeneracy_breaker, stagnation_kill_switch), enabling traceability of system interventions per symbol over time.';
