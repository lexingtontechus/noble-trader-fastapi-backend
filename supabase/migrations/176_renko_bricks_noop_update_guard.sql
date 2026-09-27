-- 176_renko_bricks_noop_update_guard.sql  (2026-09-27)
--
-- Fixes the dominant CPU/IO/write-error contributor identified during the
-- Supabase incident investigation (see 175_dashboard_cache_refresh_guard_and_signal_sim_ts_index.sql
-- for the cache-refresh + pagination-index fixes).
--
-- ROOT CAUSE: every light-sweep tick (*/15 min x 14 symbols) rebuilds the
-- ENTIRE renko brick series for a symbol from the current price window
-- (RenkoBrickEngine.build_from_prices) and re-upserts every brick via
-- `Prefer: resolution=merge-duplicates` (regime_platform/sweep_orchestrator/
-- supabase_client.py::log_renko_bricks). Because the rebuilt series is
-- usually IDENTICAL to what is already stored (price action didn't move
-- enough to add a new brick, or only the newest 1-2 bricks changed),
-- Postgres still performs a full UPDATE (new tuple version + WAL + index
-- maintenance on `idx_nt_bricks_symbol (symbol, ts DESC)`) for every
-- unchanged row, every tick, forever.
--
-- Confirmed live via pg_stat_user_tables: nt_renko_bricks carries only
-- 169,751 live rows (58MB) but has taken 26,273,935 total UPDATEs since the
-- 2026-06-18 stats reset — a ~155x row-count write amplification — and
-- 2,514 autovacuum runs (far more than any other table in the schema),
-- consuming disproportionate CPU/IO for bloat cleanup on a small table.
--
-- FIX: a BEFORE UPDATE trigger that compares the brick's real, meaningful
-- columns (direction/brick_size/open_price/close_price/high/low/label) and
-- returns NULL (skip the write entirely) when nothing changed. `ts` is
-- deliberately EXCLUDED from the comparison: when the caller could not
-- supply real per-bar timestamps it synthesizes a fake "now-based" ts on
-- every call (see log_renko_bricks docstring, has_real_timestamps=False
-- path) — that fabricated value differs on every tick even though the
-- brick itself is unchanged, and is not worth a write on its own.
--
-- This requires NO application/Python deploy — the sweep can keep sending
-- the full rebuilt series every tick exactly as it does today; Postgres now
-- discards the no-op writes before they touch WAL/indexes/autovacuum.

CREATE OR REPLACE FUNCTION public.nt_renko_bricks_skip_noop_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.direction   IS NOT DISTINCT FROM OLD.direction
     AND NEW.brick_size  IS NOT DISTINCT FROM OLD.brick_size
     AND NEW.open_price  IS NOT DISTINCT FROM OLD.open_price
     AND NEW.close_price IS NOT DISTINCT FROM OLD.close_price
     AND NEW.high        IS NOT DISTINCT FROM OLD.high
     AND NEW.low         IS NOT DISTINCT FROM OLD.low
     AND NEW.label       IS NOT DISTINCT FROM OLD.label
  THEN
    -- Nothing meaningful changed (ts intentionally excluded — it is
    -- fabricated on every call when real per-bar timestamps are
    -- unavailable). Returning NULL from a BEFORE UPDATE row trigger skips
    -- the update entirely: no new tuple, no WAL, no index maintenance.
    RETURN NULL;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_nt_renko_bricks_skip_noop_update ON public.nt_renko_bricks;

CREATE TRIGGER trg_nt_renko_bricks_skip_noop_update
  BEFORE UPDATE ON public.nt_renko_bricks
  FOR EACH ROW
  EXECUTE FUNCTION public.nt_renko_bricks_skip_noop_update();

COMMENT ON FUNCTION public.nt_renko_bricks_skip_noop_update() IS
  'Skips no-op UPDATEs on nt_renko_bricks (ts excluded from the comparison — '
  'it is fabricated per-call when real per-bar timestamps are unavailable). '
  'Cuts write amplification from the full-series-rebuild-and-reupsert every '
  'sweep tick (was 26M+ updates against 170K live rows).';

-- POST-RUN VERIFICATION (compare growth rate before/after over ~1 hour):
--   SELECT n_tup_upd, n_tup_hot_upd, autovacuum_count
--   FROM pg_stat_user_tables WHERE relname = 'nt_renko_bricks';
