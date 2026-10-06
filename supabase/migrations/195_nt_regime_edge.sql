-- Migration: nt_regime_edge
-- Per-(symbol, signal, regime) edge table for regime-conditional qualification.
-- Computed from resolved nt_signal_sim outcomes by regime_edge.py.
-- See worklog/20261005_REGIME_EDGE_DESIGN.md.

CREATE TABLE IF NOT EXISTS nt_regime_edge (
    symbol TEXT NOT NULL,
    signal TEXT NOT NULL,
    regime TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    expectancy_r DOUBLE PRECISION NOT NULL DEFAULT 0,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, signal, regime)
);

COMMENT ON TABLE nt_regime_edge IS 'Per-(symbol, signal, regime) edge table. Computed from resolved nt_signal_sim outcomes. Used by regime_edge gate to reject signals in negative-edge buckets.';
COMMENT ON COLUMN nt_regime_edge.symbol IS 'Trading symbol';
COMMENT ON COLUMN nt_regime_edge.signal IS 'Signal direction (buy/sell)';
COMMENT ON COLUMN nt_regime_edge.regime IS 'HMM regime label (e.g. high_vol_bull)';
COMMENT ON COLUMN nt_regime_edge.n IS 'Episode count (wins + losses)';
COMMENT ON COLUMN nt_regime_edge.win_rate IS 'wins / n';
COMMENT ON COLUMN nt_regime_edge.expectancy_r IS 'win_rate * avg_rr - (1 - win_rate) * 1.0';

-- RLS
ALTER TABLE nt_regime_edge ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_all" ON nt_regime_edge FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "anon_read" ON nt_regime_edge FOR SELECT TO anon USING (true);
