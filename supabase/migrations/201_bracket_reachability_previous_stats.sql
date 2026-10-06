-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 201: previous_stats JSONB column on nt_bracket_reachability
--
-- worklog/20261006_KELLY_GEOMETRY_QUALITY_FORK_RESOLUTION.md §4
--
-- The geometry band urgency job's drift loop does not close: it reads
-- nt_calibration_bias_by_cell to detect drift, recomputes nt_bracket_reachability,
-- but nothing recomputes the bias table from the refreshed reachability. The
-- trigger never clears — the same cell re-fires every 4h cooldown.
--
-- Fix: add previous_stats JSONB to store the last urgency-computed stats.
-- The urgency job compares fresh recomputed stats against previous_stats.
-- If drift is below threshold, the cell is stable → skip. This removes the
-- dependency on nt_calibration_bias_by_cell's refresh cadence.
--
-- No qualification, gate, or live-trading behavior changes with this migration
-- alone — nt_bracket_reachability is a read-only diagnostic table.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Add previous_stats JSONB column ───────────────────────────────────────

ALTER TABLE nt_bracket_reachability
  ADD COLUMN IF NOT EXISTS previous_stats JSONB;

COMMENT ON COLUMN nt_bracket_reachability.previous_stats IS
  'JSONB snapshot of the last urgency-computed stats (pct_hit_tp, pct_hit_sl, pct_expired, median_hours_to_resolve, p75_hours, p90_hours). Used by geometry_band_urgency.py to detect drift without depending on nt_calibration_bias_by_cell refresh cadence. NULL = never recomputed by the urgency job.';

-- ─── 2. Backfill: copy current stats into previous_stats ─────────────────────
-- For rows that have never been recomputed by the urgency job, previous_stats
-- is NULL. The urgency job treats NULL as "first run" and compares against the
-- current row stats instead.

UPDATE nt_bracket_reachability
SET previous_stats = jsonb_build_object(
    'pct_hit_tp', pct_hit_tp,
    'pct_hit_sl', pct_hit_sl,
    'pct_expired', pct_expired,
    'median_hours_to_resolve', median_hours_to_resolve,
    'p75_hours', p75_hours,
    'p90_hours', p90_hours
)
WHERE previous_stats IS NULL
  AND pct_hit_tp IS NOT NULL;

-- ─── 3. RLS policy (same as migration 120) ──────────────────────────────────

-- No new policy needed — the existing "Service role full access" policy
-- already covers the new column.