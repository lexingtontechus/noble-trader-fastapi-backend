-- ═══════════════════════════════════════════════════════════════
-- 019: ASSET-CLASS-AWARE STALE-RETRY SKIP (Phase 1 — P1.b)
--
-- BACKGROUND:
--   Migration 017 added heavy-hour awareness to sim_retry_stale_symbols()
--   by hardcoding the 6 heavy hours (02,06,10,14,18,22 UTC). This was
--   correct under migration 015's flat 4h schedule: every symbol's
--   heavy sweep fell in one of those 6 hours.
--
--   Migration 018 changes that:
--     - Crypto symbols (BTC/ETH/SOL/BTCUSD/ETHUSD) still sweep at
--       hours 02/06/10/14/18/22 UTC.
--     - Stock/ETF symbols (AAPL/AMZN/GOOGL/META/MSFT/NVDA/TSLA/SPY/
--       QQQ) now sweep ONCE DAILY at 21:xx UTC (single hour).
--
--   So the 017 hardcode is now wrong for stocks/ETFs: a stale retry
--   at 21:10 UTC for AAPL would collide with the 21:00 heavy sweep
--   cron, while a stale retry at 14:10 UTC (formerly a heavy hour
--   for AAPL) is now harmless.
--
-- FIX:
--   Replace the global heavy-hour check with an asset-class-aware one.
--   For each stale symbol, look up its asset_class and decide:
--     - crypto       → skip during 02/06/10/14/18/22 UTC (6 heavy hrs)
--     - stocks/etf   → skip during 21 UTC (single daily heavy hr)
--     - forex        → skip during 22 UTC (when forex heavy is scheduled)
--     - commodities  → skip during 22 UTC (15-min offset from forex)
--
--   This keeps the collision-prevention guarantee from 017 while
--   honoring the new per-asset-class heavy schedule.
--
-- FORWARD NOTE (TimesFM, P3/P4):
--   When TimesFM-based sweep gating lands, this function will gain
--   a "skip if forecast says regime stable" branch — further
--   reducing both retry-stale invocations and heavy sweeps.
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION sim_retry_stale_symbols()
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  v_symbol     TEXT;
  v_asset      TEXT;
  v_url        TEXT := 'https://pcvscowltlrxzgxjurcr.supabase.co/functions/v1/sweep';
  v_auth       TEXT;
  v_request_id BIGINT;
  v_status     INTEGER;
  v_hour_utc   INTEGER;
  v_skipped    INTEGER := 0;
  v_invoked    INTEGER := 0;
BEGIN
  v_auth := 'Bearer ' || vault_read('supabase_service_role');
  v_hour_utc := EXTRACT(HOUR FROM now() AT TIME ZONE 'UTC')::INTEGER;

  FOR v_symbol, v_asset IN
    SELECT s.symbol, s.asset_class
    FROM sim_symbols s
    WHERE s.active = true
      AND NOT EXISTS (
        SELECT 1
        FROM sim_regime_log rl
        WHERE rl.symbol = s.symbol
          AND rl.detected_at > now() - interval '20 minutes'
      )
    ORDER BY s.symbol
  LOOP
    -- Asset-class-aware heavy-hour skip (replaces 017's global hardcode)
    IF (
         (v_asset = 'crypto'      AND v_hour_utc IN (2, 6, 10, 14, 18, 22))
      OR (v_asset IN ('stocks','etf') AND v_hour_utc = 21)
      OR (v_asset = 'forex'       AND v_hour_utc = 22)
      OR (v_asset = 'commodities' AND v_hour_utc = 22)
    ) THEN
      RAISE NOTICE '[retry-sweep] % (%): SKIP — heavy hour % UTC, deferring to heavy sweep cron',
        v_symbol, v_asset, v_hour_utc;
      v_skipped := v_skipped + 1;
      CONTINUE;
    END IF;

    RAISE NOTICE '[retry-sweep] % (%): invoke mode=light (hour % UTC)',
      v_symbol, v_asset, v_hour_utc;

    SELECT net.http_post(
      url     := v_url,
      headers := jsonb_build_object(
        'Content-Type',  'application/json',
        'Authorization', v_auth
      ),
      body    := jsonb_build_object('mode', 'light', 'symbol', v_symbol)
    ) INTO v_request_id;

    BEGIN
      v_status := (net.http_collect_response(v_request_id)).status_code;
    EXCEPTION WHEN OTHERS THEN
      v_status := NULL;
    END;

    RAISE NOTICE '[retry-sweep] %: status=%', v_symbol, v_status;
    v_invoked := v_invoked + 1;

    -- Small delay between retries to respect global concurrency cap (4)
    PERFORM pg_sleep(2);
  END LOOP;

  RAISE NOTICE '[retry-sweep] done: invoked=%, skipped (heavy hour)=%',
    v_invoked, v_skipped;
END;
$$;

-- ═══════════════════════════════════════════════════════════════
-- VERIFY (run manually after applying):
--   SELECT prosrc FROM pg_proc WHERE proname = 'sim_retry_stale_symbols';
--   -- Should contain 'v_asset IN (''stocks'',''etf')' and 'v_hour_utc = 21'
-- ═══════════════════════════════════════════════════════════════
