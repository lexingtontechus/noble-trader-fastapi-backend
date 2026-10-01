-- ═══════════════════════════════════════════════════════════════════════════
-- 190: nt_daily_close — durable daily-close storage, independent of brick retention
--
-- Root cause (2026-10-01, worklog
-- 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.1/§13.6, "Track A"):
-- v_talaria_daily_close (nobletradingapp migration 0011) resamples
-- nt_renko_bricks directly for correlation's 120-day window
-- (nobletradingapp-backend's fetch_daily_closes, DAILY_CLOSE_LOOKBACK_DAYS).
-- nt_renko_bricks ALSO needs to be prunable to its designed ~2-day live-grid
-- steady state (renko_brick_retention_days) for the chart/plugin's "last 10
-- bricks" need (nobletradingapp/docs/worklog-renko-bricks-retention-contract.md)
-- AND to stop growing unbounded (924k+ rows, confirmed dominant cause: the
-- nightly stale-brick_size pruner's roster discovery
-- (renko_retention.py::_distinct_symbols()) only ever sees 2 of 14 active
-- symbols — PostgREST caps GET response rows at 1000 regardless of the
-- `limit=10000` param requested, and those 1000 unordered rows happened to
-- cover only AAPL/AUDUSD).
--
-- These two needs (2-day live chart grid vs. 120-day correlation history)
-- are in direct conflict if correlation keeps reading nt_renko_bricks
-- directly — fixing retention to finally work would immediately regress
-- correlation back to "2 complete sessions across 14 symbols" (the exact
-- bug nobletradingapp/docs/worklog-portfolio-analytics-cache-and-thin-history.md
-- already fixed around once, 2026-09-30, from a different root cause).
--
-- Fix: a small, durable table — one row per (symbol, session_date) — that
-- persists correlation's close-price history independently of brick-level
-- retention. Sized at symbols × trading-days (tens of thousands of rows
-- over a year for a 14-39 symbol roster), not hundreds of thousands.
--
-- This migration ONLY creates the table + backfills the current 120-day
-- window. It does NOT touch nt_renko_bricks, does NOT change any existing
-- retention behavior, and does NOT repoint v_talaria_daily_close (that is
-- nobletradingapp's own change, out of scope for this repo — see worklog
-- §13.6's cross-repo table). Zero behavior change to any existing consumer.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.nt_daily_close (
  symbol        text NOT NULL,
  session_date  date NOT NULL,
  close_price   numeric NOT NULL,
  ts            timestamptz NOT NULL,
  captured_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol, session_date)
);

ALTER TABLE public.nt_daily_close ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Service role full access" ON public.nt_daily_close;
CREATE POLICY "Service role full access" ON public.nt_daily_close
  FOR ALL USING (true) WITH CHECK (true);

-- Anon read, same precedent as nt_renko_bricks itself (migration 108) —
-- nobletradingapp's eventual v_talaria_daily_close repoint will need this.
DROP POLICY IF EXISTS "anon read nt_daily_close" ON public.nt_daily_close;
CREATE POLICY "anon read nt_daily_close" ON public.nt_daily_close
  FOR SELECT USING (true);

GRANT SELECT ON public.nt_daily_close TO anon;

COMMENT ON TABLE public.nt_daily_close IS
  'One closing price per (symbol, session_date), independent of '
  'nt_renko_bricks'' retention. Backfilled from nt_renko_bricks (120-day '
  'window) by this migration; kept current by the nightly '
  'daily_close_snapshot.py module (Part VI of orchestrator.py''s '
  'eod-lookback cycle). Exists so correlation''s 120-day history survives '
  'nt_renko_bricks being pruned to its designed ~2-day live-grid steady '
  'state. Worklog 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.1.';

-- ─── One-time backfill: preserve the current 120-day correlation window ───
-- Mirrors v_talaria_daily_close's own DISTINCT ON logic exactly (migration
-- 0011, nobletradingapp repo) — last brick of the day (highest brick_index,
-- tie-broken by ts) as that day's close. 120 days matches
-- nobletradingapp-backend's DAILY_CLOSE_LOOKBACK_DAYS.
INSERT INTO nt_daily_close (symbol, session_date, close_price, ts)
SELECT DISTINCT ON (b.symbol, b.session_date)
  b.symbol, b.session_date, b.close_price, b.ts
FROM nt_renko_bricks b
WHERE b.session_date >= (CURRENT_DATE - INTERVAL '120 days')
ORDER BY b.symbol, b.session_date, b.brick_index DESC, b.ts DESC
ON CONFLICT (symbol, session_date) DO NOTHING;

-- POST-RUN VERIFICATION:
--   SELECT count(*), count(DISTINCT symbol), min(session_date), max(session_date)
--   FROM nt_daily_close;
--   -- expect roughly symbols × trading-days-in-120-days rows, one row per
--   -- (symbol, session_date), newest session_date == today or yesterday.
-- ═══════════════════════════════════════════════════════════════════════════
