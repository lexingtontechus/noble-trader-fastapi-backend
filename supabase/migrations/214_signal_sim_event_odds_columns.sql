-- Migration 214: nt_signal_sim — event-odds capture stamps
-- Nullable columns stamped regardless of mode (migration 124's
-- macro_label/macro_strength precedent: capture first so the evidence
-- layer can accumulate, apply never until a flag says so).
-- Design: docs/scopes/20261019_MACRO_EVENT_ODDS_LAYER_ASSESSMENT.md §6.2

ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS hours_to_event DOUBLE PRECISION;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS event_surprise_potential DOUBLE PRECISION;
