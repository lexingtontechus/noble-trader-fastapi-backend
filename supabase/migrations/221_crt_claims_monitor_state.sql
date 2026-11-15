-- 221_crt_claims_monitor_state.sql  (2026-11-15)
-- CRT-2: claims + monitors for the CRT v5 CONT paper engine (worklog/
-- 20261115_KAIROS_CRT2_BUILD.md; scope pre-registered by the assessment
-- 20261115_CRT_V5_CONT_INTEGRATION_ASSESSMENT.md §6 item CRT-2).
--
-- Three additive pieces, zero existing-table column changes:
--
-- 1. WIDEN the nt_signal_claims CHECK lists so CRT can register
--    kind='crt_option' / source='crt_walkforward' claims — the exact
--    pattern of migrations 116/217 (drop + re-add with the old values
--    retained; purely widening, never narrowing). CRT claims mint at
--    paper-fill time (frozen-at-C2-close monitor state as claimed
--    probabilities) and resolve through CRT's OWN close pass — the
--    single-resolver contract holds: one settlement verdict per position,
--    the labeler's walk (shared with the paper book), never a second
--    resolution rule. The EOD machinery never touches CRT claims: the
--    eod hooks pop nt_signal_sim-keyed claims only, and CRT signal_ids
--    are self-minted UUIDs on nt_paper_positions(source='crt_cont').
--
-- 2. nt_crt_monitor_state — ONE row per (symbol, tf, dir, session) option:
--    the CONFIG-DEPLOY PRE-REGISTRATION (the CRT-0 walk-forward artifact
--    fields verbatim: oos_ev/sd/t/n, train_end, thresholds, rolling-gate
--    config, artifact runid) plus the live monitor state (CUSUM S, live
--    n/EV, rolling n/EV, decay state) refreshed by a throttled flush.
--    The nt_symbol_calibration_state precedent (migration 150): exactly
--    one current-state row per key, upserted through exactly one writer
--    (crt/state_store.py), never insert-and-orphaned.
--
-- 3. nt_crt_rolling_daily — the rolling-EV table: one row per (UTC day,
--    option) snapshot of the 45d rolling stats + the TF pool (the
--    nt_live_expectancy_daily precedent, migration 163 — daily upsert,
--    survives eod_retention, the CRT-3 promotion evidence surface).
--
-- Apply manually (Supabase SQL editor / management API), like 210–220.

-- ── 1. Widen the claims CHECKs (drop + re-add, old values retained) ────────

alter table public.nt_signal_claims
    drop constraint if exists nt_signal_claims_kind_check;
alter table public.nt_signal_claims
    add constraint nt_signal_claims_kind_check
    check (kind in ('bracket','vol_fan','thesis','crt_option'));

alter table public.nt_signal_claims
    drop constraint if exists nt_signal_claims_source_check;
alter table public.nt_signal_claims
    add constraint nt_signal_claims_source_check
    check (source in ('tsfm_sidecar','vol_backtest_recalib','manual','atr_normal','crt_walkforward'));

comment on column public.nt_signal_claims.kind is
    'bracket | vol_fan | thesis | crt_option (CRT v5 CONT paper fills — minted at fill with the frozen C2-close monitor state, resolved by the CRT close pass).';
comment on column public.nt_signal_claims.source is
    'Fan provenance, frozen at registration: tsfm_sidecar | vol_backtest_recalib | manual | atr_normal (house ATR normal-approx — Phase A bracket fans) | crt_walkforward (CRT-0 walk-forward artifact — the optimizer''s own OOS numbers, no house overlay).';

-- ── 2. nt_crt_monitor_state (one row per option; one writer) ────────────────

create table if not exists public.nt_crt_monitor_state (
    symbol          text not null,
    tf              text not null check (tf in ('M15','M30','H1')),
    dir             text not null check (dir in ('BUY','SELL')),
    session         text not null check (session in ('OUT','NY','ASIA')),
    model           text not null check (model in ('STOP','C3C','CONT')),
    -- config-deploy pre-registration (CRT-0 artifact row, verbatim)
    enabled         boolean not null default false,
    eb              double precision,
    sb              double precision,
    r               double precision,
    hold            integer,
    max_sweep       double precision,
    chop            integer,
    vol_mask        integer,
    c3_dir          integer,
    base_ev         double precision,
    base_sd         double precision,
    oos_t           double precision,
    oos_n           integer,
    train_end       timestamptz,
    generated        text,
    artifact_runid  text,
    thresholds      jsonb not null default '{}'::jsonb,
    rolling_gate    jsonb not null default '{}'::jsonb,
    -- live monitor state (throttled flush; the in-memory OptionCfg is truth)
    state           text not null default 'DISABLED'
                      check (state in ('DISABLED','NORMAL','CAUTION','OFF')),
    s_cusum         double precision not null default 0,
    live_n          integer not null default 0,
    live_ev         double precision not null default 0,
    roll_ev         double precision not null default 0,
    rl_n            integer not null default 0,
    rl_ev           double precision not null default 0,
    updated_at      timestamptz not null default now(),
    primary key (symbol, tf, dir, session)
);

comment on table public.nt_crt_monitor_state is
    'CRT-2: THE single current-state row per CRT option (symbol, tf, dir, session) — the config-deploy pre-registration (CRT-0 artifact fields verbatim: the promised OOS EV/SD/t/n, decay thresholds, rolling-gate config) plus the live monitor state (CUSUM S, live n/EV, rolling n/EV, decay state). One writer (crt/state_store.py upsert), throttled flush; the in-memory OptionCfg is the operating truth, this row is the durable pre-registration + audit. Precedent: nt_symbol_calibration_state (migration 150).';

comment on column public.nt_crt_monitor_state.thresholds is
    'The decay (CUSUM) thresholds in force at deploy, from config/crt/crt_engine.yaml decay block — the pre-registered kill-switch settings (kappa_frac/min, h_caution, h_off, roll_n, roll_off_ev, react_n, use_monitor, caution_mult).';
comment on column public.nt_crt_monitor_state.rolling_gate is
    'The rolling-gate config in force at deploy (use_gate, roll_days, roll_min_n, roll_min_ev, scope) — pre-registered alongside the thresholds it will be judged by.';
comment on column public.nt_crt_monitor_state.base_ev is
    'The OOS walk-forward mean R this option was minted on (the CRT-0 artifact row''s base_ev) — the pre-registered claim the live paper evidence is scored against.';

alter table public.nt_crt_monitor_state enable row level security;
drop policy if exists "Service role full access" on public.nt_crt_monitor_state;
create policy "Service role full access" on public.nt_crt_monitor_state
    for all to service_role using (true) with check (true);
drop policy if exists "anon read nt_crt_monitor_state" on public.nt_crt_monitor_state;
create policy "anon read nt_crt_monitor_state" on public.nt_crt_monitor_state
    for select using (true);
grant select on public.nt_crt_monitor_state to anon, authenticated;
grant all on public.nt_crt_monitor_state to service_role;

-- ── 3. nt_crt_rolling_daily (daily rolling-EV snapshot) ─────────────────────

create table if not exists public.nt_crt_rolling_daily (
    day             date not null,
    symbol          text not null,
    tf              text not null check (tf in ('M15','M30','H1')),
    dir             text not null check (dir in ('BUY','SELL')),
    session         text not null check (session in ('OUT','NY','ASIA')),
    enabled         boolean not null default false,
    state           text not null default 'DISABLED'
                      check (state in ('DISABLED','NORMAL','CAUTION','OFF')),
    rl_n            integer not null default 0,
    rl_ev           double precision not null default 0,
    tf_n            integer not null default 0,
    tf_ev           double precision not null default 0,
    open_positions  integer not null default 0,
    computed_at     timestamptz not null default now(),
    primary key (day, symbol, tf, dir, session)
);

comment on table public.nt_crt_rolling_daily is
    'CRT-2: the rolling-EV evidence table — one row per (UTC day, option): the 45d rolling stats (unfiltered paper labels, the EA rlN/rlEV path incl. the README training-overlap honesty note) plus the (symbol, tf) pool columns repeated per option row (tf_n/tf_ev = the ENABLED options'' pooled window; identical by algebra to ComputeRolling''s pool since per-option windows share the horizon filter). Daily upsert; the CRT-3 promotion gate reads the same kernels from resolved claims. Precedent: nt_live_expectancy_daily (migration 163).';

comment on column public.nt_crt_rolling_daily.tf_n is
    'The (symbol, tf) pool n (ENABLED options, same 45d window) — repeated on each option row of that tf for simplicity; the pool is a TF-level fact.';
comment on column public.nt_crt_rolling_daily.rl_ev is
    'Mean R of the option''s resolved paper labels in the rolling window (unfiltered — live health check, NOT validation; overlaps training by the README''s own note).';

alter table public.nt_crt_rolling_daily enable row level security;
drop policy if exists "Service role full access" on public.nt_crt_rolling_daily;
create policy "Service role full access" on public.nt_crt_rolling_daily
    for all to service_role using (true) with check (true);
drop policy if exists "anon read nt_crt_rolling_daily" on public.nt_crt_rolling_daily;
create policy "anon read nt_crt_rolling_daily" on public.nt_crt_rolling_daily
    for select using (true);
grant select on public.nt_crt_rolling_daily to anon, authenticated;
grant all on public.nt_crt_rolling_daily to service_role;

-- POST-RUN VERIFICATION:
--   select kind, count(*) from nt_signal_claims group by kind;
--   select count(*) from nt_crt_monitor_state;      -- one row per option
--   select day, count(*) from nt_crt_rolling_daily group by day order by day desc;

notify pgrst, 'reload schema';
