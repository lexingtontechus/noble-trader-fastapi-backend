-- =============================================================================
-- 197: backfill nt_renko_bricks.session_date from ts (correct the mis-stamp)
-- =============================================================================
-- Follows 196 (which fixed the column DEFAULT to UTC and documented the writer
-- bug). The writer itself is fixed in code (regime_platform/sweep_orchestrator/
-- supabase_client.py log_renko_bricks now derives session_date from each brick's
-- own ts). This migration corrects the EXISTING rows so the table is consistent
-- with what the fixed writer produces going forward.
--
-- SCOPE (live, 2026-10-04): 95,871 rows; 94,590 have session_date <> ts::date;
-- 33 distinct real trading days.
--
-- UNIQUE-CONSTRAINT COLLISION: the unique index is
--   (symbol, brick_index, session_date, brick_size).
-- Correcting session_date makes 9,485 groups collide (22,480 rows). Those are
-- many generations of the SAME grid position written by successive sweeps (each
-- rebuild re-upserts index 0..N-1 with a shifted price window). We keep the
-- LATEST row per corrected key (max brick_id) — the most recent sweep's version
-- — and delete the superseded earlier ones: 12,995 rows. This is the same
-- "latest generation wins" rule log_renko_bricks' own on_conflict upsert uses.
--
-- SAFE TO RE-RUN: after the first run every row already satisfies
-- session_date = ts::date and there are no collisions, so both statements are
-- no-ops.
--
-- IDEMPOTENT: yes (see above).
-- =============================================================================

-- ─── 1. Delete superseded duplicate generations ─────────────────────────────
WITH ranked AS (
  SELECT brick_id,
         row_number() OVER (
           PARTITION BY symbol, brick_index, (ts AT TIME ZONE 'utc')::date, brick_size
           ORDER BY brick_id DESC
         ) AS rn
  FROM public.nt_renko_bricks
)
DELETE FROM public.nt_renko_bricks b
USING ranked r
WHERE b.brick_id = r.brick_id
  AND r.rn > 1;

-- ─── 2. Correct session_date to the brick's own real trading day ────────────
UPDATE public.nt_renko_bricks
SET session_date = (ts AT TIME ZONE 'utc')::date
WHERE session_date <> (ts AT TIME ZONE 'utc')::date;

-- =============================================================================
-- POST-RUN VERIFICATION:
--   SELECT count(*) FILTER (WHERE session_date <> (ts AT TIME ZONE 'utc')::date)
--     AS still_wrong, count(*) AS total,
--     count(DISTINCT (ts AT TIME ZONE 'utc')::date) AS real_days
--   FROM nt_renko_bricks;
--   -- expect still_wrong = 0, real_days ~33
-- =============================================================================
