-- Migration 166: qualified_geometry_shadow → geometry-risk only (B5 split)
--
-- 2026-09-24 (worklog 20260924_B5_VETO_SPLIT_SCOPE.md): the light B gate
-- dropped its `p_win_bracket < min_p_win_bracket` clause. Bracket gating is
-- owned by R2 (p_win_bracket_mode). Both metrics derive from the same
-- nt_bracket_reachability.pct_expired (corr ≈ -0.87 live), so risk-over
-- without bracket-under was ~0.2% of scored rows — a per-cause column was not
-- needed. Cause is derivable in SQL from geometry_risk_hat + bracket_gate_shadow.
--
-- Metadata only: no schema change, no data change.
-- Rows stamped before 2026-09-24 used the OR (risk OR bracket) semantics;
-- B5 SQL derives risk from geometry_risk_hat so it reads both eras the same way.

COMMENT ON COLUMN nt_signal_sim.qualified_geometry_shadow IS
  'Light B gate shadow stamp among would-qualify rows. Since 2026-09-24: true = geometry_risk_hat > dynamic_atr_risk_cap (would veto); false = risk scored and within cap; NULL = not would-qualify, gate off/live, or risk unscored (fail-open). Before 2026-09-24 true also included p_win_bracket < min_p_win_bracket, and false could be stamped with risk unscored — derive from geometry_risk_hat for cross-era B5 analysis. Bracket veto lives in bracket_gate_shadow (R2).';
