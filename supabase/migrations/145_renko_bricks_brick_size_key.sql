-- ═══════════════════════════════════════════════════════════════════════════
-- 145: Renko brick hygiene — brick_size discriminator + one-time prune
--
-- Root cause (2026-09-09, worklog
-- 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7 "Finding 2"):
-- nt_renko_bricks' unique key was (symbol, brick_index, session_date) with
-- NO brick_size discriminator. Heavy sweep now re-optimizes brick_size
-- DAILY (migration 142, heavy_sweep_daily_cadence), so a symbol's grid
-- geometry can change every day; every light-sweep tick rebuilds the ENTIRE
-- brick series from scratch and upserts it. Without brick_size in the key,
-- a re-sweep at a NEW brick_size silently overwrote whatever row already
-- occupied (symbol, brick_index, session_date) from the OLD brick_size (up
-- to the shorter series' length), while any longer-index rows from the old
-- grid were never touched and sat there forever — orphaned historical
-- grids accumulating with no retention. Confirmed live 2026-09-09:
-- 129,583 rows across 14 symbols, oldest session_date a full month back
-- (2026-08-08); AAPL alone carries 8+ distinct historical brick_size
-- values across that month.
--
-- Fix:
--   1. Add brick_size to the unique key. This is a CORRECTNESS fix (a
--      re-sweep at a new brick_size can no longer silently overwrite a
--      different grid's row) — it is NOT a growth fix by itself; each
--      (symbol, brick_size) pair now safely owns its own index/session
--      space, which on its own would let orphaned grids accumulate even
--      faster. Adding the column to the key is safe unconditionally: the
--      OLD 3-column key already guaranteed uniqueness, so widening it to 4
--      columns cannot introduce a conflict on existing data.
--   2. One-time prune: delete everything older than 2 days (matching the
--      same cutoff feature_flags.yaml's new renko_brick_retention_days
--      default uses going forward) — mirrors migration 111's own
--      precedent for this exact table ("Keep at most 2 sessions (today +
--      yesterday)..."). The recurring policy (regime_platform/
--      sweep_orchestrator/renko_retention.py, run nightly from
--      orchestrator.py's eod-lookback cycle, Part VI) additionally prunes
--      rows whose brick_size no longer matches the symbol's CURRENT
--      config/brick_params.yaml value — that comparison needs the live
--      YAML file (not visible to SQL), so it lives in application code,
--      not this migration, following the same eod_retention.py precedent
--      already established in this codebase (Python-scheduled retention,
--      not pg_cron — nothing in this project's migration history uses
--      pg_cron).
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_renko_bricks
    DROP CONSTRAINT IF EXISTS nt_renko_bricks_symbol_brick_index_session_date_key;

ALTER TABLE nt_renko_bricks
    ADD CONSTRAINT nt_renko_bricks_symbol_brick_index_session_date_brick_size_key
    UNIQUE (symbol, brick_index, session_date, brick_size);

DELETE FROM nt_renko_bricks
WHERE session_date < (CURRENT_DATE - INTERVAL '2 days');

-- Verify (expect 0 rows, and every symbol showing exactly ONE distinct
-- brick_size per recent session_date once the light sweep has re-swept):
--   SELECT symbol, session_date, count(DISTINCT brick_size) AS grids
--   FROM nt_renko_bricks
--   GROUP BY symbol, session_date
--   HAVING count(DISTINCT brick_size) > 1
--   ORDER BY symbol, session_date DESC;
