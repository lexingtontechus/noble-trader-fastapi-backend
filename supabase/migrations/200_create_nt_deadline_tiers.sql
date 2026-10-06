-- =============================================================================
-- 200: create nt_deadline_tiers table
-- =============================================================================
-- Stores per-(symbol, vol_tier) deadline tiers computed by dynamic_deadline.py.
-- Previously stored as JSON in nt_roadmap.current_state (not queryable).
-- This table is populated by the 4h reopt cycle and read by the frontend
-- via v_talaria_deadline_tiers.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.nt_deadline_tiers (
  id SERIAL PRIMARY KEY,
  symbol TEXT NOT NULL,
  vol_tier TEXT NOT NULL,
  soft_deadline_hours FLOAT NOT NULL,
  hard_deadline_hours FLOAT NOT NULL,
  median_hours FLOAT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'low',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(symbol, vol_tier)
);

CREATE INDEX IF NOT EXISTS idx_nt_deadline_tiers_symbol ON public.nt_deadline_tiers(symbol);
CREATE INDEX IF NOT EXISTS idx_nt_deadline_tiers_vol_tier ON public.nt_deadline_tiers(vol_tier);

COMMENT ON TABLE public.nt_deadline_tiers IS
  'Per-(symbol, vol_tier) resolution deadline tiers from dynamic_deadline.py. Soft = p75 (full credit), Hard = p90 (zero credit), Median = p50 (expected resolution).';

-- Grant read access to anon (frontend reads via PostgREST)
GRANT SELECT ON public.nt_deadline_tiers TO anon;
GRANT SELECT ON public.nt_deadline_tiers TO authenticated;
