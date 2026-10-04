-- 2026-10-03: Deprecate old p_win_dir (directional logit blend = p_win_raw)
-- and redefine p_win_dir as the bracket probability (renamed from p_win_bracket).
--
-- The old p_win_dir was stamped as `_p_win_dir = p_win_raw` in orchestrator.py:4834
-- and heavy_sweep.py:1790 — identical on 18,064/18,064 rows. The capture stamped
-- two column names for one quantity.
--
-- The new p_win_dir holds the bracket probability (p_resolve * p_win|resolve),
-- which was previously stored in p_win_bracket. p_win_bracket is kept as a
-- duplicate column for backward compatibility with existing views/queries.
--
-- p_win_in_ev_mode flipped from "shadow" to "live" — EV/Kelly now price
-- the settlement event using p_win_dir (bracket probability).

comment on column nt_signal_sim.p_win_dir is
  'Bracket-outcome probability: p_resolve * p_win|resolve (renamed from p_win_bracket, 2026-10-03). NULL when bucket unscored. Old meaning (directional logit blend = p_win_raw) is deprecated.';

-- p_win_bracket is now a deprecated alias for p_win_dir. Keep the column
-- for backward compatibility with existing views (v_p_win_bracket_live_expectancy,
-- v_calibration_confidence_board, v_aligned_baseline_and_evidence_floor).
comment on column nt_signal_sim.p_win_bracket is
  'DEPRECATED (2026-10-03): use p_win_dir instead. Kept for backward compatibility with existing views.';

-- Update the index to cover p_win_dir as well
create index if not exists idx_signal_sim_p_win_dir
  on nt_signal_sim (symbol, ts desc)
  where p_win_dir is not null;
