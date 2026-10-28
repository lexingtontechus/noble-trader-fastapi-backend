-- 220_claims_vol_fan.sql
-- Claims layer, Phase D (worklog/20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §4 +
-- the Phase D build worklog): the recalibrated vol fan.
--
-- Two additive pieces, zero behavior change until the operator flips
-- claims_vol_fan_mode (default off — the design doc's "research-only, as
-- today"):
--
-- 1. nt_signal_claims gains two nullable provenance columns:
--      arm         text  — which GARCH-family arm built the fan
--                         (garch|gjr|egarch; the flag IS the pre-registration,
--                         the row makes switches auditable evidence)
--      calib_level text  — evidence ladder of the terminal-horizon cell
--                         (cell|pooled_symbol_arm|global_arm|none; 'none' =
--                         identity calibration, honestly stamped)
--    NULL for every existing bracket row — additive only, no backfill, no
--    CHECK (the flag family + the writer module constrain the values).
--
-- 2. v_talaria_latest_signals rebuilt (the 183/219 DROP-CASCADE precedent)
--    with the claims LATERAL gaining c.arm/c.calib_level AND a kind
--    priority tiebreak: ORDER BY c.ts DESC, (c.kind = 'vol_fan') DESC.
--    Why the tiebreak: a signal's bracket and vol_fan claims share the same
--    ts (both stamp sim_payload ts), so the 219 ordering was a
--    nondeterministic tie once vol_fan claims exist. Phase D's semantics:
--    latest claim per symbol, and WITHIN a signal the vol_fan fan is the
--    display fan (the honest distribution the design §2 end-state draws
--    ENTRY/SL/TP on) — the bracket claim stays in the ledger as the
--    atr_normal control arm it was registered as. While
--    claims_vol_fan_mode=off no vol_fan rows exist and the view is
--    byte-equivalent to 219. A vol_fan claim only exists when its ledger
--    write SUCCEEDED (fail-open registration), so a failed vol fan degrades
--    the view to the bracket claim automatically.
--
-- The bundle rides unchanged: to_jsonb(s) sweeps the matview, pg_cron 924
-- keeps refreshing every 5 min, /api/dashboard-data + talaria_dashboard_cache
-- pick the new fields up with ZERO function changes. MT5/EA/talaria-copy
-- untouched (SIGNAL_COLUMNS allow-list does not select claim fields).
-- v_talaria_kelly_by_symbol rebuilt byte-identically (the DROP ... CASCADE
-- kills it — 183 precedent) and does NOT gain the claim columns.
--
-- Requires 218 + 219 — both applied by the operator (2026-10-28).
-- Apply manually (Supabase SQL editor / management API), like 210–219.

ALTER TABLE public.nt_signal_claims
    ADD COLUMN IF NOT EXISTS arm text;
ALTER TABLE public.nt_signal_claims
    ADD COLUMN IF NOT EXISTS calib_level text;

COMMENT ON COLUMN public.nt_signal_claims.arm IS
  'Phase D (220): GARCH-family arm that built this vol_fan fan (garch|gjr|egarch); '
  'NULL on bracket rows. The claims_vol_fan_arm flag is the pre-registration; '
  'this column makes arm switches auditable evidence.';
COMMENT ON COLUMN public.nt_signal_claims.calib_level IS
  'Phase D (220): recalib evidence ladder of the terminal-horizon cell '
  '(cell|pooled_symbol_arm|global_arm|none). none = identity calibration — '
  'the fan shipped unrecalibrated, honestly stamped.';

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
  -- object (Phase B transport #2) plus `outcome` (filled by the resolver
  -- claims hook), so the frontend sees ONE claim shape from both transport
  -- paths. The (kind = 'vol_fan') DESC tiebreak (220): within one signal's
  -- same-ts claims the vol_fan fan is the display fan; across signals the
  -- latest ts wins regardless of kind.
  SELECT
    c.signal_id, c.symbol, c.ts, c.deadline_ts, c.kind, c.source, c.space,
    c.horizon_h, c.fan, c.bracket_ref, c.claimed_probs, c.status, c.outcome,
    c.arm, c.calib_level
  FROM public.nt_signal_claims c
  WHERE c.symbol = sym.symbol
  ORDER BY c.ts DESC, (c.kind = 'vol_fan') DESC
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
  'claims-row claim JSONB (219: frozen fan/bracket positions/outcome; 220: '
  'arm/calib_level provenance + vol_fan kind priority on the same-ts tie; '
  'may lag the sweep row like the rank stamp). Refreshed by '
  'refresh_talaria_dashboard_cache() via pg_cron.';

REFRESH MATERIALIZED VIEW public.v_talaria_latest_signals;

notify pgrst, 'reload schema';
