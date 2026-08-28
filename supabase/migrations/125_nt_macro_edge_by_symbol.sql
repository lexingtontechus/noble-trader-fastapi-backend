-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 125: nt_macro_edge_by_symbol
--
-- Win/loss + PnL evidence for the macro filter (migration 124). The macro
-- filter itself is purely price-based (by design — see that migration's
-- comment); this table is the separate, outcome-based read that answers
-- "does trading WITH the measured macro trend actually produce a better
-- win rate / expectancy than trading against it, for this symbol." It is
-- diagnostic evidence for a human to review before setting
-- macro.boost_scale/penalty_scale (config/feature_flags.yaml) — it does
-- NOT auto-tune them and does not feed back into the live Kelly multiplier.
--
-- Population + formula: identical to nt_calibration_bias_by_bucket
-- (migration 121) — win/loss-resolved nt_signal_sim rows only (outcome IN
-- tp_hit/sl_hit; expired excluded, same rule as everywhere else), grouped
-- instead by (symbol, alignment) where alignment = aligned/opposed/neutral
-- (the signal's implied direction vs. the macro_label captured on the row
-- at signal time). expectancy_r reuses the exact bug-fixed formula from
-- migration 121's module (win_rate * avg_rr_winners - (1 - win_rate) * 1),
-- not reinvented.
--
-- Column-style follows migrations 120-124: nt_ prefix, snake_case,
-- service-role-only RLS.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS nt_macro_edge_by_symbol (
  symbol         TEXT NOT NULL,
  alignment      TEXT NOT NULL CHECK (alignment IN ('aligned', 'opposed', 'neutral')),
  n              INTEGER NOT NULL,
  win_rate       DOUBLE PRECISION,
  avg_rr_winners DOUBLE PRECISION,
  expectancy_r   DOUBLE PRECISION,
  computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, alignment)
);

COMMENT ON TABLE nt_macro_edge_by_symbol IS
  'Win/loss+PnL evidence for the macro filter (nt_macro_regime, migration 124): per symbol, expectancy_r split by whether the trade agreed (aligned), disagreed (opposed), or the macro read was neutral at signal time. Diagnostic only — read manually to decide macro.boost_scale/penalty_scale (config/feature_flags.yaml); does not auto-tune or feed back into live sizing.';

ALTER TABLE nt_macro_edge_by_symbol ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_macro_edge_by_symbol;
CREATE POLICY "Service role full access" ON nt_macro_edge_by_symbol
  FOR ALL TO service_role USING (true) WITH CHECK (true);
