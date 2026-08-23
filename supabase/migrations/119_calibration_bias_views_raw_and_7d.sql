-- ═══════════════════════════════════════════════════════════════
-- Migration 119: calibration bias views — raw bias + genuine 7-day window
-- (2026-08-23, worklog/PARAMETER_TUNING_PLAYBOOK.md investigation;
-- full writeup in noble-trader-hermes-plugins/worklog/
-- 20260823_calibration_bias_panel_raw_vs_enforced_mismatch.md)
--
-- Three problems fixed, all in the views that feed the "Calibration bias
-- (7d)" panel in the noble-trader-admin and talaria dashboards:
--
-- 1. RAW vs ENFORCED bias: the old views sourced from nt_signal_sim.p_win,
--    which is the ENFORCED (post-Bayesian-shrink) value — the exact
--    masking mechanism the 2026-08-22 raw-bias-cycle-reset fix addressed
--    on the backend side, but never here. Switched the source to
--    nt_calibration_records, which already carries BOTH predicted_p_win
--    (enforced) and predicted_p_win_raw in the same row (no join needed).
--    Added avg_predicted_p_win_raw / bias_raw / status_raw columns using
--    the raw-band thresholds (0.30 / -0.20) alongside the existing
--    enforced columns (0.10 / -0.10, unchanged) -- additive, so any
--    consumer still selecting only the old columns keeps working.
--
-- 2. Hardcoded threshold drift: the ±0.10 / ±0.30/-0.20 bands are
--    necessarily still hardcoded here (a SQL view cannot read
--    regime_platform.core.settings). MUST mirror
--    settings.EOD_BIAS_ENFORCED_THRESHOLD (enforced) and
--    settings.EOD_BIAS_RAW_OVERCONFIDENT / _UNDERCONFIDENT (raw) --
--    if those are ever changed (e.g. via the Layer 1 auto-tuner, which
--    CAN move EOD_BIAS_ENFORCED_THRESHOLD live without a restart), this
--    view's classification will silently disagree until it is
--    re-migrated. No automated sync exists; this comment is the sync
--    mechanism until one does.
--
-- 3. Per-day, not 7-day: v_eod_calibration_bias_latest used to be
--    DISTINCT ON (symbol) over a per-day-grouped base view -- i.e. ONE
--    DAY's bucket, despite the outer "(7d)" label and the 7-day WHERE
--    filter. Redefined as a genuine GROUP BY symbol over the trailing 7
--    days, with status/status_raw = 'INSUFFICIENT_DATA' (row KEPT, not
--    dropped -- see the 2026-08-20 symbol-count worklog for why silently
--    dropping rows is exactly the bug class to avoid) when n < 20,
--    mirroring settings.EOD_BIAS_MIN_RECORDS.
--
-- v_eod_calibration_bias (the per-day breakdown) is kept for historical/
-- trend purposes and also switched to nt_calibration_records + given the
-- same raw columns, but is NOT the view either dashboard actually queries
-- (confirmed via repo search, 2026-08-23) -- v_eod_calibration_bias_latest
-- is. nt_calibration_records uses outcome IN ('win','loss') (not
-- nt_signal_sim's 'tp_hit'/'sl_hit'/'expired' vocabulary) and its
-- timestamp column is entered_at, not ts.
--
-- Idempotent: CREATE OR REPLACE VIEW.
--
-- Fix-up (2026-08-23, second pass): first apply attempt failed with 42P16
-- ("cannot change name of view column ... to ...") because the new raw
-- columns were interleaved with the pre-existing ones. CREATE OR REPLACE
-- VIEW can only APPEND columns, never reorder or insert mid-list. Both
-- SELECT lists below now keep the original 103/115 column order (day,
-- symbol, avg_predicted_p_win, realized_win_rate, bias, status) untouched
-- and append avg_predicted_p_win_raw / n / bias_raw / status_raw after it.
-- ═══════════════════════════════════════════════════════════════

-- ─── 1. v_eod_calibration_bias — per-day breakdown (historical/trend) ──────

CREATE OR REPLACE VIEW v_eod_calibration_bias AS
SELECT
  DATE(entered_at)                                                    AS day,
  symbol,
  AVG(predicted_p_win)                                                AS avg_predicted_p_win,
  AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)                AS realized_win_rate,
  (AVG(predicted_p_win)
     - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END))          AS bias,
  CASE
    WHEN (AVG(predicted_p_win) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) > 0.10 THEN 'OVERCONFIDENT'
    WHEN (AVG(predicted_p_win) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) < -0.10 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END                                                                 AS status,
  -- New columns appended AFTER the pre-existing ones (migrations 103/115) —
  -- CREATE OR REPLACE VIEW cannot reorder or insert columns mid-list, only
  -- append (42P16). See migration 119 fix-up note below.
  AVG(predicted_p_win_raw)                                            AS avg_predicted_p_win_raw,
  COUNT(*)                                                            AS n,
  (COALESCE(AVG(predicted_p_win_raw), AVG(predicted_p_win))
     - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END))          AS bias_raw,
  CASE
    WHEN (COALESCE(AVG(predicted_p_win_raw), AVG(predicted_p_win)) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) >= 0.30 THEN 'OVERCONFIDENT'
    WHEN (COALESCE(AVG(predicted_p_win_raw), AVG(predicted_p_win)) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) <= -0.20 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END                                                                 AS status_raw
FROM nt_calibration_records
WHERE outcome IN ('win', 'loss')
  AND entered_at >= now() - interval '7 days'
GROUP BY DATE(entered_at), symbol
ORDER BY day DESC;

-- ─── 2. v_eod_calibration_bias_latest — genuine 7-day rolling, one row/symbol ──
-- This is the view both dashboards actually query. "latest" now means
-- "as of now, over the trailing 7 days" (matching the panel's own "(7d)"
-- label), not "the most recent single day."

CREATE OR REPLACE VIEW v_eod_calibration_bias_latest AS
SELECT
  now()::date                                                         AS day,
  symbol,
  AVG(predicted_p_win)                                                AS avg_predicted_p_win,
  AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)                AS realized_win_rate,
  (AVG(predicted_p_win)
     - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END))          AS bias,
  CASE
    WHEN COUNT(*) < 20 THEN 'INSUFFICIENT_DATA'
    WHEN (AVG(predicted_p_win) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) > 0.10 THEN 'OVERCONFIDENT'
    WHEN (AVG(predicted_p_win) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) < -0.10 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END                                                                 AS status,
  -- New columns appended AFTER the pre-existing ones (migration 115) —
  -- CREATE OR REPLACE VIEW cannot reorder or insert columns mid-list, only
  -- append (42P16).
  AVG(predicted_p_win_raw)                                            AS avg_predicted_p_win_raw,
  COUNT(*)                                                            AS n,
  (COALESCE(AVG(predicted_p_win_raw), AVG(predicted_p_win))
     - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END))          AS bias_raw,
  CASE
    WHEN COUNT(*) < 20 THEN 'INSUFFICIENT_DATA'
    WHEN (COALESCE(AVG(predicted_p_win_raw), AVG(predicted_p_win)) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) >= 0.30 THEN 'OVERCONFIDENT'
    WHEN (COALESCE(AVG(predicted_p_win_raw), AVG(predicted_p_win)) - AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END)) <= -0.20 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END                                                                 AS status_raw
FROM nt_calibration_records
WHERE outcome IN ('win', 'loss')
  AND entered_at >= now() - interval '7 days'
GROUP BY symbol;

-- Grants unchanged (already applied to both views by migrations 107/115) --
GRANT SELECT ON v_eod_calibration_bias TO anon;
GRANT SELECT ON v_eod_calibration_bias_latest TO anon;

COMMENT ON VIEW v_eod_calibration_bias IS
  'Per-day predicted-vs-realized calibration bias, both enforced (bias/status) and raw pre-enforcement (bias_raw/status_raw) — see migration 119.';
COMMENT ON VIEW v_eod_calibration_bias_latest IS
  'Rolling 7-day (not per-day) predicted-vs-realized calibration bias per symbol, both enforced and raw, with INSUFFICIENT_DATA below 20 records — see migration 119.';
