-- ═══════════════════════════════════════════════════════════════════════════
-- 188: One-time backlog prune — nt_sweep_result + nt_regime_log
--
-- Root cause (2026-10-01, worklog
-- 20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §7/§10/§11): a Supabase
-- table/index size audit found nt_sweep_result at 103.41 MB (17.09% of the
-- whole database) and nt_regime_log at 79.47 MB (13.13%) — both append-only,
-- written on every sweep tick (~174K/~178K inserts per migration 177's
-- 2026-09-27 note), with NO retention pass of any kind ever built. Compare
-- nt_signal_sim, which got a dedicated nightly retention module
-- (eod_retention.py) back on 2026-08-12 when it hit this same
-- unbounded-growth problem first — these two tables never got the
-- equivalent treatment.
--
-- A full-repo audit (same worklog §10) checked every replay/research script
-- plus every live app query path against both tables and confirmed: nothing
-- anywhere reads more than ~7-30 days back (the one 7-day consumer is
-- noble_calibration.py §5's failed-heavy-sweep audit; the two deeper pulls
-- against nt_sweep_result, kairos_p12_timesfm_ic.py and kairos_p3_rlayer.py,
-- are fixed, already-executed one-time dated studies, not recurring
-- dependencies). Unlike nt_renko_bricks (whose retention is in genuine
-- tension with correlation's need for 120 days of resampled daily closes,
-- same worklog §4c), there is no such conflict here.
--
-- Neither table is referenced by another table's rows the way nt_signal_sim
-- is protected from deletion by open nt_paper_positions.signal_id references
-- (eod_retention.py's _fetch_protected_signal_ids) — a plain age-cutoff
-- DELETE is safe with no protect-list step.
--
-- Fix (mirrors migration 145's own precedent for nt_renko_bricks — a
-- one-time backlog DELETE using the SAME cutoff the recurring policy uses
-- going forward):
--   One-time prune: delete everything older than 30 days (matching
--   feature_flags.yaml's new sweep_result_retention_days /
--   regime_log_retention_days defaults). The recurring policy
--   (regime_platform/sweep_orchestrator/sweep_result_retention.py, run
--   nightly from orchestrator.py's eod-lookback cycle, Part VII)
--   additionally prunes going forward on the same cutoff, read from
--   config/feature_flags.yaml (not hardcoded in SQL) so the policy can be
--   tuned without a new migration — same eod_retention.py /
--   renko_retention.py precedent already established in this codebase.
--
-- Plain DELETE (not CONCURRENTLY — N/A for DELETE anyway; consistent with
-- migration 177/187's noted constraint that Supabase's SQL Management API
-- runs inside an implicit transaction).
--
-- A full point-in-time backup of both tables (plus nt_renko_bricks) was
-- taken before this migration ran — scripts/backup_retention_tables.py,
-- output at backups/20261001_pre_retention_cleanup/ (182,003 nt_sweep_result
-- rows, 261,548 nt_regime_log rows, gzip-compressed JSON-lines, copied off
-- the Studio to local + repo-adjacent storage).
-- ═══════════════════════════════════════════════════════════════════════════

DELETE FROM nt_sweep_result
WHERE sweep_timestamp < (now() - INTERVAL '30 days');

DELETE FROM nt_regime_log
WHERE detected_at < (now() - INTERVAL '30 days');

-- Verify (expect both at/near 0 extra rows beyond the 30-day window):
--   SELECT count(*) FROM nt_sweep_result WHERE sweep_timestamp < (now() - INTERVAL '30 days');
--   SELECT count(*) FROM nt_regime_log WHERE detected_at < (now() - INTERVAL '30 days');
