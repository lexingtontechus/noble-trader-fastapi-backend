-- =============================================================================
-- 196: nt_renko_bricks.session_date mis-stamping — diagnosis + safe default fix
-- =============================================================================
-- FINDING (2026-10-04, live probe on NobleTrading / pcvscowltlrxzgxjurcr)
-- ──────────────────────────────────────────────────────────────────────────
-- `nt_renko_bricks.session_date` is stamped with the SWEEP RUN date, not the
-- brick's real trading day. Root cause: `log_renko_bricks`
-- (regime_platform/sweep_orchestrator/supabase_client.py:680-816) only sets
-- `row["session_date"]` when the caller passes one; the orchestrator call site
-- (orchestrator.py:5309-5315) does NOT pass it, so the column default
-- `CURRENT_DATE` (server-local) wins — the date the sweep ran, applied to
-- EVERY brick in the batch, even though a single sweep rebuilds the whole
-- series from a multi-week price window.
--
-- Live impact: 94,590 of 95,871 rows (98.7%) have session_date <> ts::date.
--   session_date=2026-10-04 → 76,896 rows whose real ts::date spans 32 days
--   session_date=2026-10-03 →  8,921 rows, 29 real days inside
--   session_date=2026-10-02 →    301 rows, 10 real days inside
--   session_date=2026-10-05 →  9,753 rows, 14 real days inside  (future-dated)
--
-- This is why v_talaria_daily_close (nobletradingapp, migration 0011) collapsed
-- ~30 trading days into 3 — see nobletradingapp docs/scope-correlation-daily-close-repoint.md.
-- That consumer is now repointed to nt_daily_close (nobletradingapp migration
-- 0027), so correlation no longer depends on this. But v_talaria_renko (the
-- dashboard Renko chart) still orders by session_date, so the mis-stamp affects
-- it too.
--
-- WHY THIS MIGRATION DOES NOT REWRITE HISTORY
-- ──────────────────────────────────────────────────────────────────────────
-- A naive `UPDATE nt_renko_bricks SET session_date = ts::date` CANNOT run: the
-- unique index `nt_renko_bricks_symbol_brick_index_session_date_brick_size_key`
-- is on (symbol, brick_index, session_date, brick_size). Correcting session_date
-- makes 9,313 groups collide (22,136 rows) — because the table already holds
-- many generations of the same (symbol, brick_index, brick_size) at different
-- mis-stamped dates. Of those colliding groups, 7,621 contain rows that differ
-- in ts/price — i.e. genuinely different bricks, NOT duplicates. Resolving the
-- collision by keeping one row would silently DELETE real bricks. That is a
-- reviewed product/data decision, not a mechanical migration.
--
-- The real fix is in the WRITER, not a migration:
--   1. supabase_client.py log_renko_bricks: set row["session_date"] from each
--      brick's own `ts` (the brick dict already carries a real epoch when
--      has_real_timestamps=True) instead of falling back to CURRENT_DATE.
--   2. orchestrator.py:5309 — the call site should pass real per-brick dates, or
--      log_renko_bricks should derive them. (Sweeps that rebuild a multi-week
--      window then write rows across MANY session_dates in one call — correct,
--      but only once the writer stops using one date for the whole batch.)
--   3. Decide the historical-rewrite policy (see the recipe below) separately.
--
-- See also: noble-trader-fastapi-backend worklog 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md,
-- nobletradingapp docs/worklog-renko-chart-grid-mismatch-and-orphan-purge.md.
--
-- IDEMPOTENT: SET DEFAULT is a metadata-only change (no row rewrite).
-- =============================================================================

-- ─── Part A (SAFE, applies now): deterministic UTC default ──────────────────
-- The default is only the fallback when the writer omits session_date (the
-- current bug). CURRENT_DATE is server-local; pin it to UTC so the fallback at
-- least matches ts::date for a live sweep instead of drifting with the host
-- timezone. This does NOT fix the multi-day-span problem — only the writer can —
-- but it removes the timezone nondeterminism.
ALTER TABLE public.nt_renko_bricks
  ALTER COLUMN session_date SET DEFAULT ((now() AT TIME ZONE 'utc')::date);

COMMENT ON COLUMN public.nt_renko_bricks.session_date IS
  'Trading session date. SHOULD equal (ts AT TIME ZONE ''utc'')::date, but as of '
  '2026-10-04 ~99% of rows are mis-stamped with the sweep RUN date (writer bug — '
  'see migration 196 header). Consumers needing correct daily closes should read '
  'nt_daily_close (kept correct by daily_close_snapshot.py), not this column.';

-- =============================================================================
-- Part B (DESTRUCTIVE — NOT EXECUTED, needs a reviewed policy)
-- ──────────────────────────────────────────────────────────────────────────
-- When the collision policy is decided, the historical correction looks like
-- this. Do NOT run as-is; the ON CONFLICT-style dedup here keeps the latest
-- insert per corrected key, which DELETES the other colliding rows (7,621
-- groups of which are genuinely different bricks).
--
--   -- 1. See the blast radius first:
--   --   SELECT count(*) FROM (
--   --     SELECT symbol, brick_index, ts::date, brick_size, count(*)
--   --     FROM nt_renko_bricks GROUP BY 1,2,3,4 HAVING count(*) > 1) t;
--
--   -- 2. Corrected-date UPDATE, deleting all but the latest row per key:
--   --   WITH ranked AS (
--   --     SELECT brick_id,
--   --            (ts AT TIME ZONE 'utc')::date AS d,
--   --            row_number() OVER (
--   --              PARTITION BY symbol, brick_index, (ts AT TIME ZONE 'utc')::date, brick_size
--   --              ORDER BY brick_id DESC) AS rn
--   --     FROM nt_renko_bricks)
--   --   DELETE FROM nt_renko_bricks b USING ranked r
--   --   WHERE b.brick_id = r.brick_id AND r.rn > 1;
--   --   UPDATE nt_renko_bricks SET session_date = (ts AT TIME ZONE 'utc')::date
--   --   WHERE session_date <> (ts AT TIME ZONE 'utc')::date;
--
--   -- 3. Future-dated rows (session_date > today) are unambiguously bogus:
--   --   DELETE FROM nt_renko_bricks WHERE session_date > (now() AT TIME ZONE 'utc')::date;
--   --   (9,753 rows today — but confirm none is the live grid first.)
-- =============================================================================
