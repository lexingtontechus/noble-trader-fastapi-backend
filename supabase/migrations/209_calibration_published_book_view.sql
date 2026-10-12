-- 209_calibration_published_book_view.sql
--
-- 2026-10-12 — scorecard published-book standard (worklog
-- 20261012_POOLED_ATR_AND_SCORECARD_FILTER.md).
--
-- WHY
-- ---
-- The calibration bias / scorecard reads nt_calibration_records, which the
-- resolver feeds from EVERY signal (outcome IS NULL, no qualified filter) and
-- log_signal_sim writes for every tick. Validated 2026-10-12: 962 suppressed
-- : 38 published resolved rows. The bias is dominated by signals we never
-- published. The PUBLISHED book (qualified=true) is the standard users
-- experience; the full population stays as a diagnostic stratifier.
--
-- VIEW ONLY (no table, no INSERT, no backfill) — joins nt_calibration_records
-- to nt_signal_sim on signal_id and keeps qualified=true rows. Same columns
-- the bias readers select (predicted_p_win, outcome, entered_at, closed_at,
-- signal_id) so fetch_calibration_bias can point at it unchanged.

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
WHERE s.qualified = true;

COMMENT ON VIEW v_calibration_records_published IS
    'Published-book calibration records: nt_calibration_records joined to
    nt_signal_sim on signal_id, qualified=true only. The scorecard/bias
    STANDARD (what users experience). Full population remains in
    nt_calibration_records as a diagnostic stratifier. 2026-10-12.';

NOTIFY pgrst, 'reload schema';
