-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 124: nt_macro_regime + nt_signal_sim macro capture columns
--
-- Long-range macro-regime filter (worklog/20260825_PIPELINE_OVERVIEW_EV_FEEDBACK_LOOP.md,
-- "Macro filter — full build scope", 2026-08-26/28). Every existing signal
-- source in this platform looks at most ~8-12 days of data (the trend HMM's
-- own refit window); nothing has multi-month market memory. This is a pure
-- price-based daily-bar read (last close vs. 200-day SMA, EMA-smoothed) —
-- deliberately separate from win/loss+PnL signal-quality work (migration 121),
-- which answers a different question (see nt_macro_edge_by_symbol, migration
-- 125, for where the two connect as evidence, not a blended metric).
--
-- Column-style follows migrations 120-123: nt_ prefix, snake_case,
-- DOUBLE PRECISION, TIMESTAMPTZ, service-role-only RLS.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_macro_regime (
  symbol               TEXT PRIMARY KEY,
  macro_score          DOUBLE PRECISION,
  macro_score_smoothed DOUBLE PRECISION,
  macro_label          TEXT CHECK (macro_label IN ('bear', 'neutral', 'bull')),
  macro_strength       DOUBLE PRECISION,
  sma_window_days      INTEGER,
  computed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_macro_regime IS
  'Daily-refreshed per-symbol macro trend read: (last_close - SMA_200d) / SMA_200d, EMA-smoothed, labeled bear/neutral/bull with a 0-1 strength. Read by both sweep paths right before compute_ev to derive a Kelly multiplier (macro_regime_mode=off|shadow|live, config/feature_flags.yaml). macro_score_smoothed is carried across daily refreshes (read-before-write) so the EMA has real continuity.';

ALTER TABLE nt_macro_regime ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_macro_regime;
CREATE POLICY "Service role full access" ON nt_macro_regime
  FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Capture the macro read at signal-generation time (mirrors the ATR
-- precedent, migration 120) so the win/loss+PnL evidence layer
-- (nt_macro_edge_by_symbol, migration 125) can bucket resolved outcomes by
-- whether the trade agreed with the macro trend, without needing a
-- separate point-in-time history join. Nullable — legacy rows and callers
-- that predate this migration are unaffected.
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS macro_label TEXT;
ALTER TABLE nt_signal_sim ADD COLUMN IF NOT EXISTS macro_strength DOUBLE PRECISION;
