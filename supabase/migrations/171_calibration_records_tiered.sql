-- ═══════════════════════════════════════════════════════════════════════════════
-- 171_calibration_records_tiered.sql  (2026-09-26, branch kairos)
--
-- View only: no new table, no new column, no insert or write-path change.
-- Scope: worklog/kairos/20260926_CALIBRATION_EVIDENCE_POOL.md; Kairos V3
-- scope §14.3 (evidence-tier split).
--
-- v_calibration_records_tiered = nt_calibration_records + the gate tier of
-- the signal that produced each record, joined on signal_id (the
-- nt_signal_sim primary key, so the join is an index lookup). Every
-- nt_calibration_records column passes through unchanged, so a reader can
-- switch table → view and add one filter:
--   evidence_tier ∈ qualified | suppressed | gate_fail | unknown
--   tier_src      ∈ stamp (gate_outcome, migration 168, since 2026-09-24)
--                 | recon (reconstructed from stored gate inputs)
--                 | missing (no nt_signal_sim row: pre-signal_id records or
--                   retention-deleted)
--
-- Reconstruction mirrors scripts/kairos_weekly_check.py reconstruct_tier
-- (validated 2026-09-25: tier agreement 1.000 on 910 stamped rows):
--   qualified → 'qualified'; else regime_conf ≥ 0.60 AND effective_kelly ≥
--   0.02 AND ev > 0 AND tp/sl bricks ≥ 1.5 AND coalesce(p_win_dir, p_win) ≥
--   0.50 → 'suppressed'; else 'gate_fail'. NULL inputs → gate_fail (the same
--   as the pandas NaN comparisons). The thresholds are hardcoded here (SQL
--   can't read config/feature_flags.yaml). They only matter for rows with
--   no stamp, and that set stops growing now that every light sweep stamps
--   gate_outcome. The ::numeric cast avoids integer division on the brick
--   columns.
--
-- security_invoker: the caller's RLS applies (the service role in the
-- orchestrator). This avoids a SECURITY DEFINER view that bypasses RLS.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW v_calibration_records_tiered
WITH (security_invoker = on) AS
SELECT
  c.*,
  CASE
    WHEN s.gate_outcome IN ('qualified', 'suppressed', 'gate_fail') THEN s.gate_outcome
    WHEN s.signal_id IS NULL THEN 'unknown'
    WHEN s.qualified THEN 'qualified'
    WHEN s.regime_conf >= 0.60
     AND s.effective_kelly >= 0.02
     AND s.ev > 0
     AND s.tp_bricks::numeric / NULLIF(s.sl_bricks, 0) >= 1.5
     AND coalesce(s.p_win_dir, s.p_win) >= 0.50 THEN 'suppressed'
    ELSE 'gate_fail'
  END AS evidence_tier,
  CASE
    WHEN s.gate_outcome IN ('qualified', 'suppressed', 'gate_fail') THEN 'stamp'
    WHEN s.signal_id IS NULL THEN 'missing'
    ELSE 'recon'
  END AS tier_src,
  s.source AS sim_source
FROM nt_calibration_records c
LEFT JOIN nt_signal_sim s ON s.signal_id = c.signal_id;

COMMENT ON VIEW v_calibration_records_tiered IS
  'nt_calibration_records + evidence_tier (qualified|suppressed|gate_fail|unknown) of the producing signal, joined on signal_id. tier_src: stamp (gate_outcome) | recon (validated reconstruction) | missing. Read by fetch_calibration_bias(_raw)(evidence_tiers=...) for the calibration_evidence_pool_mode shadow/live bias (migration 171, Kairos V3 §14.3).';
