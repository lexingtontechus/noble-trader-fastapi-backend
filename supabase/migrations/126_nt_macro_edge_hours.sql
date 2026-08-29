-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 126: nt_macro_edge_by_symbol — time-to-resolve columns
--
-- The macro-edge win/loss+PnL evidence (migration 125) answers "does
-- trading with the macro trend win more"; it didn't answer whether it
-- also resolves FASTER — capital turnover matters even at equal win
-- rate. Same hours-to-resolve computation nt_bracket_reachability
-- already does (migration 120), keyed by alignment instead of
-- distance/ATR bucket.
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_macro_edge_by_symbol
  ADD COLUMN IF NOT EXISTS median_hours_to_resolve DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p75_hours DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p90_hours DOUBLE PRECISION;

COMMENT ON COLUMN nt_macro_edge_by_symbol.median_hours_to_resolve IS
  'Median hours from signal ts to resolved_at, over tp_hit/sl_hit rows in this (symbol, alignment) bucket — same definition as nt_bracket_reachability.median_hours_to_resolve.';
COMMENT ON COLUMN nt_macro_edge_by_symbol.p75_hours IS
  '75th percentile hours-to-resolve, same population as median_hours_to_resolve.';
COMMENT ON COLUMN nt_macro_edge_by_symbol.p90_hours IS
  '90th percentile hours-to-resolve, same population as median_hours_to_resolve.';
