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
-- CREATE POLICY has no IF NOT EXISTS — drop first so re-running the
-- combined migration (104) is idempotent.
DROP POLICY IF EXISTS "Service role full access" ON nt_signal_sim;
CREATE POLICY "Service role full access" ON nt_signal_sim
  FOR ALL USING (true) WITH CHECK (true);

ALTER TABLE nt_eod_actions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_eod_actions;
CREATE POLICY "Service role full access" ON nt_eod_actions
  FOR ALL USING (true) WITH CHECK (true);
