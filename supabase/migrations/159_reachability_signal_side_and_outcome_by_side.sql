-- Migration 159: BUY vs SELL split on reachability + EOD outcome reporting
--
-- 2026-09-22 (user): "break out BUY vs SELL TP/SL success. We are aggregating
-- TP vs SL reachability success; distinguishing BUY vs SELL gives more insight
-- into where the geometry might be skewed, ie. if the platform can't read
-- market shifts and is over/underconfident in the wrong direction."
--
-- 1. nt_bracket_reachability gains `signal` ('buy'|'sell'|'all'). Per-side rows
--    isolate direction-asymmetric TP/SL reach (e.g. buy TP often hit while sell
--    TP expires → geometry or direction skew). 'all' keeps the pre-existing
--    aggregate key shape for heavy_sweep_reachability / Stage 1 consumers.
-- 2. v_signal_outcome_by_side — qualified-stratified TP/SL/expired rates by
--    (symbol, signal, qualified) so pulse/EOD can report the live book
--    separately from the rejected research stream.

-- ─── 1. nt_bracket_reachability.signal ─────────────────────────────────────
ALTER TABLE nt_bracket_reachability
  ADD COLUMN IF NOT EXISTS signal TEXT NOT NULL DEFAULT 'all';

ALTER TABLE nt_bracket_reachability
  DROP CONSTRAINT IF EXISTS nt_bracket_reachability_signal_check;
ALTER TABLE nt_bracket_reachability
  ADD CONSTRAINT nt_bracket_reachability_signal_check
  CHECK (signal = ANY (ARRAY['buy', 'sell', 'all']));

-- Widen PK to include signal. Existing rows default to signal='all'.
ALTER TABLE nt_bracket_reachability
  DROP CONSTRAINT IF EXISTS nt_bracket_reachability_pkey;
ALTER TABLE nt_bracket_reachability
  ADD CONSTRAINT nt_bracket_reachability_pkey
  PRIMARY KEY (symbol, vol_tier, distance_atr_bucket, signal);

COMMENT ON COLUMN nt_bracket_reachability.signal IS
  'buy | sell = TP/SL reachability for that trade side only; all = pooled (pre-159 aggregate). Compare buy vs sell pct_hit_tp/sl to detect direction-asymmetric geometry or regime misreads.';

COMMENT ON TABLE nt_bracket_reachability IS
  'Read-only diagnostic: per (symbol, vol_tier, distance/ATR bucket, signal side) hit-rate and time-to-resolution from resolved nt_signal_sim. signal=buy|sell isolates directional reach; signal=all is the pooled row. Consumed by heavy_sweep_reachability (expectancy path) and Stage 1 ATR review. 2026-09-22 migration 159 added the signal dimension.';

-- ─── 2. v_signal_outcome_by_side — EOD/product quality by BUY vs SELL ──────
-- Cohorts are explicit: qualified=true is the live book; qualified=false is
-- the rejected research stream (gates + geometry diagnostics). Never mix.

CREATE OR REPLACE VIEW v_signal_outcome_by_side AS
SELECT
  symbol,
  signal,
  qualified,
  COUNT(*)                                          AS n_resolved,
  COUNT(*) FILTER (WHERE outcome = 'tp_hit')        AS tp_hit,
  COUNT(*) FILTER (WHERE outcome = 'sl_hit')        AS sl_hit,
  COUNT(*) FILTER (WHERE outcome = 'expired')       AS expired,
  ROUND(
    (COUNT(*) FILTER (WHERE outcome = 'tp_hit'))::numeric
    / NULLIF(COUNT(*), 0), 4
  )                                                 AS pct_tp,
  ROUND(
    (COUNT(*) FILTER (WHERE outcome = 'sl_hit'))::numeric
    / NULLIF(COUNT(*), 0), 4
  )                                                 AS pct_sl,
  ROUND(
    (COUNT(*) FILTER (WHERE outcome = 'expired'))::numeric
    / NULLIF(COUNT(*), 0), 4
  )                                                 AS pct_expired,
  -- closed-only win rate (tp / (tp+sl)); expired excluded — same dual-report
  -- convention as the scorecard views
  ROUND(
    (COUNT(*) FILTER (WHERE outcome = 'tp_hit'))::numeric
    / NULLIF(
        (COUNT(*) FILTER (WHERE outcome IN ('tp_hit', 'sl_hit'))), 0
      ), 4
  )                                                 AS win_pct_closed,
  MAX(resolved_at)                                  AS last_resolved_at
FROM nt_signal_sim
WHERE outcome IN ('tp_hit', 'sl_hit', 'expired')
  AND signal IN ('buy', 'sell')
GROUP BY symbol, signal, qualified;

COMMENT ON VIEW v_signal_outcome_by_side IS
  'EOD/product quality broken out by BUY vs SELL and qualified flag (migration 159). qualified=true = live book; qualified=false = rejected research stream. Use win_pct_closed + pct_expired asymmetry across buy/sell to spot direction-skewed geometry or regime over/under-confidence.';

GRANT SELECT ON v_signal_outcome_by_side TO anon, authenticated;

-- Side-by-side buy vs sell expire/TP gap per symbol (all-cohort geometry view)
CREATE OR REPLACE VIEW v_reachability_side_gap AS
SELECT
  b.symbol,
  b.vol_tier,
  b.distance_atr_bucket,
  buy.n                    AS n_buy,
  sell.n                   AS n_sell,
  buy.pct_hit_tp           AS buy_pct_hit_tp,
  sell.pct_hit_tp          AS sell_pct_hit_tp,
  ROUND(
    (COALESCE(buy.pct_hit_tp, 0) - COALESCE(sell.pct_hit_tp, 0))::numeric, 4
  )                        AS tp_gap_buy_minus_sell,
  buy.pct_hit_sl           AS buy_pct_hit_sl,
  sell.pct_hit_sl          AS sell_pct_hit_sl,
  ROUND(
    (COALESCE(buy.pct_hit_sl, 0) - COALESCE(sell.pct_hit_sl, 0))::numeric, 4
  )                        AS sl_gap_buy_minus_sell,
  buy.pct_expired          AS buy_pct_expired,
  sell.pct_expired         AS sell_pct_expired,
  ROUND(
    (COALESCE(buy.pct_expired, 0) - COALESCE(sell.pct_expired, 0))::numeric, 4
  )                        AS expire_gap_buy_minus_sell
FROM nt_bracket_reachability b
LEFT JOIN nt_bracket_reachability buy
  ON buy.symbol = b.symbol
 AND buy.vol_tier = b.vol_tier
 AND buy.distance_atr_bucket = b.distance_atr_bucket
 AND buy.signal = 'buy'
LEFT JOIN nt_bracket_reachability sell
  ON sell.symbol = b.symbol
 AND sell.vol_tier = b.vol_tier
 AND sell.distance_atr_bucket = b.distance_atr_bucket
 AND sell.signal = 'sell'
WHERE b.signal = 'all';

COMMENT ON VIEW v_reachability_side_gap IS
  'BUY minus SELL gaps on TP/SL/expired reachability by (symbol, vol_tier, distance_atr_bucket). Large |tp_gap| with similar n_buy/n_sell suggests direction-asymmetric geometry (e.g. TP always measured in the wrong side of the brick) or regime reads that systematically favor one side.';

GRANT SELECT ON v_reachability_side_gap TO anon, authenticated;

-- ─── 3. Refresh v_bracket_performance (admin) for the signal dimension ─────
-- Pre-159 this view joined every reachability row. After the side split the
-- join must pin signal='all' or admin consumers see buy/sell/all triplicates.
-- DROP + CREATE because the column list changes (adds `signal`).

DROP VIEW IF EXISTS v_bracket_performance;

CREATE VIEW v_bracket_performance AS
SELECT
  r.symbol,
  r.vol_tier,
  r.distance_atr_bucket,
  r.signal,
  r.n                        AS n_all,
  r.pct_hit_tp,
  r.pct_hit_sl,
  r.pct_expired,
  r.median_hours_to_resolve,
  r.p75_hours,
  r.p90_hours,
  r.atr_source,
  b.n                        AS n_resolved,
  b.avg_p_win,
  b.win_rate,
  b.bias,
  b.avg_rr_winners,
  b.expectancy_r,
  b.daily_expectancy_r,
  r.computed_at
FROM nt_bracket_reachability r
LEFT JOIN nt_calibration_bias_by_bucket b
  ON b.symbol = r.symbol
 AND b.distance_atr_bucket = r.distance_atr_bucket
 AND (r.vol_tier IS NOT DISTINCT FROM b.vol_tier)
WHERE r.signal = 'all'
ORDER BY r.symbol, r.distance_atr_bucket;

COMMENT ON VIEW v_bracket_performance IS
  'Admin-viewable bracket-width performance (migration 159: pinned to nt_bracket_reachability.signal=all so buy/sell side rows do not triplicate). Hit/expire rates any-outcome LEFT JOIN win/loss expectancy. Side-level detail: v_reachability_side_gap.';

GRANT SELECT ON v_bracket_performance TO anon;
