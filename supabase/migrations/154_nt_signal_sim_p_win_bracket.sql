-- 2026-09-16 Batch C — p_win_bracket capture (worklog
-- 20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md Phase 3).
--
-- Stores BOTH probability concepts on every non-neutral signal:
--   p_win_dir      — current directional logit blend (what p_win means today)
--   p_win_bracket  — P(TP before SL within deadline) = p_resolve * p_win|resolve
--   p_resolve      — P(resolved within 48h | symbol, vol_tier, bucket)
--   p_win_resolve  — Beta-shrunk P(win | resolved)
--   bracket_*      — geometry key used for the lookup
--   bracket_gate_shadow — true if live gates would qualify but bracket gate would not
--
-- Additive columns only.

alter table nt_signal_sim
  add column if not exists p_win_dir double precision,
  add column if not exists p_win_bracket double precision,
  add column if not exists p_resolve double precision,
  add column if not exists p_win_resolve double precision,
  add column if not exists bracket_bucket text,
  add column if not exists bracket_vol_tier text,
  add column if not exists bracket_gate_shadow boolean;

comment on column nt_signal_sim.p_win_dir is
  'Directional confidence at capture (logit blend). NOT P(TP before SL).';
comment on column nt_signal_sim.p_win_bracket is
  'Bracket-outcome probability: p_resolve * p_win|resolve (Batch C). NULL when bucket unscored.';
comment on column nt_signal_sim.p_resolve is
  'P(TP or SL resolved under settlement within deadline | bucket).';
comment on column nt_signal_sim.p_win_resolve is
  'Beta-shrunk P(tp_hit | resolved) for the bucket.';
comment on column nt_signal_sim.bracket_gate_shadow is
  'Shadow: true when live gates pass but p_win_bracket gate would reject.';

create index if not exists idx_signal_sim_p_win_bracket
  on nt_signal_sim (symbol, ts desc)
  where p_win_bracket is not null;
