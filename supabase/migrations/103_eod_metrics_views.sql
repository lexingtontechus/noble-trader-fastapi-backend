-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 103: EOD metrics views (signal-lookback feature)
--
-- Read-model views over nt_signal_sim (+ nt_sweep_result for backtest drift)
-- consumed by the EOD resolver and calibration loop:
--
--   v_eod_calibration_bias   — Predicted P_win vs. realized win rate per
--                              day+symbol over the last 7 days, with
--                              OVERCONFIDENT / UNDERCONFIDENT / CALIBRATED tags.
--   v_eod_sweep_drift        — Realized win rate (last 7 days of signals) vs.
--                              the latest backtest win_rate per symbol
--                              (nt_sweep_result), so sweep-LIGHT optimism vs.
--                              sweep-HEAVY backtest reality is visible.
--   v_eod_exit_attribution   — Resolved-outcome breakdown per regime × signal
--                              × outcome (count, win rate, avg hold, $ PnL).
--   v_eod_parameter_decay    — Realized win rate per symbol × brick geometry ×
--                              day, so parameter effectiveness decay is visible
--                              as a time series.
--
-- Views are read-only; no data is required for creation. All use
-- CREATE OR REPLACE VIEW so re-applying the migration is idempotent.
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. v_eod_calibration_bias — predicted vs. realized per day+symbol ────
-- bias > 0  ⇒ system predicted higher win probability than realized
--             (OVERCONFIDENT). bias < 0 ⇒ UNDERCONFIDENT.
CREATE OR REPLACE VIEW v_eod_calibration_bias AS
SELECT
  DATE(ts)                                            AS day,
  symbol,
  AVG(p_win)                                          AS avg_predicted_p_win,
  AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS realized_win_rate,
  AVG(p_win) - AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS bias,
  CASE
    WHEN AVG(p_win) - AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) > 0.10 THEN 'OVERCONFIDENT'
    WHEN AVG(p_win) - AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) < -0.10 THEN 'UNDERCONFIDENT'
    ELSE 'CALIBRATED'
  END                                                 AS status
FROM nt_signal_sim
WHERE outcome IN ('tp_hit', 'sl_hit')
  AND ts >= now() - interval '7 days'
GROUP BY DATE(ts), symbol
ORDER BY day DESC;

-- ─── 2. v_eod_sweep_drift — realized wr vs. latest backtest wr per symbol ──
-- Realized win rate is computed over the same 7-day lookback window as the
-- calibration view; backtest_wr is the MOST RECENT nt_sweep_result.win_rate
-- for the symbol (DISTINCT ON symbol, ordered by created_at DESC).
-- win_rate_drift > 0 ⇒ backtest outperforms live signals (optimism gap).
CREATE OR REPLACE VIEW v_eod_sweep_drift AS
SELECT
  sig.symbol,
  sig.realized_wr,
  bt.win_rate      AS backtest_wr,
  bt.win_rate - sig.realized_wr AS win_rate_drift,
  bt.last_sweep_ts
FROM (
  SELECT
    symbol,
    AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS realized_wr
  FROM nt_signal_sim
  WHERE outcome IN ('tp_hit', 'sl_hit')
    AND ts >= now() - interval '7 days'
  GROUP BY symbol
) sig
LEFT JOIN (
  SELECT DISTINCT ON (symbol)
    symbol,
    win_rate,
    created_at AS last_sweep_ts
  FROM nt_sweep_result
  WHERE win_rate IS NOT NULL
  ORDER BY symbol, created_at DESC
) bt ON bt.symbol = sig.symbol;

-- ─── 3. v_eod_exit_attribution — regime × signal × outcome breakdown ─────
CREATE OR REPLACE VIEW v_eod_exit_attribution AS
SELECT
  regime,
  signal,
  outcome,
  COUNT(*)                                            AS n_signals,
  AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS win_rate,
  AVG(hold_bars)                                      AS avg_hold_bars,
  SUM(pnl_dollars)                                    AS total_pnl_dollars
FROM nt_signal_sim
WHERE outcome IN ('tp_hit', 'sl_hit')
  AND regime IS NOT NULL
GROUP BY regime, signal, outcome;

-- ─── 4. v_eod_parameter_decay — win rate over time per brick geometry ─────
-- Grouping by day makes parameter-effectiveness decay visible as a time
-- series: same (brick_size, sl_bricks, tp_bricks) config, win rate over days.
CREATE OR REPLACE VIEW v_eod_parameter_decay AS
SELECT
  symbol,
  brick_size,
  sl_bricks,
  tp_bricks,
  DATE(ts)                                            AS day,
  COUNT(*)                                            AS n_resolved,
  AVG(CASE WHEN outcome = 'tp_hit' THEN 1.0 ELSE 0.0 END) AS realized_win_rate
FROM nt_signal_sim
WHERE outcome IN ('tp_hit', 'sl_hit')
GROUP BY symbol, brick_size, sl_bricks, tp_bricks, DATE(ts);
