-- ============================================================================
-- Migration 012: Renko Pipeline Snapshot Table
-- ============================================================================
-- Stores v3 pipeline snapshots for instant hydration on restart.
-- The `stats` JSONB column holds the full snapshot payload including
-- brick history, classified bricks, trade journal, active position,
-- and signal filter state.
--
-- Key: (user_id, symbol, brick_size) — one snapshot per user/symbol/brick_size
-- For MCP agent use (single admin), user_id is NULL.
-- ============================================================================
-- v7.3.0: Added user_id column for multi-user session key stability.
--   - Composite unique constraint: (user_id, symbol, brick_size)
--   - MCP agents use user_id=NULL (admin namespace)
--   - Frontend users get per-user isolation via Clerk user_id
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.ta_renko_snapshot (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     TEXT,                       -- Clerk user_id (NULL for MCP admin)
    symbol      TEXT    NOT NULL,
    brick_size  DOUBLE PRECISION NOT NULL DEFAULT 0.50,
    total_bricks    INTEGER NOT NULL DEFAULT 0,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    total_pnl_bricks DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    config      JSONB   NOT NULL DEFAULT '{}',
    stats       JSONB   NOT NULL DEFAULT '{}',
    prices_fed  INTEGER NOT NULL DEFAULT 0,
    period      TEXT    NOT NULL DEFAULT 'pipeline-persist',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One snapshot per (user_id, symbol, brick_size)
    -- NULLS NOT DISTINCT ensures two NULL user_id rows for same symbol/brick_size conflict
    CONSTRAINT uq_renko_snapshot_user_symbol_brick UNIQUE NULLS NOT DISTINCT (user_id, symbol, brick_size)
);

-- Index for fast lookup by symbol (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_renko_snapshot_symbol
    ON public.ta_renko_snapshot (symbol);

-- Index for user-scoped lookups (multi-user isolation)
CREATE INDEX IF NOT EXISTS idx_renko_snapshot_user_id
    ON public.ta_renko_snapshot (user_id) WHERE user_id IS NOT NULL;

-- Composite index for the most common MCP query: user_id + symbol
CREATE INDEX IF NOT EXISTS idx_renko_snapshot_user_symbol
    ON public.ta_renko_snapshot (user_id, symbol);

-- Index for finding stale snapshots (updated_at > 4h ago)
CREATE INDEX IF NOT EXISTS idx_renko_snapshot_updated
    ON public.ta_renko_snapshot (updated_at DESC);

-- GIN index for JSONB queries on stats (e.g. checking snapshot_version)
CREATE INDEX IF NOT EXISTS idx_renko_snapshot_stats_gin
    ON public.ta_renko_snapshot USING GIN (stats jsonb_path_ops);

-- Auto-update updated_at on row change
CREATE OR REPLACE FUNCTION public.handle_renko_snapshot_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_renko_snapshot_updated_at
    ON public.ta_renko_snapshot;

CREATE TRIGGER trg_renko_snapshot_updated_at
    BEFORE UPDATE ON public.ta_renko_snapshot
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_renko_snapshot_updated_at();

-- ============================================================================
-- Row Level Security
-- ============================================================================
ALTER TABLE public.ta_renko_snapshot ENABLE ROW LEVEL SECURITY;

-- Service role (backend) can do anything
CREATE POLICY "Service role full access" ON public.ta_renko_snapshot
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Authenticated users can read their own snapshots + admin (NULL user_id) snapshots
CREATE POLICY "Authenticated read own + admin" ON public.ta_renko_snapshot
    FOR SELECT
    TO authenticated
    USING (user_id = auth.uid()::text OR user_id IS NULL);

-- Authenticated users can insert/update their own snapshots
CREATE POLICY "Authenticated write own" ON public.ta_renko_snapshot
    FOR INSERT
    TO authenticated
    WITH CHECK (user_id = auth.uid()::text);

CREATE POLICY "Authenticated update own" ON public.ta_renko_snapshot
    FOR UPDATE
    TO authenticated
    USING (user_id = auth.uid()::text)
    WITH CHECK (user_id = auth.uid()::text);

-- ============================================================================
-- Comments
-- ============================================================================
COMMENT ON TABLE public.ta_renko_snapshot IS
    'Renko pipeline v3 snapshots — full state persistence for MCP agent autonomy. '
    'stats JSONB contains bricks[], classified[], trades[], active_position, '
    'signal_filter_state. Hydrate via RenkoPipeline.from_snapshot() for <1s restore. '
    'v7.3.0: user_id column for session key stability (user_id:symbol composite key).';

COMMENT ON COLUMN public.ta_renko_snapshot.user_id IS
    'Clerk user_id for session key stability. NULL for MCP admin namespace. '
    'Composite key (user_id, symbol, brick_size) ensures per-user isolation.';
