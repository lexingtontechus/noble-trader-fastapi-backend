-- ═══════════════════════════════════════════════════════════════
-- 014: Simplify to 15 symbols (3 crypto × 2 venues + 9 stocks)
--
-- Deactivates: 6 extra HL crypto, 5 forex, 4 commodities
-- Adds: SOLUSD (Alpaca crypto spot)
-- Reactivates: META (was inactive)
-- ═══════════════════════════════════════════════════════════════

-- 1. Deactivate removed symbols
UPDATE sim_symbols SET active = false
WHERE symbol IN ('AVAX','ARB','DOGE','LINK','XRP','WIF',
                 'EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD',
                 'GOLD','SILVER','OIL','COPPER');

-- 2. Add SOLUSD (3rd Alpaca crypto spot)
INSERT INTO sim_symbols (symbol, asset_class, data_source, yahoo_ticker, hyperliquid_coin,
                         sweep_cron, regime_cron, active, heartbeat_ttl_seconds, alpaca_symbol)
VALUES ('SOLUSD', 'crypto', 'alpaca', 'SOL-USD', NULL,
        '30 2,6,10,14,18,22 * * *', '*/15 * * * *', true, 900, 'SOL/USD')
ON CONFLICT (symbol) DO UPDATE
SET active = true, alpaca_symbol = 'SOL/USD', data_source = 'alpaca',
    asset_class = 'crypto', heartbeat_ttl_seconds = 900;

-- 3. Reactivate META (was inactive with 10-day TTL)
UPDATE sim_symbols SET active = true, heartbeat_ttl_seconds = 900
WHERE symbol = 'META' AND active = false;

-- ═══════════════════════════════════════════════════════════════
-- SQL function to sync sim_symbols → Redis allowlist + venue_map
-- Must be called after setting vault secrets:
--   vault.create_secret('upstash_redis_url', '<URL>', 'Upstash Redis REST URL');
--   vault.create_secret('upstash_redis_token', '<TOKEN>', 'Upstash Redis REST Token');
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION sim_sync_redis_allowlist()
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
  v_url      text;
  v_token    text;
  v_result   jsonb;
  hl_symbols text[];
  alpaca_symbols text[];
  rec        record;
  cmd_json   jsonb;
  all_cmds   jsonb := '[]';
  del_cmds   jsonb := '[]';
BEGIN
  -- Read Redis credentials from vault
  v_url   := get_vault_secret('upstash_redis_url');
  v_token := get_vault_secret('upstash_redis_token');

  IF v_url IS NULL OR v_token IS NULL THEN
    RETURN jsonb_build_object(
      'ok', false,
      'error', 'Vault secrets upstash_redis_url / upstash_redis_token not found. Run vault.create_secret() first.'
    );
  END IF;

  -- Collect active symbols by venue
  hl_symbols     := ARRAY[]::text[];
  alpaca_symbols := ARRAY[]::text[];

  FOR rec IN
    SELECT symbol, data_source, asset_class, alpaca_symbol, hyperliquid_coin
    FROM sim_symbols WHERE active = true
    ORDER BY symbol
  LOOP
    IF rec.data_source = 'hyperliquid' THEN
      hl_symbols := hl_symbols || rec.symbol;
    ELSIF rec.data_source = 'alpaca' THEN
      alpaca_symbols := alpaca_symbols || rec.symbol;
    END IF;
  END LOOP;

  -- Build Redis pipeline commands:
  -- 1. DEL + SADD for hyperliquid allowlist
  -- 2. DEL + SADD for alpaca allowlist
  -- 3. HSET for each venue_map entry
  -- 4. DEL for all deactivated venue_map keys

  -- Delete old allowlists
  del_cmds := del_cmds || jsonb_build_array('DEL', 'trading:allowlist:hyperliquid');
  del_cmds := del_cmds || jsonb_build_array('DEL', 'trading:allowlist:alpaca');

  -- SADD hyperliquid allowlist
  cmd_json := jsonb_build_array('SADD', 'trading:allowlist:hyperliquid');
  FOR i IN 1..array_length(hl_symbols, 1) LOOP
    cmd_json := cmd_json || jsonb_build_array(hl_symbols[i]);
  END LOOP;
  all_cmds := all_cmds || jsonb_build_array(del_cmds -> 0);
  all_cmds := all_cmds || jsonb_build_array(cmd_json);

  -- SADD alpaca allowlist
  cmd_json := jsonb_build_array('SADD', 'trading:allowlist:alpaca');
  FOR i IN 1..array_length(alpaca_symbols, 1) LOOP
    cmd_json := cmd_json || jsonb_build_array(alpaca_symbols[i]);
  END LOOP;
  all_cmds := all_cmds || jsonb_build_array(del_cmds -> 1);
  all_cmds := all_cmds || jsonb_build_array(cmd_json);

  -- HSET venue_map for each active symbol
  FOR rec IN
    SELECT symbol, data_source, asset_class, alpaca_symbol, hyperliquid_coin
    FROM sim_symbols WHERE active = true
    ORDER BY symbol
  LOOP
    IF rec.data_source = 'hyperliquid' THEN
      cmd_json := jsonb_build_array(
        'HSET', 'trading:venue_map:' || rec.symbol,
        'venueId', 'hyperliquid',
        'assetClass', rec.asset_class
      );
    ELSIF rec.data_source = 'alpaca' THEN
      IF rec.alpaca_symbol IS NOT NULL THEN
        cmd_json := jsonb_build_array(
          'HSET', 'trading:venue_map:' || rec.symbol,
          'venueId', 'alpaca',
          'assetClass', rec.asset_class,
          'alpacaSymbol', rec.alpaca_symbol
        );
      ELSE
        cmd_json := jsonb_build_array(
          'HSET', 'trading:venue_map:' || rec.symbol,
          'venueId', 'alpaca',
          'assetClass', rec.asset_class
        );
      END IF;
    END IF;
    all_cmds := all_cmds || jsonb_build_array(cmd_json);
  END LOOP;

  -- DEL venue_map for all deactivated symbols
  FOR rec IN
    SELECT symbol FROM sim_symbols WHERE active = false
  LOOP
    all_cmds := all_cmds || jsonb_build_array('DEL', 'trading:venue_map:' || rec.symbol);
  END LOOP;

  -- Execute Redis pipeline via Upstash REST API
  SELECT net.http_post(
    url    := v_url || '/pipeline',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || v_token,
      'Content-Type',  'application/json'
    ),
    body   := all_cmds
  ) INTO v_result;

  RETURN jsonb_build_object(
    'ok', true,
    'hl_symbols', hl_symbols,
    'alpaca_symbols', alpaca_symbols,
    'total_active', array_length(hl_symbols, 1) + array_length(alpaca_symbols, 1),
    'pipeline_commands', jsonb_array_length(all_cmds)
  );
END;
$$;

-- ═══════════════════════════════════════════════════════════════
-- Auto-sync trigger: when sim_symbols changes, update Redis
-- ═══════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION sim_symbols_sync_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  -- Fire-and-forget sync (errors logged but don't block the DML)
  BEGIN
    PERFORM sim_sync_redis_allowlist();
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Redis sync failed: %', SQLERRM;
  END;
  RETURN COALESCE(NEW, OLD);
END;
$$;

-- Trigger on INSERT, UPDATE, DELETE
DROP TRIGGER IF EXISTS sim_symbols_redis_sync ON sim_symbols;
CREATE TRIGGER sim_symbols_redis_sync
  AFTER INSERT OR UPDATE OF active, data_source, asset_class, alpaca_symbol, hyperliquid_coin
  OR DELETE ON sim_symbols
  FOR EACH ROW
  EXECUTE FUNCTION sim_symbols_sync_trigger();
