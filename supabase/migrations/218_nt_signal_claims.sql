-- 218_nt_signal_claims.sql
-- Claims layer, Phase A prediction ledger (worklog/
-- 20261027_KAIROS_CLAIMS_LAYER_DESIGN.md §3, §4 Phase A).
--
-- One row per registered claim, keyed to nt_signal_sim.signal_id (the
-- client-generated UUID the capture path now mints BEFORE both inserts, so
-- the claim and its signal row share the id without a FK — this schema
-- family uses no FKs by convention).
--
-- Population (Phase A): bracket claims are MANDATORY for every QUALIFIED
-- signal — the same population the paper book opens and the broadcast
-- publishes (the acted-on book). vol_fan claims register only when a real
-- multi-horizon quantile-fan source was available at that timestamp; today
-- none is (TimesFM live is a horizon-1 q10/q90 3-point stamp; vol_backtest
-- arms are research-only), so Phase A writes bracket claims whose fan is the
-- house ATR normal-approximation (source = 'atr_normal') — an honest
-- provenance label, one value beyond the design doc's three, added for the
-- same reason the doc itself demands fan provenance: claims are evidence.
--
-- The fan is PRICE space at the boundary (log-space stays inside the fan
-- construction; converted once at registration — the design §1 units
-- contract), on the 7 lab levels q05..q95, hourly steps h = 1..48 where h is
-- the 1-based walk-bar index of the SAME 48-bar window the settlement
-- contract walks. bracket_ref carries the live ENTRY/SL/TP plus their
-- fan-space positions (tp_q / sl_q at the claim's reference horizon) — a
-- read-only stamp; claims never set levels.
--
-- Scoring: the EOD resolver's claims hook resolves the claim through the
-- SAME settlement contract verdict its signal row received (same bars, same
-- basis, same policy — no second resolver) and writes outcome JSONB:
-- {result, exit_price, hold_bars, exit_ts, coverage50/80/90, crps, pinball,
--  brier_resolve, brier_win, n_scored_h}. Claims are immutable once written
-- except status/outcome/resolved_at.
--
-- Shadow-only: claims_mode: "shadow" writes here and NOTHING reads the
-- ledger until Phase B's claims_publish flag flips. MT5/EA consumers are
-- excluded by construction — no broadcast/payload changes in Phase A.
--
-- Apply manually (Supabase SQL editor / management API), like 210–217.

create table if not exists public.nt_signal_claims (
    claim_id        uuid primary key default gen_random_uuid(),
    signal_id       uuid not null,
    symbol          text not null,
    ts              timestamptz not null,
    deadline_ts     timestamptz,
    kind            text not null check (kind in ('bracket','vol_fan','thesis')),
    source          text not null check (source in ('tsfm_sidecar','vol_backtest_recalib','manual','atr_normal')),
    space           text not null default 'price' check (space = 'price'),
    horizon_h       double precision,
    fan             jsonb,
    bracket_ref     jsonb,
    claimed_probs   jsonb,
    status          text not null default 'open' check (status in ('open','resolved','void')),
    outcome         jsonb,
    resolved_at     timestamptz,
    created_at      timestamptz not null default now(),
    unique (signal_id, kind)
);

create index if not exists nt_signal_claims_symbol_ts_idx
    on public.nt_signal_claims (symbol, ts desc);

create index if not exists nt_signal_claims_open_idx
    on public.nt_signal_claims (status)
    where status = 'open';

alter table public.nt_signal_claims enable row level security;

comment on table public.nt_signal_claims is
    'Claims ledger: pre-registered falsifiable claims (bracket / vol_fan / '
    'thesis) resolved by the SAME settlement contract as their signal. '
    'Phase A shadow writes only (claims_mode: shadow); nothing reads it '
    'until Phase B claims_publish.';

comment on column public.nt_signal_claims.signal_id is
    'Client-generated UUID PK of the nt_signal_sim row this claim pre-registers (no FK by family convention; joined offline).';
comment on column public.nt_signal_claims.deadline_ts is
    'ts + expiry window (48h fixed in Phase A). Registered artifact, frozen at claim time.';
comment on column public.nt_signal_claims.source is
    'Fan provenance, frozen at registration: tsfm_sidecar | vol_backtest_recalib | manual | atr_normal (house ATR normal-approx — Phase A bracket fans).';
comment on column public.nt_signal_claims.fan is
    '[{h, q05, q10, q25, q50, q75, q90, q95}] PRICE space, h = 1-based walk-bar of the same 48-bar settlement window.';
comment on column public.nt_signal_claims.bracket_ref is
    '{entry, sl, tp, direction, tp_q, sl_q, h_ref} — the live bracket plus its read-only fan-space positions. Claims never set levels.';
comment on column public.nt_signal_claims.claimed_probs is
    '{p_resolve, p_win_given_resolve, p_win_bracket, pi_win, pi_loss, pi_expire} — probabilities already computed at qualification; carried as evidence, never scored inputs to gates.';
comment on column public.nt_signal_claims.outcome is
    'Filled by the resolver claims hook: {result, exit_price, hold_bars, exit_ts, coverage50, coverage80, coverage90, crps, pinball, brier_resolve, brier_win, n_scored_h} or {result: void, reason}.';

notify pgrst, 'reload schema';
