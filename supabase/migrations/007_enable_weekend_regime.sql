-- Patch 007: Enable weekend regime detection for stocks, forex, commodities
-- ========================================================================
-- PROBLEM: Stock/forex/commodity cron jobs were originally scheduled weekdays
--          only (1-5). Even though patch 006 moved to * * * * *, the edge
--          functions handle weekends correctly — fetchLatestCandles() falls
--          back from hourly → daily (1y/2y) candles when markets are closed,
--          and regime detection works fine with daily data (>=81 bars).
--
--          Skipping weekends means NaN values that appeared on Friday night
--          persist until Monday morning, creating a 60+ hour gap where the
--          dashboard shows stale/broken data.
--
-- SOLUTION: Confirm all light-regime cron jobs run 7 days a week (* * * * *).
--           The edge function already writes "market_closed" when Yahoo
--           returns zero bars (rare), so there's no downside to running
--           on weekends. Heavy-sweep stays on its daily/weekly schedule
--           since it's more resource-intensive.
--
-- ALSO:     Reduce weekend frequency to every 30 min (vs 15 min weekdays)
--           for stocks/forex/commodities — less load on Supabase workers
--           when markets are closed, but still keeps NaN from accumulating.

-- ─── Helper ────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION cron_safe_unschedule(p_job_name TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM cron.unschedule(p_job_name);
  RAISE NOTICE 'Unscheduled %', p_job_name;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'Job % not found (skipping)', p_job_name;
END;
$$;

-- ─── Reschedule stock light-regime: every 15 min weekdays, every 30 min weekends ──
-- Cron syntax: min hour day month dow
-- Weekday schedule: 5,20,35,50 * * * 1-5   (every 15 min, Mon-Fri)
-- Weekend schedule: 5,35 * * * 0,6         (every 30 min, Sat-Sun)
-- We need TWO jobs per symbol to express this in pg_cron

-- AAPL
SELECT cron_safe_unschedule('light-regime-AAPL');
SELECT cron.schedule('light-regime-AAPL-wk',  '5,20,35,50 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'AAPL');  $$);
SELECT cron.schedule('light-regime-AAPL-we',  '5,35 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'AAPL');  $$);

-- AMZN
SELECT cron_safe_unschedule('light-regime-AMZN');
SELECT cron.schedule('light-regime-AMZN-wk',  '6,21,36,51 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'AMZN');  $$);
SELECT cron.schedule('light-regime-AMZN-we',  '6,36 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'AMZN');  $$);

-- GOOGL
SELECT cron_safe_unschedule('light-regime-GOOGL');
SELECT cron.schedule('light-regime-GOOGL-wk', '5,20,35,50 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'GOOGL'); $$);
SELECT cron.schedule('light-regime-GOOGL-we', '5,35 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'GOOGL'); $$);

-- META
SELECT cron_safe_unschedule('light-regime-META');
SELECT cron.schedule('light-regime-META-wk',  '6,21,36,51 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'META');  $$);
SELECT cron.schedule('light-regime-META-we',  '6,36 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'META');  $$);

-- MSFT
SELECT cron_safe_unschedule('light-regime-MSFT');
SELECT cron.schedule('light-regime-MSFT-wk',  '6,21,36,51 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'MSFT');  $$);
SELECT cron.schedule('light-regime-MSFT-we',  '6,36 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'MSFT');  $$);

-- NVDA
SELECT cron_safe_unschedule('light-regime-NVDA');
SELECT cron.schedule('light-regime-NVDA-wk',  '6,21,36,51 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'NVDA');  $$);
SELECT cron.schedule('light-regime-NVDA-we',  '6,36 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'NVDA');  $$);

-- TSLA
SELECT cron_safe_unschedule('light-regime-TSLA');
SELECT cron.schedule('light-regime-TSLA-wk',  '7,22,37,52 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'TSLA');  $$);
SELECT cron.schedule('light-regime-TSLA-we',  '7,37 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'TSLA');  $$);

-- SPY
SELECT cron_safe_unschedule('light-regime-SPY');
SELECT cron.schedule('light-regime-SPY-wk',   '7,22,37,52 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'SPY');   $$);
SELECT cron.schedule('light-regime-SPY-we',   '7,37 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'SPY');   $$);

-- QQQ
SELECT cron_safe_unschedule('light-regime-QQQ');
SELECT cron.schedule('light-regime-QQQ-wk',   '7,22,37,52 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'QQQ');   $$);
SELECT cron.schedule('light-regime-QQQ-we',   '7,37 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'QQQ');   $$);

-- ─── Forex/Commodities: same treatment ──────────────────────
-- Forex trades Sun 22:00 UTC – Fri 22:00 UTC, so weekend gaps are shorter.
-- Still useful to run regime detection on weekends via daily candle fallback.

-- EURUSD
SELECT cron_safe_unschedule('light-regime-EURUSD');
SELECT cron.schedule('light-regime-EURUSD-wk', '10,25,40,55 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'EURUSD'); $$);
SELECT cron.schedule('light-regime-EURUSD-we', '10,40 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'EURUSD'); $$);

-- GBPUSD
SELECT cron_safe_unschedule('light-regime-GBPUSD');
SELECT cron.schedule('light-regime-GBPUSD-wk', '10,25,40,55 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'GBPUSD'); $$);
SELECT cron.schedule('light-regime-GBPUSD-we', '10,40 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'GBPUSD'); $$);

-- USDJPY
SELECT cron_safe_unschedule('light-regime-USDJPY');
SELECT cron.schedule('light-regime-USDJPY-wk', '10,25,40,55 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'USDJPY'); $$);
SELECT cron.schedule('light-regime-USDJPY-we', '10,40 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'USDJPY'); $$);

-- AUDUSD
SELECT cron_safe_unschedule('light-regime-AUDUSD');
SELECT cron.schedule('light-regime-AUDUSD-wk', '11,26,41,56 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'AUDUSD'); $$);
SELECT cron.schedule('light-regime-AUDUSD-we', '11,41 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'AUDUSD'); $$);

-- USDCAD
SELECT cron_safe_unschedule('light-regime-USDCAD');
SELECT cron.schedule('light-regime-USDCAD-wk', '11,26,41,56 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'USDCAD'); $$);
SELECT cron.schedule('light-regime-USDCAD-we', '11,41 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'USDCAD'); $$);

-- GOLD
SELECT cron_safe_unschedule('light-regime-GOLD');
SELECT cron.schedule('light-regime-GOLD-wk',   '11,26,41,56 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'GOLD');   $$);
SELECT cron.schedule('light-regime-GOLD-we',   '11,41 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'GOLD');   $$);

-- SILVER
SELECT cron_safe_unschedule('light-regime-SILVER');
SELECT cron.schedule('light-regime-SILVER-wk', '12,27,42,57 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'SILVER'); $$);
SELECT cron.schedule('light-regime-SILVER-we', '12,42 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'SILVER'); $$);

-- OIL
SELECT cron_safe_unschedule('light-regime-OIL');
SELECT cron.schedule('light-regime-OIL-wk',    '12,27,42,57 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'OIL');    $$);
SELECT cron.schedule('light-regime-OIL-we',    '12,42 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'OIL');    $$);

-- COPPER
SELECT cron_safe_unschedule('light-regime-COPPER');
SELECT cron.schedule('light-regime-COPPER-wk', '12,27,42,57 * * * 1-5', $$ SELECT sim_invoke_edge_function('light-regime', 'COPPER'); $$);
SELECT cron.schedule('light-regime-COPPER-we', '12,42 * * * 0,6',       $$ SELECT sim_invoke_edge_function('light-regime', 'COPPER'); $$);

-- ─── Crypto stays 24/7 (no change needed) ──────────────────
-- Crypto cron jobs already run * * * * * (every day) — no changes.

-- ─── Update sim_symbols regime_cron to reflect new schedules ──
UPDATE sim_symbols SET regime_cron = '5,20,35,50 * * * 1-5 / 5,35 * * * 0,6' WHERE symbol IN ('AAPL','GOOGL');
UPDATE sim_symbols SET regime_cron = '6,21,36,51 * * * 1-5 / 6,36 * * * 0,6' WHERE symbol IN ('AMZN','META','MSFT','NVDA');
UPDATE sim_symbols SET regime_cron = '7,22,37,52 * * * 1-5 / 7,37 * * * 0,6' WHERE symbol IN ('TSLA','SPY','QQQ');
UPDATE sim_symbols SET regime_cron = '10,25,40,55 * * * 1-5 / 10,40 * * * 0,6' WHERE symbol IN ('EURUSD','GBPUSD','USDJPY');
UPDATE sim_symbols SET regime_cron = '11,26,41,56 * * * 1-5 / 11,41 * * * 0,6' WHERE symbol IN ('AUDUSD','USDCAD','GOLD');
UPDATE sim_symbols SET regime_cron = '12,27,42,57 * * * 1-5 / 12,42 * * * 0,6' WHERE symbol IN ('SILVER','OIL','COPPER');

-- Clean up
DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);
