-- 178_stagger_cron_schedules.sql  (2026-09-27)
--
-- Fixes the remaining cron-collision contributor to "job startup timeout"
-- bursts (the third fix in this incident's harmonization pass, after
-- 175 = dashboard-cache overlap guard + pagination index,
-- 176 = renko no-op-update guard,
-- 177 = duplicate index removal).
--
-- ROOT CAUSE: all four frequent cron jobs were anchored at minute 0 with no
-- offset, using bare `*/N` schedules:
--   924 refresh_talaria_dashboard_cache   */5   -> fires on every multiple of 5
--   886 Heartbeat Lightningai HMM-TimesFM */10  -> fires on every multiple of 10 (subset of 5)
--   917 helio-subscription-sync           */15  -> fires on every multiple of 15 (subset of 5)
--   920 Health-NobleTradingApp-Backend    */14  -> fires at :00, :14, :28, :42, :56 (hits :00 every hour)
--
-- Every multiple of 10 and 15 is ALSO a multiple of 5, so 886/917 collided
-- with 924 on literally every one of their own firings, and all four
-- collided together every hour at :00. On the Micro compute tier
-- (max_worker_processes=6, shared with autovacuum/pg_net/logical
-- replication), a simultaneous burst of 3-4 jobs regularly could not all
-- get a worker slot in time -> "job startup timeout" failures, confirmed
-- live in cron.job_run_details clustering at :00/:14/:15/:20/:30 marks.
--
-- FIX: give each job a distinct minute offset so no two ever fire in the
-- same minute (verified pairwise disjoint below), while preserving each
-- job's original cadence:
--   924 every 5 min,  offset 2  -> :02,:07,:12,...,:57
--   886 every 10 min, offset 0  -> unchanged (:00,:10,...,:50 — already disjoint from the rest)
--   917 every 15 min, offset 3  -> :03,:18,:33,:48
--   920 every ~14 min, offset 11 -> :11,:25,:39,:53 (4/hour instead of 5 — the
--       lost occurrence is an acceptable trade for eliminating the :00
--       collision; health checks do not need sub-15-minute precision)

SELECT cron.alter_job(job_id => 924,
  schedule => '2,7,12,17,22,27,32,37,42,47,52,57 * * * *');

SELECT cron.alter_job(job_id => 917,
  schedule => '3,18,33,48 * * * *');

SELECT cron.alter_job(job_id => 920,
  schedule => '11,25,39,53 * * * *');

-- 886 already sits on a schedule (*/10, i.e. :00/:10/:20/:30/:40/:50) that is
-- disjoint from all three schedules above — left unchanged.

-- POST-RUN VERIFICATION:
--   SELECT jobid, jobname, schedule FROM cron.job WHERE jobid IN (886,917,920,924) ORDER BY jobid;
--   -- confirm no more same-minute collisions over the next hour:
--   SELECT date_trunc('minute', start_time) AS minute, count(*) AS concurrent_runs
--   FROM cron.job_run_details
--   WHERE jobid IN (886,917,920,924) AND start_time > now() - interval '2 hours'
--   GROUP BY minute HAVING count(*) > 1 ORDER BY minute DESC;
