-- 179_fix_latest_signals_matview_scan.sql  (2026-09-27)
--
-- Fixes the query-plan root cause behind refresh_talaria_dashboard_cache()
-- timing out even with cron collisions removed (175/178) and the schema
-- untouched otherwise (176/177). Part of the same harmonization pass.
--
-- ROOT CAUSE: public.v_talaria_latest_signals (materialized view, defined
-- in nobletradingapp/supabase/migrations/0022_materialize_latest_signals.sql)
-- uses `SELECT DISTINCT ON (symbol) ... ORDER BY symbol, sweep_timestamp DESC`
-- over nt_sweep_result (177,870 live rows) and a second DISTINCT ON over
-- nt_regime_log for markov_p_up/dn. Despite a supporting
-- (symbol, sweep_timestamp DESC) index on both tables, Postgres cannot turn
-- DISTINCT ON into an index-skip-scan — it performs one full ordered scan
-- of the ENTIRE table to peel off the first row per symbol.
--
-- Confirmed live via EXPLAIN (ANALYZE, BUFFERS):
--   DISTINCT ON plan:  Index Only Scan using idx_nt_sweep_result_symbol_ts
--                      rows=177870 loops=1, 17,076 heap fetches, 572ms
--   LATERAL plan:      Nested Loop over 14 active symbols, one index probe
--                      each (LIMIT 1 per symbol) — rows=14 loops=1, 13ms
-- ~44x faster, and the row count this scales with is the SYMBOL COUNT
-- (14-39), not the growing history table (177K+ rows and rising forever).
-- The DISTINCT ON cost was silently getting worse every single sweep tick
-- as nt_sweep_result grew, while contributing to the refresh function's
-- statement-timeout failures logged in cron.job_run_details for job 924.
--
-- FIX: rewrite the matview to drive from the small, bounded active-symbol
-- set (public.v_talaria_symbols, ~14-39 rows) and LATERAL-probe each
-- underlying history table for its single latest row per symbol — same
-- output contract (identical columns/order), same unique index, same
-- grants. `v_talaria_kelly_by_symbol` (a thin wrapper view) is untouched;
-- it selects FROM this matview and needs no changes.

DROP MATERIALIZED VIEW IF EXISTS public.v_talaria_latest_signals CASCADE;

CREATE MATERIALIZED VIEW public.v_talaria_latest_signals AS
SELECT
  sym.symbol,
  l.sweep_timestamp,
  l.regime,
  l.id,
  l.regime_conf,
  m.markov_p_up,
  m.markov_p_dn,
  l.p_win,
  l.ev,
  l.p_timesfm,
  l.kelly_f,
  l.effective_kelly,
  l.brick_size,
  l.sl_bricks,
  l.tp_bricks,
  l.signal,
  l.entry_price,
  l.stop_loss,
  l.take_profit,
  l.qualified,
  l.aggression,
  l.regime_shift,
  l.prev_regime,
  l.size_mult,
  l.ta_adx,
  l.ta_ao,
  l.ta_bb_power,
  l.ta_macd,
  l.ta_rsi
FROM public.v_talaria_symbols sym
CROSS JOIN LATERAL (
  SELECT
    s.sweep_timestamp, s.regime, s.id, s.regime_conf, s.p_win, s.ev,
    s.p_timesfm, s.kelly_f, s.effective_kelly, s.brick_size, s.sl_bricks,
    s.tp_bricks, s.signal, s.entry_price, s.stop_loss, s.take_profit,
    s.qualified, s.aggression, s.regime_shift, s.prev_regime, s.size_mult,
    s.ta_adx, s.ta_ao, s.ta_bb_power, s.ta_macd, s.ta_rsi
  FROM public.nt_sweep_result s
  WHERE s.symbol = sym.symbol
  ORDER BY s.sweep_timestamp DESC
  LIMIT 1
) l
LEFT JOIN LATERAL (
  SELECT rl.markov_p_up, rl.markov_p_dn
  FROM public.nt_regime_log rl
  WHERE rl.symbol = sym.symbol
  ORDER BY rl.detected_at DESC
  LIMIT 1
) m ON true;

CREATE UNIQUE INDEX IF NOT EXISTS uq_talaria_latest_signals_symbol
  ON public.v_talaria_latest_signals (symbol);

CREATE VIEW public.v_talaria_kelly_by_symbol AS
SELECT
  symbol, sweep_timestamp, signal, qualified, effective_kelly, kelly_f, regime,
  aggression, ev, p_win, markov_p_up, entry_price, stop_loss, take_profit,
  brick_size, sl_bricks, tp_bricks
FROM public.v_talaria_latest_signals;

GRANT SELECT ON public.v_talaria_latest_signals TO anon, service_role;
GRANT SELECT ON public.v_talaria_kelly_by_symbol TO anon, service_role;

COMMENT ON MATERIALIZED VIEW public.v_talaria_latest_signals IS
  'Latest sweep result per active symbol, driven by v_talaria_symbols (small, '
  'bounded) with LATERAL index probes into nt_sweep_result/nt_regime_log — '
  'NOT a DISTINCT ON full-table scan (see migration 179 for why). Refreshed '
  'every 5 min by refresh_talaria_dashboard_cache() via pg_cron job 924.';

REFRESH MATERIALIZED VIEW public.v_talaria_latest_signals;

-- POST-RUN VERIFICATION:
--   EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM v_talaria_latest_signals;
--   -- expect a Nested Loop over v_talaria_symbols with per-symbol LIMIT 1
--   -- probes, NOT a Unique/DISTINCT ON scan of nt_sweep_result.
--   SELECT count(*) FROM v_talaria_latest_signals;  -- expect one row per active symbol
--   -- confirm refresh_talaria_dashboard_cache() completes well under
--   -- statement_timeout now: SELECT refresh_talaria_dashboard_cache();
