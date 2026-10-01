-- 187_renko_daily_close_covering_index.sql  (2026-10-01)
--
-- Live incident: nobletradingapp's Portfolio tab "Signal risk overview"
-- panel showed "Backend timed out (cold start...)" while the Render
-- service itself was healthy. Root cause was NOT a cold start — Render
-- logs showed:
--
--   ERROR:webapp_backend.supabase:supabase GET v_talaria_daily_close
--   failed HTTP 500: {"code":"57014",...,"message":"canceling statement
--   due to statement timeout"}
--
-- v_talaria_daily_close (nobletradingapp migration 0011) is:
--
--   SELECT DISTINCT ON (b.symbol, b.session_date)
--     b.symbol, b.session_date, b.close_price, b.ts
--   FROM public.nt_renko_bricks b
--   ORDER BY b.symbol, b.session_date, b.brick_index DESC, b.ts DESC;
--
-- The only existing index anywhere near this shape is
-- idx_nt_renko_bricks_live_grid (migration 185):
--   (symbol, brick_size, session_date DESC, brick_index DESC)
-- — brick_size sits between symbol and session_date, so it does NOT match
-- this view's (symbol, session_date, brick_index DESC, ts DESC) ordering.
-- Without a matching index, Postgres must sort per DISTINCT ON group across
-- the whole table for every symbol in the IN-list, which routinely blew the
-- Supabase-enforced statement_timeout as nt_renko_bricks grew.
--
-- This index is purpose-built for v_talaria_daily_close's exact DISTINCT ON
-- + ORDER BY — `services/supabase_client.py::fetch_daily_closes` (webapp
-- backend, correlation-overview) is the only caller of that view.
--
-- Plain CREATE INDEX (not CONCURRENTLY): Supabase's SQL Management API runs
-- statements inside an implicit transaction, where CONCURRENTLY is
-- rejected (see migration 177's note). Takes a brief lock but does not
-- rewrite the table.

CREATE INDEX IF NOT EXISTS idx_nt_renko_bricks_daily_close
  ON public.nt_renko_bricks (symbol, session_date, brick_index DESC, ts DESC);

COMMENT ON INDEX public.idx_nt_renko_bricks_daily_close IS
  'Supports v_talaria_daily_close''s DISTINCT ON (symbol, session_date) '
  'ORDER BY symbol, session_date, brick_index DESC, ts DESC — used by '
  'webapp-backend fetch_daily_closes() for the Portfolio tab correlation '
  'panel. Without this, Postgres statement_timeout (57014) on the view.';

-- POST-RUN VERIFICATION:
--   EXPLAIN (ANALYZE, BUFFERS)
--   SELECT DISTINCT ON (symbol, session_date)
--     symbol, session_date, close_price, ts
--   FROM nt_renko_bricks
--   WHERE symbol IN ('AAPL','EURUSD','XAUUSD','BTCUSD')
--   ORDER BY symbol, session_date, brick_index DESC, ts DESC;
--   -- expect Index Scan (or Index Only Scan) using
--   -- idx_nt_renko_bricks_daily_close, no Sort node feeding the DISTINCT ON.
