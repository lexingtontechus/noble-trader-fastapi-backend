-- Migration 001: Create sim_symbols table and supporting infrastructure
-- This is the canonical schema for the Intelligence system's symbol registry.
-- Run this against your Supabase PostgreSQL instance.

-- ─── sim_symbols table ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS sim_symbols (
  symbol                TEXT PRIMARY KEY,
  asset_class           TEXT NOT NULL CHECK (asset_class IN ('crypto', 'forex', 'stocks', 'commodities')),
  data_source           TEXT NOT NULL DEFAULT 'yahoo' CHECK (data_source IN ('yahoo', 'hyperliquid')),
  yahoo_ticker          TEXT,              -- e.g. 'AAPL', 'EURUSD=X'
  hyperliquid_coin      TEXT,              -- e.g. 'BTC', 'ETH'
  active                BOOLEAN NOT NULL DEFAULT true,
  heartbeat_ttl_seconds INTEGER NOT NULL DEFAULT 900,  -- 15 min
  sweep_cron            TEXT NOT NULL DEFAULT '0 */4 * * *',
  regime_cron           TEXT NOT NULL DEFAULT '*/15 * * * *',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── sim_sweep_results table ───────────────────────────────

CREATE TABLE IF NOT EXISTS sim_sweep_results (
  id              BIGSERIAL PRIMARY KEY,
  symbol          TEXT NOT NULL REFERENCES sim_symbols(symbol),
  asset_class     TEXT NOT NULL,
  brick_size      DOUBLE PRECISION NOT NULL,
  sl_bricks       INTEGER NOT NULL,
  tp_bricks       INTEGER NOT NULL,
  sharpe          DOUBLE PRECISION,
  total_return    DOUBLE PRECISION,
  win_rate        DOUBLE PRECISION,
  n_trades        INTEGER,
  profit_factor   DOUBLE PRECISION,
  regime          TEXT,
  regime_conf     DOUBLE PRECISION,
  kelly_f         DOUBLE PRECISION,
  sweep_timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sweep_symbol ON sim_sweep_results(symbol);
CREATE INDEX IF NOT EXISTS idx_sweep_timestamp ON sim_sweep_results(sweep_timestamp DESC);

-- ─── sim_regime_log table ──────────────────────────────────

CREATE TABLE IF NOT EXISTS sim_regime_log (
  id              BIGSERIAL PRIMARY KEY,
  symbol          TEXT NOT NULL REFERENCES sim_symbols(symbol),
  source          TEXT NOT NULL DEFAULT 'light-regime',
  regime          TEXT NOT NULL,
  regime_conf     DOUBLE PRECISION,
  markov_p_up     DOUBLE PRECISION,
  markov_p_dn     DOUBLE PRECISION,
  kelly_f         DOUBLE PRECISION,
  prev_regime     TEXT,
  regime_shift    BOOLEAN NOT NULL DEFAULT false,
  detected_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_regime_symbol ON sim_regime_log(symbol);
CREATE INDEX IF NOT EXISTS idx_regime_detected ON sim_regime_log(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_regime_shift ON sim_regime_log(regime_shift) WHERE regime_shift = true;

-- ─── Enable required extensions ────────────────────────────

CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net SCHEMA extensions;

-- ─── Helper: invoke edge function via pg_net ───────────────
-- Replace <SUPABASE_URL> and <SUPABASE_SERVICE_ROLE> with your actual values

CREATE OR REPLACE FUNCTION sim_invoke_edge_function(
  p_function TEXT,
  p_symbol   TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
  v_url     TEXT := '<SUPABASE_URL>/functions/v1/' || p_function;
  v_auth    TEXT := 'Bearer <SUPABASE_SERVICE_ROLE>';
BEGIN
  PERFORM net.http_post(
    url     := v_url,
    headers := jsonb_build_object(
      'Content-Type',  'application/json',
      'Authorization', v_auth
    ),
    body    := jsonb_build_object('symbol', p_symbol)
  );
END;
$$;

-- ─── Seed: All 27 symbols ──────────────────────────────────

INSERT INTO sim_symbols (symbol, asset_class, data_source, yahoo_ticker, hyperliquid_coin, active, heartbeat_ttl_seconds, sweep_cron, regime_cron) VALUES
  -- Crypto (8)
  ('BTC',    'crypto', 'hyperliquid', NULL,   'BTC',    true, 900, '0 */4 * * *',     '*/15 * * * *'),
  ('ETH',    'crypto', 'hyperliquid', NULL,   'ETH',    true, 900, '5 */4 * * *',     '*/15 * * * *'),
  ('SOL',    'crypto', 'hyperliquid', NULL,   'SOL',    true, 900, '10 */4 * * *',    '*/15 * * * *'),
  ('AVAX',   'crypto', 'hyperliquid', NULL,   'AVAX',   true, 900, '15 */4 * * *',    '*/15 * * * *'),
  ('DOT',    'crypto', 'hyperliquid', NULL,   'DOT',    true, 900, '20 */4 * * *',    '*/15 * * * *'),
  ('LINK',   'crypto', 'hyperliquid', NULL,   'LINK',   true, 900, '25 */4 * * *',    '*/15 * * * *'),
  ('MATIC',  'crypto', 'hyperliquid', NULL,   'MATIC',  true, 900, '30 */4 * * *',    '*/15 * * * *'),
  ('ADA',    'crypto', 'hyperliquid', NULL,   'ADA',    true, 900, '35 */4 * * *',    '*/15 * * * *'),
  -- Forex (5)
  ('EURUSD', 'forex', 'yahoo', 'EURUSD=X', NULL, true, 900, '40 */4 * * *',   '*/15 * * * *'),
  ('GBPUSD', 'forex', 'yahoo', 'GBPUSD=X', NULL, true, 900, '45 */4 * * *',   '*/15 * * * *'),
  ('USDJPY', 'forex', 'yahoo', 'USDJPY=X', NULL, true, 900, '50 */4 * * *',   '*/15 * * * *'),
  ('AUDUSD', 'forex', 'yahoo', 'AUDUSD=X', NULL, true, 900, '55 */4 * * *',   '*/15 * * * *'),
  ('USDCAD', 'forex', 'yahoo', 'USDCAD=X', NULL, true, 900, '0 */4 * * *',    '*/15 * * * *'),
  -- Stocks & ETFs (8)
  ('AAPL',   'stocks', 'yahoo', 'AAPL', NULL, true, 900, '5 */4 * * *',     '*/15 * * * *'),
  ('MSFT',   'stocks', 'yahoo', 'MSFT', NULL, true, 900, '10 */4 * * *',    '*/15 * * * *'),
  ('GOOGL',  'stocks', 'yahoo', 'GOOGL', NULL, true, 900, '15 */4 * * *',   '*/15 * * * *'),
  ('AMZN',   'stocks', 'yahoo', 'AMZN', NULL, true, 900, '20 */4 * * *',    '*/15 * * * *'),
  ('NVDA',   'stocks', 'yahoo', 'NVDA', NULL, true, 900, '25 */4 * * *',    '*/15 * * * *'),
  ('TSLA',   'stocks', 'yahoo', 'TSLA', NULL, true, 900, '30 */4 * * *',    '*/15 * * * *'),
  ('SPY',    'stocks', 'yahoo', 'SPY',  NULL, true, 900, '35 */4 * * *',    '*/15 * * * *'),
  ('QQQ',    'stocks', 'yahoo', 'QQQ',  NULL, true, 900, '40 */4 * * *',    '*/15 * * * *'),
  -- Commodities (3)
  ('GOLD',   'commodities', 'yahoo', 'GC=F',   NULL, true, 900, '45 */4 * * *',  '*/15 * * * *'),
  ('SILVER', 'commodities', 'yahoo', 'SI=F',   NULL, true, 900, '50 */4 * * *',  '*/15 * * * *'),
  ('OIL',    'commodities', 'yahoo', 'CL=F',   NULL, true, 900, '55 */4 * * *',  '*/15 * * * *')
ON CONFLICT (symbol) DO UPDATE SET
  asset_class = EXCLUDED.asset_class,
  data_source = EXCLUDED.data_source,
  yahoo_ticker = EXCLUDED.yahoo_ticker,
  hyperliquid_coin = EXCLUDED.hyperliquid_coin,
  active = EXCLUDED.active,
  heartbeat_ttl_seconds = EXCLUDED.heartbeat_ttl_seconds,
  sweep_cron = EXCLUDED.sweep_cron,
  regime_cron = EXCLUDED.regime_cron,
  updated_at = now();

-- ─── Enable RLS ────────────────────────────────────────────

ALTER TABLE sim_symbols ENABLE ROW LEVEL SECURITY;
ALTER TABLE sim_sweep_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE sim_regime_log ENABLE ROW LEVEL SECURITY;

-- Service role can do everything
CREATE POLICY "Service role full access" ON sim_symbols FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON sim_sweep_results FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access" ON sim_regime_log FOR ALL USING (true) WITH CHECK (true);
