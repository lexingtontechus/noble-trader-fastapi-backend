-- 204_signal_sim_ambiguous.sql
--
-- 2026-10-08 — persist the same-bar ambiguity flag.
-- Worklog: worklog/20261008_GEOMETRY_BASIS_DIVERGENCE.md (Stage 3).
--
-- WHY
-- ---
-- `resolve_signal_outcome_ohlc` already computes `ambiguous` — True when a
-- single 1h bar first-touches BOTH the stop and the target, so the order is
-- unknowable from OHLC and the same-bar policy decides the outcome. It then
-- DISCARDS the flag: there was no column to write it to.
--
-- Measured on GBPUSD sell at the live bracket (TP 1.61 / SL 0.34 ATR):
-- **21.4% of episodes** have a single bar spanning both levels. That is the
-- single most decision-relevant fact about a tight-stop bracket and it was
-- invisible. Persisting it makes the ambiguity measurable per bucket, so a
-- geometry pick can be judged on how often its own bracket is undecidable.
--
-- ADDITIVE + NULLABLE: rows resolved before this migration have NULL, which
-- reads as "unknown" (the resolver predates the flag). No backfill — the
-- historical resolver did not record it, and guessing would fabricate data.

ALTER TABLE nt_signal_sim
    ADD COLUMN IF NOT EXISTS ambiguous BOOLEAN;

COMMENT ON COLUMN nt_signal_sim.ambiguous IS
    'True when a single bar first-touched both TP and SL (same-bar tie; '
    'outcome chosen by settlement_same_bar_policy). NULL for rows resolved '
    'before 2026-10-08. Worklog 20261008_GEOMETRY_BASIS_DIVERGENCE.md.';

-- Partial index: only the ambiguous rows are ever queried (the population is
-- small — ~20% of resolved rows at a tight-stop bracket).
CREATE INDEX IF NOT EXISTS idx_signal_sim_ambiguous
    ON nt_signal_sim (symbol, ts DESC)
    WHERE ambiguous IS TRUE;

-- PostgREST must see the column immediately (stale schema cache → PGRST204).
NOTIFY pgrst, 'reload schema';
