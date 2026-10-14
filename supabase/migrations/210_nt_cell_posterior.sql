-- 210_nt_cell_posterior.sql
-- Posterior Barrier Model, Layer 2 storage (worklog/
-- 20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md §3, proposal §3).
--
-- ONE table replaces the lookup-plus-bias-plus-calibration-records sprawl
-- for the probability object: every resolved outcome class — tp_hit,
-- sl_hit, AND expired (survivorship closed by construction) — increments a
-- decay-weighted Dirichlet on the single cell key
--   (symbol, side, vol_tier, distance_atr_bucket, deadline_class, era)
-- that the solver, the pricer, and the measurement all read.
--
-- Keying notes:
--   * side            — finally consumes migration 159's buy/sell split;
--                       the live lookup's signal=eq.all pooling retires with
--                       this table's readers.
--   * deadline_class  — 'short' | 'medium' | 'long' | 'standard', from the
--                       signal's dynamic-deadline stamps (48h = standard).
--   * era             — 'live' rows only accumulate outcomes resolved after
--                       the nt_calibration_cutover marker; pre-cutover rows
--                       are skipped at UPDATE time, so the era leak closes
--                       by construction, not by a forgettable read filter.
--
-- Sufficient statistics, not summaries: s_w/s_l/s_e are decayed weighted
-- counts (2^(-age/tau)), so a daily decay rebuild is exact, and alpha_*
-- store k0*pi + s so any reader can rebuild credible intervals without
-- re-deriving the analytic anchor.

create table if not exists public.nt_cell_posterior (
    symbol                  text not null,
    side                    text not null check (side in ('buy','sell')),
    vol_tier                text not null default 'unknown',
    distance_atr_bucket     text not null,
    deadline_class          text not null default 'standard',
    era                     text not null default 'live',
    s_w                     double precision not null default 0,
    s_l                     double precision not null default 0,
    s_e                     double precision not null default 0,
    n_w                     bigint not null default 0,
    n_l                     bigint not null default 0,
    n_e                     bigint not null default 0,
    n_eff                   double precision not null default 0,
    p_w                     double precision,
    p_l                     double precision,
    p_e                     double precision,
    p_w_null                double precision,
    gap                     double precision,
    m_mtm                   double precision,
    alpha_w                 double precision,
    alpha_l                 double precision,
    alpha_e                 double precision,
    updated_at              timestamptz,
    primary key (symbol, side, vol_tier, distance_atr_bucket, deadline_class, era)
);

create index if not exists nt_cell_posterior_symbol_era_idx
    on public.nt_cell_posterior (symbol, era);

comment on table public.nt_cell_posterior is
    'PBM cell posterior: decay-weighted Dirichlet over (W, L, E) per cell — '
    'one probability object for solver, pricer, and measurement.';
