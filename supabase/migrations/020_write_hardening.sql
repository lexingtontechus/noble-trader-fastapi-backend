-- ═══════════════════════════════════════════════════════════════
-- 020: SUPABASE WRITE HARDENING (Phase 2 — P2.a + P2.e)
--
-- GOALS:
--   1. Formally declare `sim_errors` (referenced in code since
--      migration 017 but never created via migration — it had to
--      exist via manual SQL editor).
--   2. Create `sim_write_failures` dead-letter table for Supabase
--      POSTs that fail even after retries. Prevents silent data
--      loss when PostgREST 5xx's during burst windows.
--   3. Add composite index on sim_regime_log(symbol, detected_at)
--      WHERE regime_shift = true — fixes the unindexed scan in
--      countShifts24h() that was scanning per-symbol on every
--      heavy sweep.
--
-- RATIONALE (P2.a):
--   Investigation (worklog.md assessment-1) found that
--   `supabaseRestWithRetry()` retries 1x only on 503/conn/reset.
--   Genuine 500/502/504 from PostgREST under burst load goes
--   straight to `logError()` — which is itself another Supabase
--   POST that can also fail. With single-row POSTs (~16 concurrent
--   in peak windows), this caused silent write failures that the
--   retry-stale cron then had to pick up.
--
--   The dead-letter table gives us a durable fallback: when a write
--   to sim_sweep_results / sim_regime_log / sim_errors fails after
--   all retries, the payload + error are written to
--   sim_write_failures for later replay. The replay itself can be
--   triggered manually (admin route) or by a future cron job.
--
-- RATIONALE (P2.e):
--   countShifts24h() runs once per heavy sweep per symbol. Its
--   query `symbol=eq.X&regime_shift=eq.true&detected_at=gte.Y` was
--   hitting the partial index `idx_regime_shift` (created in
--   migration 001) which only indexes `regime_shift WHERE true` —
--   the symbol + detected_at filters still triggered a scan.
--   Adding a composite partial index `(symbol, detected_at) WHERE
--   regime_shift = true` turns this into an index-only lookup.
-- ═══════════════════════════════════════════════════════════════

-- ──────────────────────────────────────────────────────────────
-- STEP 1: Formally declare sim_errors (idempotent)
-- ──────────────────────────────────────────────────────────────
-- Referenced from _shared/utils.ts:logError() and from
-- sweep/index.ts error paths. If it already exists (created out of
-- band), CREATE IF NOT EXISTS is a no-op.

CREATE TABLE IF NOT EXISTS sim_errors (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT,
  fn_name     TEXT NOT NULL,
  error_msg   TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sim_errors_symbol      ON sim_errors(symbol);
CREATE INDEX IF NOT EXISTS idx_sim_errors_created_at  ON sim_errors(created_at DESC);

ALTER TABLE sim_errors ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access to sim_errors"
  ON sim_errors FOR ALL
  USING (true) WITH CHECK (true);

-- ──────────────────────────────────────────────────────────────
-- STEP 2: Dead-letter table for failed Supabase writes
-- ──────────────────────────────────────────────────────────────
-- Written by _shared/utils.ts when logSweepResult/logRegimeEntry/
-- logError can't reach their target table after all retries.
-- Replayed by an admin route (TBD) or future cron.

CREATE TABLE IF NOT EXISTS sim_write_failures (
  id              BIGSERIAL PRIMARY KEY,
  target_table    TEXT NOT NULL,                     -- 'sim_sweep_results' | 'sim_regime_log' | 'sim_errors'
  operation       TEXT NOT NULL DEFAULT 'INSERT',    -- 'INSERT' | 'UPDATE'
  payload         JSONB NOT NULL,                    -- the row(s) we tried to write
  error_msg       TEXT NOT NULL,                     -- last error message
  error_code      INTEGER,                           -- HTTP status code if known
  retry_count     INTEGER NOT NULL DEFAULT 0,        -- times we retried before dead-lettering
  status          TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'replayed' | 'dropped'
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_write_failures_status      ON sim_write_failures(status);
CREATE INDEX IF NOT EXISTS idx_write_failures_created_at  ON sim_write_failures(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_write_failures_target      ON sim_write_failures(target_table);

ALTER TABLE sim_write_failures ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role full access to sim_write_failures"
  ON sim_write_failures FOR ALL
  USING (true) WITH CHECK (true);

-- ──────────────────────────────────────────────────────────────
-- STEP 3: Composite index for countShifts24h() (P2.e)
-- ──────────────────────────────────────────────────────────────
-- Replaces the unindexed scan in
--   supabase/functions/_shared/utils.ts:countShifts24h()
-- Query: symbol=eq.X & regime_shift=eq.true & detected_at=gte.Y
-- Old partial index (migration 001):
--   idx_regime_shift ON sim_regime_log(regime_shift) WHERE regime_shift = true
--   (only filtered on the boolean — symbol + timestamp still scanned)
-- New composite partial index:
--   Covers (symbol, detected_at) so the query becomes an index range scan.

CREATE INDEX IF NOT EXISTS idx_regime_symbol_detected_shift
  ON sim_regime_log(symbol, detected_at DESC)
  WHERE regime_shift = true;

-- Old index is now redundant but left in place (drop is risky mid-deploy;
-- it's tiny since it only indexes the boolean). To drop later:
--   DROP INDEX IF EXISTS idx_regime_shift;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (run manually after applying):
--   \d sim_errors
--   \d sim_write_failures
--   \d sim_regime_log
--   -- Should see idx_regime_symbol_detected_shift in the indexes list
--
-- Test countShifts24h speedup (run EXPLAIN ANALYZE before/after):
--   EXPLAIN ANALYZE
--   SELECT id FROM sim_regime_log
--   WHERE symbol = 'BTC'
--     AND regime_shift = true
--     AND detected_at >= now() - interval '24 hours';
-- ═══════════════════════════════════════════════════════════════
