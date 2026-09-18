-- 2026-09-18 tiered ATR geometry capture (worklog
-- 20260918_TIERED_ATR_GEOMETRY_SCOPE.md Phase C2).

alter table nt_signal_sim
  add column if not exists tp_distance_atr double precision,
  add column if not exists sl_distance_atr double precision,
  add column if not exists long_lag_hat double precision,
  add column if not exists geometry_risk_hat double precision,
  add column if not exists long_lag_risk boolean;

comment on column nt_signal_sim.tp_distance_atr is
  'Take-profit distance / ATR at capture (geometry).';
comment on column nt_signal_sim.sl_distance_atr is
  'Stop-loss distance / ATR at capture.';
comment on column nt_signal_sim.long_lag_hat is
  'Estimated P(expired / open past 48h) for this geometry.';
comment on column nt_signal_sim.geometry_risk_hat is
  'Long-lag risk score (pct_expired × tail × vol-drift).';
comment on column nt_signal_sim.long_lag_risk is
  'True when geometry_risk_hat >= CAP (diagnostic).';

create index if not exists idx_signal_sim_geometry_risk
  on nt_signal_sim (symbol, ts desc)
  where long_lag_risk = true;
