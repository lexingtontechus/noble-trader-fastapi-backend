-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 109: Retro-fix 1970 brick timestamps in nt_renko_bricks
--
-- WHY: the pre-fix sweep capture (before ee1a421) wrote brick `ts` as the
-- RenkoBrickEngine's auto-incremented epoch (1,2,3...) → rendered as
-- 1970-01-01 in the table. The sweep_ts fix (ee1a421) makes NEW writes use
-- real timestamps, but the ~408 stale rows keep the 1970 date.
--
-- FIX: anchor stale ts to the row's session_date (2026-08-07 = the day the
-- bricks were captured), spread across that day by brick_index so the chart's
-- time axis is monotonic. Rows whose ts is already sane are untouched.
--
-- IDEMPOTENT: WHERE ts < '2000-01-01' only touches stale rows; re-running is
-- a no-op.
-- ═══════════════════════════════════════════════════════════════════════════

UPDATE nt_renko_bricks
SET ts = session_date + ((brick_index % 96) * INTERVAL '15 minutes')
WHERE ts < '2000-01-01';

-- POST-RUN VERIFICATION:
--   SELECT count(*) FROM nt_renko_bricks WHERE ts < '2000-01-01';
--   Expect 0.
--   SELECT symbol, brick_index, ts FROM nt_renko_bricks
--     WHERE symbol = 'XAUUSD' ORDER BY brick_index LIMIT 5;
--   Expect ts values within 2026-08-07, increasing with brick_index.
-- ═══════════════════════════════════════════════════════════════════════════
