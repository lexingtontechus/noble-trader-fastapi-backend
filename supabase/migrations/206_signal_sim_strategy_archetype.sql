-- 206_signal_sim_strategy_archetype.sql
--
-- 2026-10-10 — persist the strategy-router archetype (R1, shadow).
-- Worklog/scope: docs/scopes/20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md §4.
--
-- WHY
-- ---
-- R1 (regime_platform/sweep_orchestrator/strategy_router.py) resolves each
-- signal to one strategy archetype from config/strategy_registry.yaml
-- (momentum / mean_reversion / stand_by / ...) and the sweep stamps it here.
-- The whole point is measurability: R2 scores whether the stamped archetype
-- predicts outcome better than the current uniform engine — which is
-- impossible until the archetype is actually persisted. The mean_reversion
-- archetype encodes the ID45 gold-buy counter-trend pattern (era-clean:
-- XAUUSD buy false-cohort 38.2% WR vs 6.5% pass-cohort).
--
-- ADDITIVE + NULLABLE: rows captured before this migration have NULL, which
-- reads as "no archetype stamped" (router.mode was off / predates R1). No
-- backfill — the router did not run on historical rows, and guessing would
-- fabricate a routing that never happened. R1 is SHADOW: this column changes
-- no live geometry, deadline, or calibration; it is a measurement stamp only.

ALTER TABLE nt_signal_sim
    ADD COLUMN IF NOT EXISTS strategy_archetype TEXT;

COMMENT ON COLUMN nt_signal_sim.strategy_archetype IS
    'Strategy-router archetype resolved by strategy_router.py (R1 shadow): '
    'momentum | mean_reversion | stand_by | (ma_trend/fx_carry/range_fade/'
    'breakout when enabled). Measurement-only for R2 — no live effect. NULL '
    'when router.mode=off or the router errored, and for rows captured before '
    '2026-10-10. Scope 20261010_KAIROS_STRATEGY_STRUCTURE_WIDER_LENS.md.';

-- Index for the R2 per-archetype outcome scan (group by archetype over
-- resolved rows). Partial on non-null so it stays small.
CREATE INDEX IF NOT EXISTS idx_signal_sim_strategy_archetype
    ON nt_signal_sim (strategy_archetype, ts DESC)
    WHERE strategy_archetype IS NOT NULL;

-- PostgREST must see the column immediately (stale schema cache -> PGRST204).
NOTIFY pgrst, 'reload schema';
