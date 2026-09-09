-- 2026-09-09, worklog 20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md:
-- light sweep (regime_cron) moves from 5 to 15 minutes for all symbols.
--
-- Root cause: TimesFM is fetched with a 15-minute in-memory cache
-- (_TIMESFM_CACHE_TTL_S = 900, orchestrator.py). At a 5-minute sweep
-- cadence, 2 of every 3 ticks silently reuse a stale cached TimesFM value
-- while regime/Kelly/EV are recomputed fresh from the SAME underlying
-- hourly bars (which themselves only roll over once an hour) -- an
-- asynchronous-refresh artifact that manufactures tick-to-tick noise
-- without any real new information having arrived. Confirmed via a real
-- production example (XAUUSD, 2026-09-07): regime label and p_win wobbled
-- across five-minute ticks within a SINGLE underlying regime episode that
-- brick-level price action showed as one unbroken move the whole time.
-- Moving to 15 minutes aligns the sweep tick rate with TimesFM's own
-- refresh rate, removing this specific noise source, and cuts TDVA API
-- call volume and compute load 3x as a side effect.
--
-- Considered and rejected: differentiating cadence by asset class
-- (crypto/forex faster than equities). The bottleneck is the shared
-- hourly-bar fetch cadence every asset class currently uses for regime
-- detection -- sweeping crypto/forex more often would not surface any
-- real new information faster, since the underlying data granularity is
-- the same hourly candle regardless of tick rate. A genuine "crypto/forex
-- move faster" fix would mean upgrading DATA GRANULARITY (5-min/1-min
-- candles) for those asset classes specifically -- a separate, larger,
-- not-yet-scoped effort, not a sweep-cadence change. Confirms the
-- 2026-08 unification (migration 028, replacing an earlier
-- weekday/weekend-split per-symbol cron scheme) was the right call and
-- should stay unified.
--
-- Note: orchestrator.py's own code-level fallback for a NULL regime_cron
-- was ALREADY "*/15 * * * *" (orchestrator.py:1196,3450,3456) -- this
-- migration brings the actual live data in line with what the code
-- itself already assumed was the sane default.

UPDATE nt_symbol
SET regime_cron = '*/15 * * * *'
WHERE active = true AND regime_cron = '*/5 * * * *';
