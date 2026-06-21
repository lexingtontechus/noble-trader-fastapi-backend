-- Patch 006: Stagger light-regime cron jobs to reduce concurrent cold starts
-- ========================================================================
-- PROBLEM: All 9 crypto jobs fire at :00/:15/:30/:45 simultaneously,
--          causing BOOT_ERROR (503) when Supabase can't spin up 9
--          workers at once. Same issue for stocks (9 at :05/:20/:35/:50)
--          and forex/commodities (9 at :10/:25/:40/:55).
--
-- SOLUTION: Spread each batch across 3 sub-batches with 1-minute offsets.
--           Instead of 9 at once, we get 3 at a time with 1 min gaps.
--           This gives Supabase time to reuse warm workers.
--
-- Rate limit check: 3 symbols × 2 calls (regime+Kelly) = 6 req per sub-batch
-- With 1-min gaps between sub-batches: 6 req in min 1 + 6 req in min 2 + 6 req in min 3
-- = 18 req over 3 min ≈ 6 req/min — still under Noble Trader's 10/min limit.

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

-- ─── Unschedule current light-regime jobs ──────────────────
SELECT cron_safe_unschedule('light-regime-BTC');
SELECT cron_safe_unschedule('light-regime-ETH');
SELECT cron_safe_unschedule('light-regime-SOL');
SELECT cron_safe_unschedule('light-regime-AVAX');
SELECT cron_safe_unschedule('light-regime-ARB');
SELECT cron_safe_unschedule('light-regime-DOGE');
SELECT cron_safe_unschedule('light-regime-LINK');
SELECT cron_safe_unschedule('light-regime-XRP');
SELECT cron_safe_unschedule('light-regime-WIF');
SELECT cron_safe_unschedule('light-regime-AAPL');
SELECT cron_safe_unschedule('light-regime-AMZN');
SELECT cron_safe_unschedule('light-regime-GOOGL');
SELECT cron_safe_unschedule('light-regime-META');
SELECT cron_safe_unschedule('light-regime-MSFT');
SELECT cron_safe_unschedule('light-regime-NVDA');
SELECT cron_safe_unschedule('light-regime-TSLA');
SELECT cron_safe_unschedule('light-regime-SPY');
SELECT cron_safe_unschedule('light-regime-QQQ');
SELECT cron_safe_unschedule('light-regime-EURUSD');
SELECT cron_safe_unschedule('light-regime-GBPUSD');
SELECT cron_safe_unschedule('light-regime-USDJPY');
SELECT cron_safe_unschedule('light-regime-AUDUSD');
SELECT cron_safe_unschedule('light-regime-USDCAD');
SELECT cron_safe_unschedule('light-regime-GOLD');
SELECT cron_safe_unschedule('light-regime-SILVER');
SELECT cron_safe_unschedule('light-regime-OIL');
SELECT cron_safe_unschedule('light-regime-COPPER');

-- ─── Reschedule with 1-minute stagger within each batch ───
-- Crypto Sub-batch A1: :00, :15, :30, :45  (3 symbols)
SELECT cron.schedule('light-regime-BTC',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'BTC');  $$);
SELECT cron.schedule('light-regime-ETH',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'ETH');  $$);
SELECT cron.schedule('light-regime-SOL',  '0,15,30,45 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'SOL');  $$);

-- Crypto Sub-batch A2: :01, :16, :31, :46  (3 symbols)
SELECT cron.schedule('light-regime-AVAX', '1,16,31,46 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AVAX'); $$);
SELECT cron.schedule('light-regime-ARB',  '1,16,31,46 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'ARB');  $$);
SELECT cron.schedule('light-regime-DOGE', '1,16,31,46 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'DOGE'); $$);

-- Crypto Sub-batch A3: :02, :17, :32, :47  (3 symbols)
SELECT cron.schedule('light-regime-LINK', '2,17,32,47 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'LINK'); $$);
SELECT cron.schedule('light-regime-XRP',  '2,17,32,47 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'XRP');  $$);
SELECT cron.schedule('light-regime-WIF',  '2,17,32,47 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'WIF');  $$);

-- Stocks Sub-batch B1: :05, :20, :35, :50  (3 symbols)
SELECT cron.schedule('light-regime-AAPL', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AAPL');  $$);
SELECT cron.schedule('light-regime-AMZN', '5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AMZN');  $$);
SELECT cron.schedule('light-regime-GOOGL','5,20,35,50 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'GOOGL'); $$);

-- Stocks Sub-batch B2: :06, :21, :36, :51  (3 symbols)
SELECT cron.schedule('light-regime-META', '6,21,36,51 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'META');  $$);
SELECT cron.schedule('light-regime-MSFT', '6,21,36,51 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'MSFT');  $$);
SELECT cron.schedule('light-regime-NVDA', '6,21,36,51 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'NVDA');  $$);

-- Stocks Sub-batch B3: :07, :22, :37, :52  (3 symbols)
SELECT cron.schedule('light-regime-TSLA', '7,22,37,52 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'TSLA');  $$);
SELECT cron.schedule('light-regime-SPY',  '7,22,37,52 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'SPY');   $$);
SELECT cron.schedule('light-regime-QQQ',  '7,22,37,52 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'QQQ');   $$);

-- Forex/Commodities Sub-batch C1: :10, :25, :40, :55  (3 symbols)
SELECT cron.schedule('light-regime-EURUSD','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'EURUSD'); $$);
SELECT cron.schedule('light-regime-GBPUSD','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'GBPUSD'); $$);
SELECT cron.schedule('light-regime-USDJPY','10,25,40,55 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'USDJPY'); $$);

-- Forex/Commodities Sub-batch C2: :11, :26, :41, :56  (3 symbols)
SELECT cron.schedule('light-regime-AUDUSD','11,26,41,56 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'AUDUSD'); $$);
SELECT cron.schedule('light-regime-USDCAD','11,26,41,56 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'USDCAD'); $$);
SELECT cron.schedule('light-regime-GOLD',  '11,26,41,56 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'GOLD');   $$);

-- Forex/Commodities Sub-batch C3: :12, :27, :42, :57  (3 symbols)
SELECT cron.schedule('light-regime-SILVER','12,27,42,57 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'SILVER'); $$);
SELECT cron.schedule('light-regime-OIL',   '12,27,42,57 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'OIL');    $$);
SELECT cron.schedule('light-regime-COPPER','12,27,42,57 * * * *', $$ SELECT sim_invoke_edge_function('light-regime', 'COPPER'); $$);

-- ─── Update sim_symbols regime_cron to reflect new schedules ──
UPDATE sim_symbols SET regime_cron = '0,15,30,45 * * * *' WHERE symbol IN ('BTC','ETH','SOL');
UPDATE sim_symbols SET regime_cron = '1,16,31,46 * * * *' WHERE symbol IN ('AVAX','ARB','DOGE');
UPDATE sim_symbols SET regime_cron = '2,17,32,47 * * * *' WHERE symbol IN ('LINK','XRP','WIF');
UPDATE sim_symbols SET regime_cron = '5,20,35,50 * * * *' WHERE symbol IN ('AAPL','AMZN','GOOGL');
UPDATE sim_symbols SET regime_cron = '6,21,36,51 * * * *' WHERE symbol IN ('META','MSFT','NVDA');
UPDATE sim_symbols SET regime_cron = '7,22,37,52 * * * *' WHERE symbol IN ('TSLA','SPY','QQQ');
UPDATE sim_symbols SET regime_cron = '10,25,40,55 * * * *' WHERE symbol IN ('EURUSD','GBPUSD','USDJPY');
UPDATE sim_symbols SET regime_cron = '11,26,41,56 * * * *' WHERE symbol IN ('AUDUSD','USDCAD','GOLD');
UPDATE sim_symbols SET regime_cron = '12,27,42,57 * * * *' WHERE symbol IN ('SILVER','OIL','COPPER');

-- Clean up
DROP FUNCTION IF EXISTS cron_safe_unschedule(TEXT);
