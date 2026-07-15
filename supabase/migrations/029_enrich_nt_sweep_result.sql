-- ═══════════════════════════════════════════════════════════════════════════════
-- 029: Enrich nt_sweep_result with full signal data
--
-- The orchestrator now computes and sends these fields to log_sweep_result(),
-- but they were missing from the nt_sweep_result table. This migration adds
-- them so every sweep record has the complete signal picture:
--
--   effective_kelly  — Kelly after EV scaling + markov multiplier
--   signal           — buy / sell / neutral
--   ev               — expected value
--   p_win            — blended probability of winning
--   qualified        — whether signal passed all qualification gates
--   p_timesfm        — TimesFM probability (null if unavailable)
--   entry_price      — trade entry price
--   stop_loss        — stop loss price
--   take_profit      — take profit price
--   aggression       — passive / mid / aggressive
--   venue            — exchange name (e.g., Coinbase, OANDA, Hyperliquid)
--
-- Also adds a 'source' column if it doesn't exist (was being sent but
-- may not have been in the original schema).
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Add new columns ───────────────────────────────────────────────────
ALTER TABLE nt_sweep_result
  ADD COLUMN IF NOT EXISTS effective_kelly DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS signal TEXT DEFAULT 'neutral',
  ADD COLUMN IF NOT EXISTS ev DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_win DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS qualified BOOLEAN DEFAULT false,
  ADD COLUMN IF NOT EXISTS p_timesfm DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS entry_price DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS stop_loss DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS take_profit DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS aggression TEXT DEFAULT 'mid',
  ADD COLUMN IF NOT EXISTS venue TEXT,
  ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'sweep-light';

-- ─── 2. Add CHECK constraint for signal field ─────────────────────────────
ALTER TABLE nt_sweep_result
  DROP CONSTRAINT IF EXISTS nt_sweep_result_signal_check;
ALTER TABLE nt_sweep_result
  ADD CONSTRAINT nt_sweep_result_signal_check
  CHECK (signal = ANY (ARRAY['buy', 'sell', 'neutral']));

-- ─── 3. Add CHECK constraint for aggression field ─────────────────────────
ALTER TABLE nt_sweep_result
  DROP CONSTRAINT IF EXISTS nt_sweep_result_aggression_check;
ALTER TABLE nt_sweep_result
  ADD CONSTRAINT nt_sweep_result_aggression_check
  CHECK (aggression = ANY (ARRAY['passive', 'mid', 'aggressive']));

-- ─── 4. Add index on qualified (for filtering qualified signals) ──────────
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_qualified
  ON nt_sweep_result(qualified)
  WHERE qualified = true;

-- ─── 5. Add index on signal (for filtering buy/sell) ──────────────────────
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_signal
  ON nt_sweep_result(signal)
  WHERE signal IN ('buy', 'sell');

-- ─── 6. Add index on venue (for per-exchange analysis) ────────────────────
CREATE INDEX IF NOT EXISTS idx_nt_sweep_result_venue
  ON nt_sweep_result(venue);

-- ─── 7. Verify ────────────────────────────────────────────────────────────
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = 'nt_sweep_result'
ORDER BY ordinal_position;
