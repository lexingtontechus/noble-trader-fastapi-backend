-- Index the unindexed FK flagged by Supabase performance advisor
-- (pulse 2026-09-27 / 504 investigation): cheap win before any RLS
-- consolidation. Idempotent.

CREATE INDEX IF NOT EXISTS idx_nt_calibration_records_signal_id
  ON public.nt_calibration_records (signal_id);
