-- 181_nt_macro_forecast_pivots.sql
-- P2 T-Layer: attach typed pivot labels to the EXISTING nt_macro_forecast
-- LLM store (one LLM dataset). Calendar stays raw in .cache/kairos/econ_calendar.json
-- (TDVA /api/calendar/economic) — not a second LLM table.

ALTER TABLE nt_macro_forecast
  ADD COLUMN IF NOT EXISTS pivot_label TEXT
    CHECK (pivot_label IS NULL OR pivot_label IN ('major_pivot', 'incremental', 'no_change')),
  ADD COLUMN IF NOT EXISTS pivot_axis TEXT
    CHECK (pivot_axis IS NULL OR pivot_axis IN ('reaction_function', 'tool', 'trajectory', 'none')),
  ADD COLUMN IF NOT EXISTS pivot_intensity DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS pivot_surprise DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS pivot_as_of TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS pivot_event_title TEXT,
  ADD COLUMN IF NOT EXISTS pivot_summary TEXT;

COMMENT ON COLUMN nt_macro_forecast.pivot_label IS
  'P2 typed pivot (major_pivot|incremental|no_change) from calendar/policy events via LLM. Research/T-Layer only.';
COMMENT ON COLUMN nt_macro_forecast.pivot_as_of IS
  'Calendar event timestamp the pivot label refers to (not the upsert time).';
