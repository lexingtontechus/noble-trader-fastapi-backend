-- ═══════════════════════════════════════════════════════════════════════════
-- 189: session_date-leading index — unblock the nightly age-based retention DELETE
--
-- Root cause (2026-10-01, worklog
-- 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.0, "Track A" step 1):
-- renko_retention.py::_delete_old_sessions runs a blanket
--   DELETE FROM nt_renko_bricks WHERE session_date < cutoff
-- with NO symbol filter, nightly (Part VI of orchestrator.py's eod-lookback
-- cycle, 01:17 UTC), enforcing renko_brick_retention_days (default 2 —
-- "today + yesterday," per migration 111's original steady-state design and
-- this view's own cross-repo contract, see
-- nobletradingapp/docs/worklog-renko-bricks-retention-contract.md).
--
-- Checked all 3 pre-existing indexes on nt_renko_bricks — none have
-- session_date as a leading column:
--   idx_nt_renko_bricks_live_grid   (symbol, brick_size, session_date desc, brick_index desc)  [migration 185]
--   idx_nt_bricks_symbol            (symbol, ts desc)
--   idx_nt_renko_bricks_daily_close (symbol, session_date, brick_index desc, ts desc)            [migration 187]
-- A `WHERE session_date < cutoff` predicate with no symbol equality cannot
-- seek on any of these — Postgres must scan start-to-end regardless of
-- which index the planner picks.
--
-- Confirmed live via the Management API (2026-10-01, read-only, no
-- mutation — ran the equivalent SELECT count(*) with the same predicate,
-- NOT the actual DELETE):
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT count(*) FROM nt_renko_bricks WHERE session_date < (CURRENT_DATE - INTERVAL '2 days');
--   -> Index Only Scan using idx_nt_renko_bricks_daily_close
--      (actual time=11.232..5492.650 rows=174160 loops=1)
--      Heap Fetches: 174160
--   Execution Time: 5507.829 ms
-- 5.5s for a read-only COUNT, with 174,160 of ~878k rows matching the
-- predicate and EVERY one needing a heap fetch (the "index" scan still had
-- to visit each matching row directly, because session_date is buried
-- 2nd/3rd in every existing index, not leading). The actual nightly DELETE
-- does the same scan work PLUS the write — very plausibly exceeding
-- whatever timeout applies to that code path, which would explain why
-- renko_brick_retention_days has silently never bound despite running
-- nightly since 2026-09-09.
--
-- Fix: a single-column index with session_date as the ONLY/leading key,
-- so a bare `WHERE session_date < cutoff` can seek directly to the cutoff
-- boundary and scan only matching rows, with no heap fetches needed for
-- rows outside the predicate.
--
-- This is step 1 of Track A (worklog §13.7) — safe, additive, zero
-- dependency on the other Track A steps (nt_daily_close separation,
-- roster-source fix, chunked backlog cleanup), and carries no risk on its
-- own: it only adds read/delete-plan capability, changes no existing
-- query's correctness.
--
-- Plain CREATE INDEX (not CONCURRENTLY — Supabase's SQL Management API
-- runs statements inside an implicit transaction where CONCURRENTLY is
-- rejected; same constraint noted in migrations 177/187). Takes a brief
-- lock but does not rewrite the table.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_nt_renko_bricks_session_date
  ON public.nt_renko_bricks (session_date);

COMMENT ON INDEX public.idx_nt_renko_bricks_session_date IS
  'Supports renko_retention.py::_delete_old_sessions'' blanket '
  'DELETE WHERE session_date < cutoff (no symbol filter) — without this, '
  'the nightly age-based retention prune has no index to seek on and must '
  'scan the full table. Worklog '
  '20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.0/§13.3.';

-- POST-RUN VERIFICATION:
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT count(*) FROM nt_renko_bricks WHERE session_date < (CURRENT_DATE - INTERVAL '2 days');
--   -- expect Index Scan (or Bitmap Index Scan) using
--   -- idx_nt_renko_bricks_session_date, execution time a small fraction
--   -- of the pre-index ~5.5s baseline above.
