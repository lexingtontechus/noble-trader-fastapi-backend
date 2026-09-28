-- 183_talaria_latest_signals_rank.sql
-- RANK board (scope 20260928_RANK_BOARD_BROADCAST_DISCORD): expose Kairos
-- rank / event-window from the latest nt_signal_sim row so the dashboard
-- can sort by RANK without a separate query.

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
  l.ta_rsi,
  sim.kairos_rank_score,
  sim.kairos_rank_gate_shadow
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
) m ON true
LEFT JOIN LATERAL (
  -- Most recent light-sweep sim row with a rank stamp (may lag the sweep row).
  SELECT ns.kairos_rank_score, ns.kairos_rank_gate_shadow
  FROM public.nt_signal_sim ns
  WHERE ns.symbol = sym.symbol
    AND ns.kairos_rank_score IS NOT NULL
  ORDER BY ns.ts DESC
  LIMIT 1
) sim ON true;

CREATE UNIQUE INDEX IF NOT EXISTS uq_talaria_latest_signals_symbol
  ON public.v_talaria_latest_signals (symbol);

CREATE VIEW public.v_talaria_kelly_by_symbol AS
SELECT
  symbol, sweep_timestamp, signal, qualified, effective_kelly, kelly_f, regime,
  aggression, ev, p_win, markov_p_up, entry_price, stop_loss, take_profit,
  brick_size, sl_bricks, tp_bricks, kairos_rank_score, kairos_rank_gate_shadow
FROM public.v_talaria_latest_signals;

GRANT SELECT ON public.v_talaria_latest_signals TO anon, service_role;
GRANT SELECT ON public.v_talaria_kelly_by_symbol TO anon, service_role;

COMMENT ON MATERIALIZED VIEW public.v_talaria_latest_signals IS
  'Latest sweep result per active symbol + Kairos rank stamp (183). '
  'Refreshed by refresh_talaria_dashboard_cache() via pg_cron.';

REFRESH MATERIALIZED VIEW public.v_talaria_latest_signals;
