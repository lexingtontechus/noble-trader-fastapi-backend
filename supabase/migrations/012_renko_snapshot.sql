-- ============================================================================
-- Migration 012: Renko Pipeline Snapshot Table
-- ============================================================================
-- Stores v3 pipeline snapshots for instant hydration on restart.
-- The `stats` JSONB column holds the full snapshot payload including
-- brick history, classified bricks, trade journal, active position,
-- and signal filter state.
--
-- Key: (symbol, brick_size) — one snapshot per symbol/brick_size combo
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.ta_renko_snapshot (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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

    -- One snapshot per (symbol, brick_size)
    CONSTRAINT uq_renko_snapshot_symbol_brick UNIQUE (symbol, brick_size)
);

-- Index for fast lookup by symbol (most common query pattern)
CREATE INDEX IF NOT EXISTS idx_renko_snapshot_symbol
    ON public.ta_renko_snapshot (symbol);

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

-- Authenticated users can read snapshots
CREATE POLICY "Authenticated read access" ON public.ta_renko_snapshot
    FOR SELECT
    TO authenticated
    USING (true);

-- ============================================================================
-- Comment
-- ============================================================================
COMMENT ON TABLE public.ta_renko_snapshot IS
    'Renko pipeline v3 snapshots — full state persistence for MCP agent autonomy. '
    'stats JSONB contains bricks[], classified[], trades[], active_position, '
    'signal_filter_state. Hydrate via RenkoPipeline.from_snapshot() for <1s restore.';
