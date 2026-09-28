-- 182_nt_macro_forecast_uniform.sql
-- P2 refactor: nt_macro_forecast as the single uniform LLM macro-context store
-- for ALL nt_symbol rows (commodity / forex / equity / index / crypto) —
-- not a commodity-only forecast table.
--
-- 1) Deconstructed narrative (LLM)
-- 2) Short-term market structure (computed / TA)
-- 3) Event/pivot memory (from 181 + history)
-- 4) Provenance for multi-source docs

-- ── Deconstructed narrative ──────────────────────────────────────────────
ALTER TABLE nt_macro_forecast
  ADD COLUMN IF NOT EXISTS thesis TEXT,
  ADD COLUMN IF NOT EXISTS drivers JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS risks JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS horizon_days INTEGER NOT NULL DEFAULT 365;

-- ── Short-term market structure (near price, not 12m narrative) ─────────
ALTER TABLE nt_macro_forecast
  ADD COLUMN IF NOT EXISTS structure_bias TEXT
    CHECK (structure_bias IS NULL OR structure_bias IN ('bull', 'bear', 'neutral')),
  ADD COLUMN IF NOT EXISTS structure_strength DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS structure_note TEXT,
  ADD COLUMN IF NOT EXISTS structure_as_of TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS structure_source TEXT;

-- ── Event memory (beyond single latest pivot) ───────────────────────────
ALTER TABLE nt_macro_forecast
  ADD COLUMN IF NOT EXISTS events JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ── Provenance / universe ───────────────────────────────────────────────
ALTER TABLE nt_macro_forecast
  ADD COLUMN IF NOT EXISTS source_doc TEXT,
  ADD COLUMN IF NOT EXISTS asset_class TEXT;

COMMENT ON COLUMN nt_macro_forecast.drivers IS
  'JSON array [{name, stance, confidence}] — LLM-deconstructed drivers (rates, USD, supply…).';
COMMENT ON COLUMN nt_macro_forecast.risks IS
  'JSON array [{name, direction, severity}] — named risks from the narrative.';
COMMENT ON COLUMN nt_macro_forecast.structure_bias IS
  'Short-term market structure (TA/sweep), distinct from current_macro_label (strategic).';
COMMENT ON COLUMN nt_macro_forecast.events IS
  'JSON array of recent labeled calendar/pivot events — single LLM event log, no side table.';
