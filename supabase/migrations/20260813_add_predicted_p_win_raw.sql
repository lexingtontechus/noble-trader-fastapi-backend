-- 2026-08-13: predicted_p_win_raw for the calibration audit
-- ============================================================
-- Carries the PRE-enforcement p_win from signal capture (light/heavy sweep)
-- through nt_signal_sim into nt_calibration_records, so the calibration loop
-- can separate model miscalibration from enforcement compensation (the
-- enforcement shrinks stored p_win toward 0.5, which otherwise masks the
-- model's true bias).
--
-- APPLY IN THE SUPABASE SQL EDITOR (or `supabase db push`) BEFORE enabling
-- NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW in the runtime env. If the flag is on
-- without this column, PostgREST rejects every nt_signal_sim insert with
-- PGRST204 and signal capture stops.

ALTER TABLE public.nt_signal_sim
    ADD COLUMN IF NOT EXISTS predicted_p_win_raw double precision;

-- Note: nt_calibration_records.predicted_p_win_raw and calibration_brier
-- already exist (seen 2026-08-13) — no DDL needed there.
