-- Migration 202: Bracket reachability latest-view + delete-then-insert support
-- 2026-10-07: Table freshness fix (Option A + Option C)
--
-- Option A (delete-then-insert) is implemented in Python:
--   bracket_reachability.py and calibration_bias_bucket.py now call
--   _delete_old_rows() before upserting fresh rows.
--
-- Option C (this migration): Create a view that returns only the latest
-- build's rows, so consumers always read fresh data without needing to
-- filter on computed_at themselves.

-- Drop existing view if it exists (idempotent)
DROP VIEW IF EXISTS v_bracket_reachability_latest;

-- Create the latest-view
CREATE OR REPLACE VIEW v_bracket_reachability_latest AS
SELECT DISTINCT ON (symbol, vol_tier, distance_atr_bucket, signal)
    symbol,
    vol_tier,
    distance_atr_bucket,
    signal,
    n,
    n_stored,
    n_proxy,
    pct_hit_tp,
    pct_hit_sl,
    pct_expired,
    median_hours_to_resolve,
    p75_hours,
    p90_hours,
    atr_source,
    previous_stats,
    computed_at
FROM nt_bracket_reachability
ORDER BY symbol, vol_tier, distance_atr_bucket, signal, computed_at DESC;

-- Grant access
GRANT SELECT ON v_bracket_reachability_latest TO anon, authenticated, service_role;

-- Also create a latest-view for calibration_bias_by_bucket
DROP VIEW IF EXISTS v_calibration_bias_by_bucket_latest;

CREATE OR REPLACE VIEW v_calibration_bias_by_bucket_latest AS
SELECT DISTINCT ON (symbol, vol_tier, distance_atr_bucket)
    symbol,
    vol_tier,
    distance_atr_bucket,
    n,
    avg_p_win,
    win_rate,
    bias,
    pct_expired,
    avg_rr_winners,
    expectancy_r,
    median_hours_to_resolve,
    daily_expectancy_r,
    atr_source,
    computed_at
FROM nt_calibration_bias_by_bucket
ORDER BY symbol, vol_tier, distance_atr_bucket, computed_at DESC;

GRANT SELECT ON v_calibration_bias_by_bucket_latest TO anon, authenticated, service_role;

-- Add a comment documenting the freshness fix
COMMENT ON VIEW v_bracket_reachability_latest IS
'Latest build of nt_bracket_reachability (one row per symbol/vol_tier/bucket/signal).
Created 2026-10-07 to fix stale-read issue: the underlying table accumulates rows
from multiple builds, and a limit=N query returns an arbitrary subset. This view
always returns the most recent build per key.';

COMMENT ON VIEW v_calibration_bias_by_bucket_latest IS
'Latest build of nt_calibration_bias_by_bucket (one row per symbol/vol_tier/bucket).
Created 2026-10-07 to fix stale-read issue.';
