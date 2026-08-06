-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration 102: nt_signal_sim + nt_eod_actions
--
-- EOD signal-lookback feature — persist every qualified buy/sell signal from the
-- sweep orchestrator into a simulation ledger so the EOD resolver can score it
-- against realized price action and the calibration loop can measure prediction
-- vs. outcome bias.
--
-- Tables:
--   nt_signal_sim  — One row per generated signal (buy/sell), with the full
--                    qualification + sizing context at generation time
--                    (entry/stop/take, brick geometry, P_win, EV, regime) and
--                    the EOD-resolution outcome columns (tp_hit/sl_hit/expired/
--                    open) filled in by the EOD resolver.
--   nt_eod_actions — Audit ledger for every self-correction the EOD process
--                    takes (emergency_heavy_sweep, bayesian_penalty,
--                    kelly_reduction, parameter_decay_check), so system
--                    interventions are traceable per symbol over time.
--
-- Both use the nt_ prefix to match the application-layer convention (migration
-- 100). Column-style follows migrations 029/036: snake_case, DOUBLE PRECISION
-- for all prob/price fields, TIMESTAMPTZ for time fields, CHECK constraints via
-- = ANY (ARRAY[...]).
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. nt_signal_sim — signal ledger ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_signal_sim (
  signal_id       UUID PRIMARY KEY,
  symbol          TEXT NOT NULL,
  venue           TEXT,
  ts              TIMESTAMPTZ NOT NULL,          -- sweep_timestamp of signal generation
  signal          TEXT NOT NULL,                 -- buy | sell
  entry_price     DOUBLE PRECISION,
  stop_loss       DOUBLE PRECISION,
  take_profit     DOUBLE PRECISION,
  brick_size      DOUBLE PRECISION,
  sl_bricks       INTEGER,
  tp_bricks       INTEGER,
  p_win           DOUBLE PRECISION,
  ev              DOUBLE PRECISION,
  regime          TEXT,
  regime_conf     DOUBLE PRECISION,
  source          TEXT,                          -- sweep-light | sweep-heavy
  qualified       BOOLEAN NOT NULL DEFAULT false,
  outcome         TEXT,                          -- tp_hit | sl_hit | expired | open (null until EOD resolver)
  exit_price      DOUBLE PRECISION,
  exit_ts         TIMESTAMPTZ,
  hold_bars       INTEGER,
  mfe             DOUBLE PRECISION,              -- max favorable excursion (bricks)
  mae             DOUBLE PRECISION,              -- max adverse excursion (bricks)
  pnl_bricks      DOUBLE PRECISION,
  pnl_dollars     DOUBLE PRECISION,
  resolved_at     TIMESTAMPTZ
);

-- Signal direction check (mirrors nt_sweep_result.signal constraint style, but
-- nt_signal_sim only records actionable buy/sell — no neutral rows).
ALTER TABLE nt_signal_sim
  DROP CONSTRAINT IF EXISTS nt_signal_sim_signal_check;
ALTER TABLE nt_signal_sim
  ADD CONSTRAINT nt_signal_sim_signal_check
  CHECK (signal = ANY (ARRAY['buy', 'sell']));

-- Outcome domain check (tp_hit / sl_hit / expired / open; NULL until resolved).
ALTER TABLE nt_signal_sim
  DROP CONSTRAINT IF EXISTS nt_signal_sim_outcome_check;
ALTER TABLE nt_signal_sim
  ADD CONSTRAINT nt_signal_sim_outcome_check
  CHECK (outcome IS NULL OR outcome = ANY (ARRAY['tp_hit', 'sl_hit', 'expired', 'open']));

-- Primary access pattern: "all signals for symbol X, newest first" (dashboard /
-- lookback queries).
CREATE INDEX IF NOT EXISTS idx_signal_sim_symbol_ts
  ON nt_signal_sim (symbol, ts DESC);

-- Partial index for the EOD resolver: "which signals are still pending?". Kept
-- small — only unresolved rows (outcome IS NULL) are indexed.
CREATE INDEX IF NOT EXISTS idx_signal_sim_pending
  ON nt_signal_sim (outcome)
  WHERE outcome IS NULL;

COMMENT ON TABLE nt_signal_sim IS
  'EOD signal-lookback ledger: one row per generated buy/sell signal with generation-time context (entry/SL/TP, brick geometry, p_win, ev, regime) and EOD-resolution outcome columns (tp_hit|sl_hit|expired|open).';
COMMENT ON COLUMN nt_signal_sim.ts IS
  'Sweep timestamp of signal generation.';
COMMENT ON COLUMN nt_signal_sim.source IS
  'Signal source: sweep-light | sweep-heavy.';
COMMENT ON COLUMN nt_signal_sim.outcome IS
  'EOD resolution: tp_hit | sl_hit | expired | open. NULL until the EOD resolver scores the signal.';
COMMENT ON COLUMN nt_signal_sim.mfe IS
  'Max favorable excursion in bricks over the signal lifetime (EOD-resolved).';
COMMENT ON COLUMN nt_signal_sim.mae IS
  'Max adverse excursion in bricks over the signal lifetime (EOD-resolved).';

-- ─── 2. nt_eod_actions — self-correction audit ledger ─────────────────────

CREATE TABLE IF NOT EXISTS nt_eod_actions (
  action_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol      TEXT,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  action      TEXT NOT NULL,                     -- emergency_heavy_sweep | bayesian_penalty | kelly_reduction | parameter_decay_check
  bias        DOUBLE PRECISION,                  -- measured calibration bias at time of action
  detail      JSONB                              -- action-specific context (penalty applied, params decayed, etc.)
);

ALTER TABLE nt_eod_actions
  DROP CONSTRAINT IF EXISTS nt_eod_actions_action_check;
ALTER TABLE nt_eod_actions
  ADD CONSTRAINT nt_eod_actions_action_check
  CHECK (action = ANY (ARRAY['emergency_heavy_sweep', 'bayesian_penalty', 'kelly_reduction', 'parameter_decay_check']));

CREATE INDEX IF NOT EXISTS idx_eod_actions_symbol_ts
  ON nt_eod_actions (symbol, ts DESC);

COMMENT ON TABLE nt_eod_actions IS
  'Audit ledger for EOD self-correction actions (emergency_heavy_sweep, bayesian_penalty, kelly_reduction, parameter_decay_check), enabling traceability of system interventions per symbol over time.';
COMMENT ON COLUMN nt_eod_actions.detail IS
  'Action-specific context as JSONB (e.g. penalty magnitude, decayed parameter, sweep trigger).';

-- ─── 3. Row Level Security ─────────────────────────────────────────────────
-- Application-layer tables are written by the service role (backend + edge
-- functions) only. Same policy shape as migration 100.

ALTER TABLE nt_signal_sim ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON nt_signal_sim
  FOR ALL USING (true) WITH CHECK (true);

ALTER TABLE nt_eod_actions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access" ON nt_eod_actions
  FOR ALL USING (true) WITH CHECK (true);
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
