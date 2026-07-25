-- ═══════════════════════════════════════════════════════════════════════════════
-- 034: Deprecate nt_pattern_stats table
--
-- Background
-- ─────────
-- nt_pattern_stats was created in the v3 era as the server-side store for
-- per-pattern historical win rates. The v4 EV engine queried this table
-- via EVEngine.validate_signal() to look up P_win by (regime, pattern).
--
-- In v5, this is no longer the correct architecture:
--
--   • The server has no trade journal — all execution + journaling lives
--     in noble-trader-agent's DuckDB (migration 011_pattern_learning.sql
--     creates the agent-side pattern_performance table).
--   • The agent already has the right table: pattern_performance. It is
--     populated by the agent's own trade outcomes, not by a server-side
--     sync job.
--   • The v5 EV engine receives p_pattern via the orchestrator's
--     agent_payload (orchestrator.py:1434), not via a DB lookup.
--   • EVEngine.sync_journal_to_stats() — the only writer — was deleted
--     in v5. The /ev/sync-journal and /strategy/sync-journal endpoints
--     that called it were also removed.
--
-- As a result, nt_pattern_stats is now always empty. No production code
-- reads from it. This migration:
--
--   1. Adds a deprecation COMMENT on the table.
--   2. Does NOT drop the table — keep it for 30 days in case any
--      external tooling (dashboards, BI) reads from it. After 30 days
--      of confirmed zero reads, run 035_drop_nt_pattern_stats.sql.
--   3. Drops the indexes (safe — table is empty).
--
-- Post-v5 architecture (for reference):
--   Server:   nt_symbol, nt_sweep_result, nt_regime_log, nt_calibration_records
--   Agent:    pattern_performance (DuckDB), pnl_realized (DuckDB),
--             signal_heartbeats (DuckDB)
--   Flow:     Agent pattern_performance → p_pattern in agent_payload
--             → EV engine's 4-source logit pool
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─── 1. Deprecation comment ─────────────────────────────────────────────────
COMMENT ON TABLE nt_pattern_stats IS
  'DEPRECATED in v5 (2026-07-22). No production code reads or writes this '
  'table. Pattern stats now live in the agent-side DuckDB '
  '(pattern_performance table, migration 011_pattern_learning.sql in '
  'noble-trader-agent). The v5 EV engine receives p_pattern via the '
  'orchestrator agent_payload, not via a DB lookup. '
  'Safe to drop after 30 days of confirmed zero reads — see migration '
  '035_drop_nt_pattern_stats.sql.';

-- ─── 2. Drop indexes (safe — table is empty) ───────────────────────────────
-- Keeps the table itself for backward compat with any external dashboards.
DROP INDEX IF EXISTS idx_nt_pattern_stats_regime_pattern;
DROP INDEX IF EXISTS idx_nt_pattern_stats_symbol;

-- ─── 3. Verify ─────────────────────────────────────────────────────────────
SELECT
  obj_description('nt_pattern_stats'::regclass, 'pg_class') AS table_comment;

-- Show that no code writes to it (row count should be 0)
SELECT COUNT(*) AS row_count FROM nt_pattern_stats;
