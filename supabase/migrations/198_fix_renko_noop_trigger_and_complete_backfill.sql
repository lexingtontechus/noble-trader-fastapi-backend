-- =============================================================================
-- 198: fix nt_renko_bricks_skip_noop_update trigger + complete the 197 backfill
-- =============================================================================
-- Migration 197's DELETE ran (12,995 superseded rows removed) but its
-- session_date UPDATE silently did nothing: the BEFORE UPDATE trigger
-- `trg_nt_renko_bricks_skip_noop_update` returns NULL (skips the row) when
-- direction/brick_size/open_price/close_price/high/low/label are all unchanged
-- — and it did NOT compare `session_date`, so an update that changes ONLY
-- session_date was misclassified as a no-op.
--
-- That trigger's intent is correct (skip re-sweeps that rewrite identical brick
-- data, avoiding WAL/index churn from a fabricated `ts`). But `session_date` is
-- a KEY field (part of the unique index) and semantically load-bearing, so a
-- change to it is never a no-op. Same for `brick_index`. Both are added to the
-- comparison below.
--
-- A second blocker: even with the trigger fixed, the UPDATE is order-dependent.
-- Some rows swap session_date values (A: 10-04→10-03 while B: 10-05→10-04), so
-- a transient duplicate fires against the UNIQUE constraint mid-statement even
-- though the FINAL state is unique. Remedy: drop the constraint, run the
-- UPDATE, re-add it — the re-add validates and rolls back if a real dup remains.
--
-- IDEMPOTENT: CREATE OR REPLACE FUNCTION; DROP IF EXISTS / ADD CONSTRAINT; a
-- guarded UPDATE.
-- =============================================================================

-- ─── 1. Make session_date / brick_index meaningful for the no-op check ──────
CREATE OR REPLACE FUNCTION public.nt_renko_bricks_skip_noop_update()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  IF NEW.direction     IS NOT DISTINCT FROM OLD.direction
     AND NEW.brick_size  IS NOT DISTINCT FROM OLD.brick_size
     AND NEW.open_price  IS NOT DISTINCT FROM OLD.open_price
     AND NEW.close_price IS NOT DISTINCT FROM OLD.close_price
     AND NEW.high        IS NOT DISTINCT FROM OLD.high
     AND NEW.low         IS NOT DISTINCT FROM OLD.low
     AND NEW.label       IS NOT DISTINCT FROM OLD.label
     AND NEW.session_date IS NOT DISTINCT FROM OLD.session_date
     AND NEW.brick_index  IS NOT DISTINCT FROM OLD.brick_index
  THEN
    -- Nothing meaningful changed (ts intentionally excluded — it is
    -- fabricated on every call when real per-bar timestamps are
    -- unavailable). Returning NULL from a BEFORE UPDATE row trigger skips
    -- the update entirely: no new tuple, no WAL, no index maintenance.
    RETURN NULL;
  END IF;
  RETURN NEW;
END;
$function$;

-- ─── 2. Drop the unique constraint so the key-swapping UPDATE can run ───────
ALTER TABLE public.nt_renko_bricks
  DROP CONSTRAINT IF EXISTS nt_renko_bricks_symbol_brick_index_session_date_brick_size_key;

-- ─── 3. Correct session_date to each brick's own real trading day ───────────
UPDATE public.nt_renko_bricks
SET session_date = (ts AT TIME ZONE 'utc')::date
WHERE session_date <> (ts AT TIME ZONE 'utc')::date;

-- ─── 4. Restore the unique constraint (validates the final state) ───────────
ALTER TABLE public.nt_renko_bricks
  ADD CONSTRAINT nt_renko_bricks_symbol_brick_index_session_date_brick_size_key
  UNIQUE (symbol, brick_index, session_date, brick_size);

-- =============================================================================
-- POST-RUN VERIFICATION:
--   SELECT count(*) FILTER (WHERE session_date <> (ts AT TIME ZONE 'utc')::date)
--     AS still_wrong, count(*) AS total,
--     count(DISTINCT session_date) AS session_days FROM nt_renko_bricks;
--   -- expect still_wrong = 0, session_days = 33
-- =============================================================================
