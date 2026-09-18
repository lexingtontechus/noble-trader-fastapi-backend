-- 2026-09-18 tiered ATR — brick write provenance (worklog
-- 20260918_TIERED_ATR_GEOMETRY_SCOPE.md Phase C1).

create table if not exists nt_brick_params_state (
  symbol          text primary key,
  brick_size      double precision,
  sl_bricks       integer,
  tp_bricks       integer,
  atr_at_write    double precision,
  asset_class     text,
  source          text,
  written_at      timestamptz not null default now()
);

comment on table nt_brick_params_state is
  'Last persisted brick_params geometry + ATR at write (vol-drift risk input).';

alter table nt_brick_params_state enable row level security;

drop policy if exists nt_brick_params_state_service on nt_brick_params_state;
create policy nt_brick_params_state_service on nt_brick_params_state
  for all using (true) with check (true);

grant select on nt_brick_params_state to anon, authenticated;
grant insert, update on nt_brick_params_state to anon, authenticated;
