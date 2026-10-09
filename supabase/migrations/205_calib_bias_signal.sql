-- 205_calib_bias_signal.sql
-- Additive: `signal` column on nt_calibration_bias_by_bucket (join key parity
-- with nt_bracket_reachability, which already carries it). Nullable, no
-- backfill — the writer (calibration_bias_bucket.build_calibration_bias_by_bucket)
-- stamps it on the next recompute; upsert on_conflict must include it.
--
-- worklog 20261009_CALIBRATION_REACHABILITY_TRIAGE.md §6 (P4).

ALTER TABLE nt_calibration_bias_by_bucket
  ADD COLUMN IF NOT EXISTS signal text;

-- The by-bucket table is rebuilt from nt_signal_sim on every pass
-- (calibration_bias_bucket.build_calibration_bias_by_bucket: _delete_old_rows
-- then upsert), so no data migration is needed for `signal` — the next
-- recompute populates it. This DDL is idempotent and safe to re-run.
