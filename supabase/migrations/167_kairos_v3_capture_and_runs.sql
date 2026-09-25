-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 167: Kairos v3 P0 — geometry-free capture columns + regime-run view
--
-- worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md §6 P0.2/P0.3/P0.5 (user-approved
-- 2026-09-24: "#3 approve"). Additive only: nullable columns on nt_signal_sim
-- and one VIEW. No new table, no data change, no behaviour change —
-- qualification / sizing / SL/TP / delivery never read these.
--
--   drift_12h_pct / drift_48h_pct — signed close move 12 / 48 completed 1h bars
--     after the entry bar, % of entry (same definition as drift_1h/4h/24h,
--     migration 121). Written by eod_resolver (12h when available) and the
--     nightly kairos.backfill_drift_horizons pass (48h + any 12/24h an early
--     TP/SL settle left NULL). Independent of SL/TP geometry.
--   rel_volume_1h  — last completed bar's volume ÷ median volume of the same
--     UTC hour in the sweep window (tick volume for OANDA, base for Coinbase).
--   clv_1h         — close-location value of the last completed bar, [-1, 1].
--   efficiency_24h — Kaufman efficiency ratio over the last 24 completed bars.
--   atr_ratio_run  — ATR now ÷ ATR at the start of the current regime-cell run
--     (in-memory tracker; resets on restart → see starts_after_gap below).
--   spread_bps     — live (ask − bid)/mid × 1e4 from /api/quote/batch.
--
-- v_kairos_regime_runs — one row per (symbol, regime cell) run from light-
-- sweep rows since the 2026-09-22 debounce go-live. Cell = vol tier ×
-- direction (strength collapsed), hourly grain = the hour's first light
-- tick. A run breaks when the cell changes OR the hourly series has a gap
-- (> 3h: weekend, outage, Studio restart). starts_after_gap marks runs whose
-- start is a data gap rather than an observed transition (left-censored);
-- ended_by_gap / is_open mark right-censored runs (scope §8.12).
-- ═══════════════════════════════════════════════════════════════════════════════

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS drift_12h_pct double precision,
  ADD COLUMN IF NOT EXISTS drift_48h_pct double precision,
  ADD COLUMN IF NOT EXISTS rel_volume_1h double precision,
  ADD COLUMN IF NOT EXISTS clv_1h double precision,
  ADD COLUMN IF NOT EXISTS efficiency_24h double precision,
  ADD COLUMN IF NOT EXISTS atr_ratio_run double precision,
  ADD COLUMN IF NOT EXISTS spread_bps double precision;

COMMENT ON COLUMN nt_signal_sim.drift_12h_pct IS
  'Kairos v3: signed close drift 12 completed 1h bars after entry, % of entry (geometry-free). Resolver + nightly backfill.';
COMMENT ON COLUMN nt_signal_sim.drift_48h_pct IS
  'Kairos v3: signed close drift 48 completed 1h bars after entry, % of entry (geometry-free). Nightly backfill (kairos.backfill_drift_horizons).';
COMMENT ON COLUMN nt_signal_sim.rel_volume_1h IS
  'Kairos v3: last completed 1h bar volume / median same-UTC-hour volume in the sweep window. Activity proxy (tick volume on OANDA), never compare across symbols.';
COMMENT ON COLUMN nt_signal_sim.clv_1h IS
  'Kairos v3: close-location value ((C-L)-(H-C))/(H-L) of the last completed 1h bar, [-1,1].';
COMMENT ON COLUMN nt_signal_sim.efficiency_24h IS
  'Kairos v3: Kaufman efficiency ratio |C_t - C_t-24| / sum|dC| over the last 24 completed 1h bars, [0,1].';
COMMENT ON COLUMN nt_signal_sim.atr_ratio_run IS
  'Kairos v3: ATR14 now / ATR14 at the start of the current regime-cell run (in-memory tracker; resets on Studio restart).';
COMMENT ON COLUMN nt_signal_sim.spread_bps IS
  'Kairos v3: live (ask-bid)/mid*1e4 from /api/quote/batch at capture.';

CREATE OR REPLACE VIEW v_kairos_regime_runs AS
WITH hourly AS (
  SELECT DISTINCT ON (symbol, date_trunc('hour', ts))
         symbol,
         date_trunc('hour', ts) AS hr,
         regime,
         split_part(regime, '_vol_', 1) || '_vol'
           || '|' || CASE WHEN regime ILIKE '%bull%' THEN 'bull'
                          WHEN regime ILIKE '%bear%' THEN 'bear'
                          ELSE 'flat' END AS cell
  FROM nt_signal_sim
  WHERE source = 'sweep-light'
    AND ts >= '2026-09-22T12:00:00Z'
    AND regime LIKE '%\_vol\_%'
  ORDER BY symbol, date_trunc('hour', ts), ts
),
marked AS (
  SELECT h.*,
         lag(cell) OVER w AS prev_cell,
         lag(hr)   OVER w AS prev_hr
  FROM hourly h
  WINDOW w AS (PARTITION BY symbol ORDER BY hr)
),
flagged AS (
  SELECT m.*,
         (prev_hr IS NULL OR hr - prev_hr > interval '3 hours') AS gap_before,
         (prev_hr IS NULL OR hr - prev_hr > interval '3 hours' OR cell IS DISTINCT FROM prev_cell) AS new_run
  FROM marked m
),
numbered AS (
  SELECT f.*,
         sum(CASE WHEN new_run THEN 1 ELSE 0 END) OVER (PARTITION BY symbol ORDER BY hr) AS run_seq
  FROM flagged f
),
runs AS (
  SELECT symbol,
         run_seq,
         min(cell)                        AS cell,
         min(hr)                          AS started_at,
         max(hr)                          AS last_seen_at,
         count(*)                         AS n_hours,
         bool_or(new_run AND gap_before)  AS starts_after_gap,
         count(DISTINCT regime)           AS n_labels
  FROM numbered
  GROUP BY symbol, run_seq
)
SELECT r.symbol,
       r.symbol || ':' || r.run_seq      AS run_id,
       r.run_seq,
       r.cell,
       split_part(r.cell, '|', 1)        AS vol_tier,
       split_part(r.cell, '|', 2)        AS direction,
       r.started_at,
       r.last_seen_at,
       nxt.started_at                    AS ended_at,
       EXTRACT(EPOCH FROM (COALESCE(nxt.started_at, r.last_seen_at + interval '1 hour') - r.started_at)) / 3600.0
                                         AS length_h,
       r.n_hours,
       r.n_labels,
       nxt.cell                          AS exit_cell,
       CASE WHEN nxt.cell IS NULL THEN NULL
            WHEN split_part(nxt.cell, '|', 2) <> split_part(r.cell, '|', 2)
             AND split_part(nxt.cell, '|', 1) <> split_part(r.cell, '|', 1) THEN 'vol+dir'
            WHEN split_part(nxt.cell, '|', 2) <> split_part(r.cell, '|', 2) THEN 'dir_flip'
            ELSE 'vol_change' END        AS exit_kind,
       r.starts_after_gap,
       COALESCE(nxt.starts_after_gap, false) AS ended_by_gap,
       (nxt.started_at IS NULL)          AS is_open
FROM runs r
LEFT JOIN runs nxt
  ON nxt.symbol = r.symbol AND nxt.run_seq = r.run_seq + 1;

COMMENT ON VIEW v_kairos_regime_runs IS
  'Kairos v3 P0.2: regime-cell runs (vol tier x direction) per symbol from sweep-light nt_signal_sim since 2026-09-22 (debounced labels). Hourly grain (first tick per hour). exit_kind is only meaningful when ended_by_gap=false; is_open / ended_by_gap / starts_after_gap mark censored runs. worklog/kairos/scopes/20260924_KAIROS_V3_SCOPE.md.';

GRANT SELECT ON v_kairos_regime_runs TO anon;

-- ═══════════════════════════════════════════════════════════════════════════════
-- POST-RUN VERIFICATION:
--   SELECT symbol, count(*) runs, percentile_cont(0.5) WITHIN GROUP (ORDER BY length_h) med_h
--   FROM v_kairos_regime_runs GROUP BY symbol ORDER BY symbol;
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'nt_signal_sim' AND column_name IN
--     ('drift_12h_pct','drift_48h_pct','rel_volume_1h','clv_1h','efficiency_24h','atr_ratio_run','spread_bps');
-- ═══════════════════════════════════════════════════════════════════════════════
