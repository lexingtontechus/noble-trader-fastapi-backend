-- 219_extend_latest_signals_claims.sql
-- Claims layer, Phase B transport #1 (worklog/
-- 20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §3 "Frontend contract additions",
-- §4 Phase B + 20261028_KAIROS_CLAIMS_PHASE_BC_BUILD.md):
-- v_talaria_latest_signals gains the LATEST registered claim per symbol as
-- a `claim` JSONB column — the 183 LATERAL pattern applied a fourth time.
--
-- Because the dashboard bundle's `sweeps` key is to_jsonb(s) over this
-- matview (refresh_talaria_dashboard_cache, migration 193), the claim rides
-- /api/dashboard-data and talaria_dashboard_cache with ZERO function
-- changes: pg_cron job 924 keeps refreshing this matview every 5 min
-- unchanged, and the frontend route's select("*") picks the column up
-- automatically.
--
-- Semantics: LATEST claim per symbol (any status). An open claim shows the
-- frozen fan + bracket-in-fan positions; a resolved claim additionally
-- carries outcome JSONB (coverage/CRPS/Brier) so the signal card can show
-- the score. May lag the sweep row exactly like the 183 rank stamp (a
-- neutral sweep emits no claim; the LATERAL is keyed on symbol, not
-- signal_id) — the claim's own ts/signal_id are exposed so the frontend
-- can display staleness honestly. NULL when the symbol has no claims yet
-- (LEFT JOIN LATERAL ... ON true; to_jsonb of the NULL row is NULL).
--
-- Requires 218 (nt_signal_claims) — applied by the operator 2026-10-28.
-- The LATERAL probe is served by nt_signal_claims_symbol_ts_idx (218).
--
-- MT5/EA/talaria-copy consumers are untouched: SIGNAL_COLUMNS is an
-- explicit allow-list that does not select `claim`; the new column is
-- additive. v_talaria_kelly_by_symbol is rebuilt byte-identically (the
-- DROP ... CASCADE kills it — 183 precedent) and does NOT gain the claim
-- column (the Kelly table is a sizing surface, not a claims surface).
--
-- Apply manually (Supabase SQL editor / management API), like 210–218.

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
  sim.kairos_rank_gate_shadow,
  to_jsonb(cl) AS claim
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
) sim ON true
LEFT JOIN LATERAL (
  -- Latest registered claims row per symbol, any status (may lag the sweep
  -- row exactly like the rank stamp — neutral sweeps emit no claim). NULL
  -- when the symbol has no claims yet. Shape matches the broadcast `claim`
  -- object from claims.py (Phase B transport #2) plus `outcome` (filled by
  -- the resolver claims hook), so the frontend sees ONE claim shape from
  -- both transport paths.
  SELECT
    c.signal_id, c.symbol, c.ts, c.deadline_ts, c.kind, c.source, c.space,
    c.horizon_h, c.fan, c.bracket_ref, c.claimed_probs, c.status, c.outcome
  FROM public.nt_signal_claims c
  WHERE c.symbol = sym.symbol
  ORDER BY c.ts DESC
  LIMIT 1
) cl ON true;

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
  'Latest sweep result per active symbol + Kairos rank stamp (183) + latest '
  'claims-row claim JSONB (219: frozen fan/bracket positions/outcome, may '
  'lag the sweep row like the rank stamp). Refreshed by '
  'refresh_talaria_dashboard_cache() via pg_cron.';

REFRESH MATERIALIZED VIEW public.v_talaria_latest_signals;

notify pgrst, 'reload schema';
