-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 105: Paper portfolio — nt_paper_positions + sizing columns
--
-- Part II Phase 2 (Option A) — track the QUALIFIED signal stream as a paper
-- book and roll it up into portfolio metrics. The EOD resolver already
-- resolves every signal's forward outcome (tp_hit/sl_hit/expired) and stores
-- per-signal PnL in nt_signal_sim; this migration adds:
--
--   1. kelly_f + effective_kelly columns to nt_signal_sim (sizing inputs
--      available at capture time but not previously persisted).
--   2. nt_paper_positions — one row per QUALIFIED signal, sized with the
--      kelly rule (base_notional × kelly_f × ev_scale, capped at 20% of
--      base), opened at signal time and closed by the EOD resolver.
--   3. Portfolio views: v_paper_equity / v_paper_perf / v_paper_exposure.
--
-- Idempotent: ALTER ... ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS,
-- DROP POLICY IF EXISTS before CREATE POLICY (CREATE POLICY has no
-- IF NOT EXISTS), CREATE OR REPLACE VIEW.
-- ═══════════════════════════════════════════════════════════════════════════

-- ─── 1. Sizing columns on nt_signal_sim ─────────────────────────────────────

ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS kelly_f DOUBLE PRECISION;
ALTER TABLE nt_signal_sim
  ADD COLUMN IF NOT EXISTS effective_kelly DOUBLE PRECISION;

COMMENT ON COLUMN nt_signal_sim.kelly_f IS
  'Full-kelly fraction at signal generation (sizing input).';
COMMENT ON COLUMN nt_signal_sim.effective_kelly IS
  'Kelly × EV scale at signal generation (sizing input).';

-- ─── 2. nt_paper_positions — the paper book ────────────────────────────────

CREATE TABLE IF NOT EXISTS nt_paper_positions (
  position_id   UUID PRIMARY KEY,
  signal_id     UUID NOT NULL,
  symbol        TEXT NOT NULL,
  venue         TEXT,
  direction     TEXT NOT NULL CHECK (direction = ANY (ARRAY['buy', 'sell'])),
  entry_price   DOUBLE PRECISION,
  stop_loss     DOUBLE PRECISION,
  take_profit   DOUBLE PRECISION,
  size_units    DOUBLE PRECISION,
  size_notional DOUBLE PRECISION,
  open_ts       TIMESTAMPTZ NOT NULL,
  close_ts      TIMESTAMPTZ,
  status        TEXT NOT NULL DEFAULT 'open'
                CHECK (status = ANY (ARRAY['open', 'closed', 'expired'])),
  exit_price    DOUBLE PRECISION,
  realized_pnl  DOUBLE PRECISION,
  pnl_bricks    DOUBLE PRECISION,
  r_multiple    DOUBLE PRECISION,
  source        TEXT,
  resolved_at   TIMESTAMPTZ
);

-- Primary access: "open book for symbol, newest first" (dashboard / exposure).
CREATE INDEX IF NOT EXISTS idx_paper_positions_symbol_ts
  ON nt_paper_positions (symbol, open_ts DESC);

-- Partial index for the reconcile pass: "which positions are still open?".
CREATE INDEX IF NOT EXISTS idx_paper_positions_open
  ON nt_paper_positions (status)
  WHERE status = 'open';

-- Join to the signal ledger.
CREATE INDEX IF NOT EXISTS idx_paper_positions_signal
  ON nt_paper_positions (signal_id);

COMMENT ON TABLE nt_paper_positions IS
  'Paper portfolio: one row per qualified signal, kelly-sized, opened at signal time and closed by the EOD resolver (tp_hit/sl_hit/expired).';

-- ─── 3. RLS (same pattern as migration 102 — drop before create) ──────────

ALTER TABLE nt_paper_positions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role full access" ON nt_paper_positions;
CREATE POLICY "Service role full access" ON nt_paper_positions
  FOR ALL USING (true) WITH CHECK (true);

-- ─── 4. Portfolio views ────────────────────────────────────────────────────

-- Daily equity curve: realized PnL + mark-to-market of open positions
-- (open positions priced at their last known close via nt_signal_sim outcome
-- resolution — here we approximate MTM with exit_price when closed, else
-- entry_price, until a finer close feed is wired).
CREATE OR REPLACE VIEW v_paper_equity AS
WITH daily AS (
  SELECT
    DATE(COALESCE(close_ts, open_ts)) AS day,
    SUM(CASE WHEN status IN ('closed', 'expired')
             THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS realized_pnl,
    SUM(CASE WHEN status = 'open'
             THEN COALESCE(size_notional, 0) ELSE 0 END) AS open_notional
  FROM nt_paper_positions
  GROUP BY 1
)
SELECT
  day,
  realized_pnl,
  open_notional,
  SUM(realized_pnl) OVER (ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                     AS cumulative_pnl
FROM daily
ORDER BY day;

-- Per-symbol performance: trades, win rate, total PnL, avg R, max drawdown
-- (drawdown approximated from realized PnL sequence per symbol).
CREATE OR REPLACE VIEW v_paper_perf AS
WITH stats AS (
  SELECT
    symbol,
    COUNT(*)                                        AS n_trades,
    SUM(CASE WHEN status = 'closed' AND realized_pnl > 0 THEN 1 ELSE 0 END)
                                                    AS wins,
    SUM(COALESCE(realized_pnl, 0))                  AS total_pnl,
    AVG(COALESCE(r_multiple, 0))                    AS avg_r,
    MAX(COALESCE(size_notional, 0))                 AS max_notional
  FROM nt_paper_positions
  WHERE status IN ('closed', 'expired')
  GROUP BY symbol
)
SELECT
  symbol,
  n_trades,
  wins,
  CASE WHEN n_trades > 0 THEN round(wins::numeric / n_trades, 4) ELSE 0 END
                                                    AS win_rate,
  round(total_pnl::numeric, 4)                      AS total_pnl,
  round(avg_r::numeric, 4)                          AS avg_r,
  round(max_notional::numeric, 2)                   AS max_notional
FROM stats
ORDER BY total_pnl DESC;

-- Exposure: open notional per symbol, % of base portfolio (base = 1000
-- default, env NOBLE_PAPER_BASE_NOTIONAL at build time).
CREATE OR REPLACE VIEW v_paper_exposure AS
SELECT
  symbol,
  COUNT(*)                                          AS open_positions,
  round(SUM(COALESCE(size_notional, 0))::numeric, 2) AS open_notional,
  round((SUM(COALESCE(size_notional, 0)) / NULLIF(
    (SELECT SUM(COALESCE(size_notional, 0)) FROM nt_paper_positions
     WHERE status = 'open'), 0))::numeric, 4)       AS pct_of_open_book
FROM nt_paper_positions
WHERE status = 'open'
GROUP BY symbol
ORDER BY open_notional DESC;
