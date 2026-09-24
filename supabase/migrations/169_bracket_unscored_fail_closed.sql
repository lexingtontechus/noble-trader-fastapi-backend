-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 169: document fail-closed suppress_reason 'bracket_unscored'
--
-- Methodology worklog 20260924 §10 #1 (operator decision): live QUALIFIED
-- book is fail-closed on unscored p_win_bracket. Research capture still
-- writes the row. Additive comment-only + optional index helper; no CHECK
-- change (suppress_reason is free text).
--
--   suppress_reason values after this migration:
--     cooldown_open | cooldown_time | content_dedup |
--     bracket_gate | bracket_unscored | geometry_gate
-- ═══════════════════════════════════════════════════════════════════════════════

COMMENT ON COLUMN nt_signal_sim.suppress_reason IS
  'Kairos G2: why a gate-passing signal was not published: cooldown_open | cooldown_time | content_dedup | bracket_gate | bracket_unscored (fail-closed, no scored p_win_bracket) | geometry_gate.';

-- Review helper: fail-closed volume by symbol (B/ops pulse).
CREATE OR REPLACE VIEW v_bracket_unscored_rejects AS
SELECT
  symbol,
  signal,
  ts,
  regime,
  p_win_bracket,
  bracket_bucket,
  bracket_vol_tier,
  qualified,
  gate_outcome,
  suppress_reason
FROM nt_signal_sim
WHERE suppress_reason = 'bracket_unscored'
   OR (gate_outcome = 'gate_fail' AND failed_gates LIKE '%p_win_bracket_ok%');
