-- 208_signal_sim_p_reach.sql
--
-- 2026-10-11 — reachability-conditioned probability shadow stamp.
-- Worklog: worklog/20261011_REACHABILITY_PROB_BUILD.md. Roadmap:
-- calibration_reachability_reframe.
--
-- WHY
-- ---
-- reachability_prob.py computes P_reach = P(reach TP before SL before expiry |
-- symbol, vol_tier, distance_atr_bucket, expiry). The sweep stamps it (and the
-- blend-vs-reach delta) here when p_win_reachability_mode=shadow. Live replay
-- (17 era-clean cells): the direction-only blend OVERPRICES vs P_reach on 15/17
-- cells, mean delta +0.195 — reproducing the era-clean calibration bias. These
-- columns let the walk-forward validation measure, per signal, whether P_reach
-- (and replacing p_win with it) reduces bias toward 0 and improves expectancy.
--
-- ADDITIVE + NULLABLE, no backfill. p_win_reachability_mode ships 'off'; rows
-- stamped before any flip have NULL. MEASUREMENT ONLY — EV/Kelly still use the
-- existing blend until the gated live flip.

ALTER TABLE nt_signal_sim
    ADD COLUMN IF NOT EXISTS p_reach DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p_reach_delta DOUBLE PRECISION;

COMMENT ON COLUMN nt_signal_sim.p_reach IS
    'Reachability-conditioned probability P(reach TP before SL before expiry | '
    'symbol, vol_tier, distance_atr_bucket, expiry) from reachability_prob.py '
    '(R-build shadow). Measurement-only; EV/Kelly unchanged. NULL when '
    'p_win_reachability_mode=off, no cell match, or captured before 2026-10-11.';
COMMENT ON COLUMN nt_signal_sim.p_reach_delta IS
    'blend p_win - p_reach. Positive = the direction-only blend OVERPRICES vs '
    'reachability (the overconfidence the reframe removes). R-build shadow.';

-- Partial index for the walk-forward scan (group by delta sign over resolved
-- rows). Only stamped rows are queried.
CREATE INDEX IF NOT EXISTS idx_signal_sim_p_reach
    ON nt_signal_sim (p_reach, ts DESC)
    WHERE p_reach IS NOT NULL;

-- PostgREST must see the columns immediately (stale schema cache -> PGRST204).
NOTIFY pgrst, 'reload schema';
