-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 136: nt_heavy_sweep_deadline_aware_shadow
--
-- Root-cause fix, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md.
-- heavy_sweep_deadline_aware_scoring_mode (off|shadow|live, config/feature_flags.yaml,
-- default off) rescores the SAME heavy-sweep grid candidates with any
-- trade held longer than EXPIRY_BARS (48h) treated as 0 PnL before
-- computing Sharpe — matching what eod_resolver.py already enforces on
-- real signals, instead of the unlimited-patience backtest Sharpe that
-- picked brackets which mostly time out live. In shadow mode the real
-- (persisted) pick is unaffected — this table is where the comparison
-- against what the corrected scoring WOULD have picked gets persisted,
-- same reasoning and same shape as migration 135's
-- nt_heavy_sweep_atr_anchored_shadow (its direct sibling — different axis
-- of comparison: scoring method here, candidate set there).
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_heavy_sweep_deadline_aware_shadow (
  symbol            TEXT PRIMARY KEY,
  n_compared        INTEGER NOT NULL DEFAULT 0,
  n_changed         INTEGER NOT NULL DEFAULT 0,
  last_uncapped_sharpe   DOUBLE PRECISION,
  last_corrected_sharpe  DOUBLE PRECISION,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE nt_heavy_sweep_deadline_aware_shadow IS
  'Root-cause shadow comparison (worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md): per symbol, how often deadline-aware rescoring (any trade held > EXPIRY_BARS treated as 0 PnL before computing Sharpe) would pick a different bracket than the unmodified uncapped-Sharpe pick. Diagnostic only while heavy_sweep_deadline_aware_scoring_mode=shadow; read manually before flipping to live -- the flag is global across all symbols, same reasoning as its ATR-anchoring sibling (migration 135).';
COMMENT ON COLUMN nt_heavy_sweep_deadline_aware_shadow.n_changed IS
  'Count of sweeps where the deadline-aware-corrected pick differs from the uncapped pick -- including cases where the uncapped grid found nothing at all and the corrected scoring rescued it.';

ALTER TABLE nt_heavy_sweep_deadline_aware_shadow ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_heavy_sweep_deadline_aware_shadow;
CREATE POLICY "Service role full access" ON nt_heavy_sweep_deadline_aware_shadow
  FOR ALL TO service_role USING (true) WITH CHECK (true);
