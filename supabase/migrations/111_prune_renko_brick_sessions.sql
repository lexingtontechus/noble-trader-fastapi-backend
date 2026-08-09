-- ═══════════════════════════════════════════════════════════════════════════
-- 111: Renko brick hygiene — prune stale sessions (old global series + old days)
--
-- Symptom (2026-08-09): the Talaria/admin renko chart appeared FROZEN at
-- 2026-08-07 (BTCUSD/XAUUSD last brick ~14:40 UTC) while sweeps were live.
--
-- Root cause: nt_renko_bricks carries one row per (symbol, brick_index,
-- session_date). The pre-refactor capture wrote a GLOBAL index series
-- (up to 8721) under session_date 2026-08-07; the current per-sweep rebuild
-- writes into CURRENT_DATE's session. Charts that ordered by brick_index.desc
-- (no session_date) therefore surfaced the old Aug-7 global leftovers and
-- looked frozen even though new sessions (Aug 8/9) were being generated.
--
-- Fixes:
--   1. Plugin chart queries now order by session_date.desc,brick_index.desc
--      (deployed 2026-08-09) so they always read the CURRENT session.
--   2. This migration prunes sessions older than yesterday, which also drops
--      the stale pre-refactor global series and bounds table growth.
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM nt_renko_bricks
WHERE session_date < (CURRENT_DATE - INTERVAL '1 day');

-- Keep at most 2 sessions (today + yesterday) so the Markov series (up to
-- 200 bricks) stays feedable and the chart's 10-brick window is always the
-- latest session.

-- Verify (expect 0 rows after a fresh sweep run for each active symbol):
--   SELECT session_date, count(*), max(brick_index)
--   FROM nt_renko_bricks
--   GROUP BY session_date
--   ORDER BY session_date DESC
--   LIMIT 5;
