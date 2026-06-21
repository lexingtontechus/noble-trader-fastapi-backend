-- ═══════════════════════════════════════════════════════════════
-- Migration 100: nt_venues, nt_calibration_records, nt_account_snapshots
--
-- New application-layer tables for the TimesFM Hybrid Intelligence
-- Calibration Loop and multi-venue account tracking.
--
-- Tables:
--   nt_venues              — Venue registry (hyperliquid / alpaca)
--   nt_calibration_records — Prediction vs. outcome ledger for bias detection
--   nt_account_snapshots   — Per-venue balance timeline
--
-- All use the nt_ prefix to distinguish from the existing edge-function tables.
-- ═══════════════════════════════════════════════════════════════

-- ─── nt_venues ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_venues (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  venue_type      TEXT NOT NULL CHECK (venue_type IN ('perp_futures', 'equities', 'spot', 'forex', 'commodities')),
  supports_stops  BOOLEAN NOT NULL DEFAULT true,
  max_leverage    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed: HyperLiquid (perp futures, 50x) + Alpaca (equities, 4x)
INSERT INTO nt_venues (id, name, venue_type, supports_stops, max_leverage, is_active) VALUES
  ('hyperliquid', 'HyperLiquid', 'perp_futures', true, 50.0, true),
  ('alpaca', 'Alpaca', 'equities', true, 4.0, true)
ON CONFLICT (id) DO NOTHING;

-- updated_at auto-trigger
CREATE OR REPLACE FUNCTION update_nt_venues_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_nt_venues_updated_at ON nt_venues;
CREATE TRIGGER trg_nt_venues_updated_at
  BEFORE UPDATE ON nt_venues
  FOR EACH ROW
  EXECUTE FUNCTION update_nt_venues_updated_at();

-- ─── nt_calibration_records ───────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_calibration_records (
  id              TEXT PRIMARY KEY,
  symbol          TEXT NOT NULL REFERENCES nt_symbol(symbol),
  predicted_p_win DOUBLE PRECISION NOT NULL,
  outcome         TEXT NOT NULL CHECK (outcome IN ('win', 'loss')),
  aggression      TEXT NOT NULL CHECK (aggression IN ('passive', 'mid', 'aggressive')),
  actual_pnl      DOUBLE PRECISION,
  error_source    TEXT,
  entry_price     DOUBLE PRECISION,
  exit_price      DOUBLE PRECISION,
  p_regime        DOUBLE PRECISION,
  p_imbalance     DOUBLE PRECISION,
  p_markov        DOUBLE PRECISION,
  p_timesfm       DOUBLE PRECISION,
  entered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_calibration_symbol ON nt_calibration_records(symbol);
CREATE INDEX IF NOT EXISTS idx_calibration_entered ON nt_calibration_records(entered_at DESC);
CREATE INDEX IF NOT EXISTS idx_calibration_outcome ON nt_calibration_records(outcome);

-- ─── nt_account_snapshots ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_account_snapshots (
  id              TEXT PRIMARY KEY,
  venue_id        TEXT NOT NULL REFERENCES nt_venues(id),
  equity          DOUBLE PRECISION NOT NULL,
  cash            DOUBLE PRECISION,
  margin_used     DOUBLE PRECISION,
  open_positions  INTEGER NOT NULL DEFAULT 0,
  max_drawdown    DOUBLE PRECISION,
  unrealized_pnl  DOUBLE PRECISION,
  snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_snapshot_venue ON nt_account_snapshots(venue_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_time ON nt_account_snapshots(snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_venue_time ON nt_account_snapshots(venue_id, snapshot_at DESC);

-- ─── Row Level Security ───────────────────────────────────────

ALTER TABLE nt_venues ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON nt_venues
  FOR ALL USING (true) WITH CHECK (true);

ALTER TABLE nt_calibration_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON nt_calibration_records
  FOR ALL USING (true) WITH CHECK (true);

ALTER TABLE nt_account_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON nt_account_snapshots
  FOR ALL USING (true) WITH CHECK (true);
