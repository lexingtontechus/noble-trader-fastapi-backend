-- Migration 164: qualified_geometry_shadow stamp (B2 complete)
--
-- light_publish_geometry_gate shadow (2026-09-20 worklog B) logs
-- "SHADOW would reject" but never stamped the sim row, so the B5
-- reject-rate (would_reject / would_qualify) could only be grepped
-- from logs. Same pattern as bracket_gate_shadow (m154).

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS qualified_geometry_shadow BOOLEAN;

COMMENT ON COLUMN nt_signal_sim.qualified_geometry_shadow IS
  'True when live gates would qualify but light_publish_geometry_gate (shadow) would veto: geometry_risk_hat > dynamic_atr_risk_cap OR (p_win_bracket scored AND < min_p_win_bracket). qualified stays true while flag is shadow. B5 review: reject rate = true / (true+false among would-qualify). If rate >~0.70 tune dynamic_atr_risk_cap rather than flipping live.';

CREATE INDEX IF NOT EXISTS idx_signal_sim_qualified_geometry_shadow
  ON nt_signal_sim (ts DESC)
  WHERE qualified_geometry_shadow IS TRUE;
