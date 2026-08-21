-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 115: v_eod_calibration_bias_latest — one row per symbol (latest day)
-- (2026-08-20 — calibration panel symbol-count fix)
--
-- The admin and Talaria plugins' "Calibration bias (7d)" panel queried
-- v_eod_calibration_bias with limit:10 and order:day.desc. Since that view
-- returns one row PER DAY per symbol, 14 active symbols produce 14 rows on
-- the most recent day — PostgREST returned only 10, silently dropping 4 symbols.
--
-- This view returns exactly ONE row per symbol (the most recent day's data),
-- so the plugins can replace:
--   v_eod_calibration_bias, { limit: '10', order: 'day.desc' }
-- with:
--   v_eod_calibration_bias_latest, { }  (no limit/order needed)
--
-- Idempotent: CREATE OR REPLACE.
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW v_eod_calibration_bias_latest AS
SELECT DISTINCT ON (symbol)
  day,
  symbol,
  avg_predicted_p_win,
  realized_win_rate,
  bias,
  status
FROM v_eod_calibration_bias
ORDER BY symbol, day DESC;

-- Grant anon (public plugin) SELECT access — same as v_eod_calibration_bias (migration 107).
GRANT SELECT ON v_eod_calibration_bias_latest TO anon;
