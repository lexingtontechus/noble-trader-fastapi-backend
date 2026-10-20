-- 216_calibration_published_cutover_predicate.sql
--
-- 2026-10-19 — restore the era/cutover predicate to the published-book
-- calibration view (W-C3, P0-3 of the 2026-10-19 weaknesses evaluation,
-- kairos-review-worklogs/20261019_KAIROS_WEAKNESSES_EVALUATION.md).
--
-- WHY
-- ---
-- Migration 209 defined v_calibration_records_published with ONLY the
-- qualified=true filter and dropped the era/cutover predicate that every
-- other calibration reader applies (v_calibration_records_current and
-- v_calibration_records_tiered, migration 192). Result: three evidence
-- populations disagree — the tiered/plain bias paths exclude pre-cutover
-- (close-walk-basis) rows while the published-book standard MIXES ERAS:
-- rows resolved under the OLD close-walk semantics ("a 1h CLOSE passed TP")
-- sit next to intrabar rows ("high/low touched TP first") in the same
-- bias/Brier window. That re-labels a substantial share of rows (survivable
-- base rate 0.2728 close-walk vs 0.057-0.069 intrabar), so the published-
-- book bias/Brier/enforcement numbers are computed against a mixed-semantics
-- population — manufactured over/underconfidence verdicts.
--
-- The repo's own blueprint (calibration_reframe.py) names the same defect
-- class: "Calibration measures a different population than the one that
-- loses money." Restoring the era predicate here makes the published-book
-- population at least SEMANTICALLY homogeneous (one resolution basis) —
-- the expiry-inclusive 3-outcome scoring remains the PBM posterior's job
-- (it already learns all three outcome classes per cell).
--
-- WHAT THIS DOES
-- ──────────────
-- CREATE OR REPLACE VIEW with the SAME column list and qualified=true
-- filter as 209, plus the migration-192 cutover predicate on the SAME
-- resolution-time basis the other two views key on:
--
--   NOT EXISTS (SELECT 1 FROM nt_calibration_cutover k
--               WHERE k.id = 1
--                 AND COALESCE(s.resolved_at, c.entered_at) < k.cutover_at)
--
-- Inert while nt_calibration_cutover is empty (predicate vacuously true):
-- identical row set to 209 until the marker exists, exactly like 192 was.
-- VIEW ONLY — no table change, no backfill, no DELETE; pre-cutover rows
-- stay in nt_calibration_records as the historical record.

CREATE OR REPLACE VIEW v_calibration_records_published AS
SELECT c.id,
       c.signal_id,
       c.symbol,
       c.venue,
       c.predicted_p_win,
       c.predicted_p_win_raw,
       c.calibration_brier,
       c.outcome,
       c.aggression,
       c.entry_price,
       c.exit_price,
       c.entered_at,
       c.closed_at,
       c.actual_pnl,
       c.error_source
FROM nt_calibration_records c
JOIN nt_signal_sim s ON s.signal_id = c.signal_id
WHERE s.qualified = true
  AND NOT EXISTS (
    SELECT 1 FROM nt_calibration_cutover k
    WHERE k.id = 1
      AND COALESCE(s.resolved_at, c.entered_at) < k.cutover_at
  );

COMMENT ON VIEW v_calibration_records_published IS
    'Published-book calibration records: nt_calibration_records joined to
    nt_signal_sim on signal_id, qualified=true only. The scorecard/bias
    STANDARD (what users experience). Since migration 216 (2026-10-19) it
    also applies the migration-192 post-cutover filter on the same
    resolved_at basis as v_calibration_records_current / _tiered, so the
    published-book population no longer mixes the close-walk and intrabar
    resolution eras. Full population remains in nt_calibration_records as
    a diagnostic stratifier. 2026-10-12 (209) / 2026-10-19 (216).';

NOTIFY pgrst, 'reload schema';
