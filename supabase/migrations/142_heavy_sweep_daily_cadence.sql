-- 2026-09-08, worklog 20260908_PRICING_SCORING_ROOT_CAUSE_AND_SCOPE.md:
-- heavy sweep moves from weekly to daily. The weekly cadence (migrations
-- 022/026/028) was justified by "TimesFM handles non-stationarity 24/7,
-- so bracket re-optimization doesn't need to run often" -- that conflates
-- directional confidence with bracket/TP/SL geometry fit. Real data
-- showed EURUSD/USDCAD/USDJPY sitting at 82-94% expiry even under the
-- corrected (2026-09-08) deadline-aware scorer, i.e. a stale bracket
-- problem persists regardless of TimesFM confidence, and the safety net
-- meant to catch it mid-week (emergency_heavy_sweep, triggered off
-- calibration bias) turned out to rely on a bias signal that massively
-- over-counts evidence (see the same worklog's USDCHF/overlapping-
-- episodes findings). Real measured heavy-sweep cost (~29s avg/symbol,
-- ~93s worst case, nt_sweep_result) makes daily trivial -- the original
-- compute-savings rationale doesn't survive contact with current numbers.
--
-- Same time-of-day (20:00 UTC) kept, just every day instead of only
-- Sunday. See heavy_sweep.py's module docstring for the full reasoning.

UPDATE nt_symbol
SET sweep_cron = '0 20 * * *'
WHERE active = true AND sweep_cron = '0 20 * * sun';
