-- 211_nt_pbm_shadow.sql
-- Posterior Barrier Model, Phase A prediction ledger (worklog/
-- 20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md §5 Phase A).
--
-- One row per EMITTED signal (the same population p_win_bracket stamps
-- cover): the posterior triple, the analytic null, the selection gap
-- (entry-timing KPI, proposal §8), and the one gate — the EV lower bound
-- with its pass flag. Side table by design: nt_signal_sim's whitelist and
-- the resolver's PATCH path stay untouched; the ledger joins offline on
-- (symbol, ts, signal) for the Phase A acceptance replay (log-score
-- posterior vs actual outcome, era-clean).
--
-- outcome is NULL at stamp time and backfilled by the acceptance harness —
-- the resolver does not write here (its job is the cell posterior, 210).

create table if not exists public.nt_pbm_shadow (
    id              uuid primary key default gen_random_uuid(),
    symbol          text not null,
    ts              timestamptz not null,
    signal          text not null check (signal in ('buy','sell')),
    source          text,
    tp_atr          double precision,
    sl_atr          double precision,
    t_bars          integer,
    sigma           double precision,
    p_w             double precision,
    p_l             double precision,
    p_e             double precision,
    p_w_null        double precision,
    gap             double precision,
    n_eff           double precision,
    ev_atr          double precision,
    ev_lb_atr       double precision,
    ev_lb_pass      boolean,
    alpha_w         double precision,
    alpha_l         double precision,
    alpha_e         double precision,
    bracket_bucket  text,
    vol_tier        text,
    deadline_class  text,
    era             text,
    outcome         text check (outcome in ('tp_hit','sl_hit','expired')),
    created_at      timestamptz not null default now()
);

create index if not exists nt_pbm_shadow_symbol_ts_idx
    on public.nt_pbm_shadow (symbol, ts desc);

create index if not exists nt_pbm_shadow_outcome_idx
    on public.nt_pbm_shadow (outcome)
    where outcome is not null;

comment on table public.nt_pbm_shadow is
    'PBM prediction ledger: per-signal posterior vs null vs EV-bound stamp, '
    'shadow-only until the Phase B/C flips earn it.';
