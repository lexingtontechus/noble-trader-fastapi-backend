-- ══════════════════════════════════════════════════════════════════════════════
-- Noble Trader — Extended System Config Seeds
-- ══════════════════════════════════════════════════════════════════════════════
-- Adds config keys for execution financing, fill probability internals,
-- Alpaca WS timeouts/throttling, stream subscriber queue, and risk
-- stress-test extended params.  These keys have DB seeds but were previously
-- hardcoded in the Python modules.
--
-- Run AFTER 007_system_config.sql (adds to the same table).
-- Uses ON CONFLICT (key) DO NOTHING so re-runs are safe.
-- ══════════════════════════════════════════════════════════════════════════════

-- ─── Execution: Financing ────────────────────────────────────────────────────

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('exec.borrow_rate_bps',           '50.0',  'float', 'execution', 'Annualized short borrow cost (bps)',         '50.0',  'EXEC_BORROW_RATE_BPS',           '0.0',   '5000.0'),
('exec.margin_rate_bps',           '150.0', 'float', 'execution', 'Annualized margin interest rate (bps)',      '150.0', 'EXEC_MARGIN_RATE_BPS',           '0.0',   '5000.0'),
('exec.dividend_yield_bps',        '200.0', 'float', 'execution', 'Annualized dividend yield for short cost',   '200.0', 'EXEC_DIVIDEND_YIELD_BPS',        '0.0',   '5000.0'),
('exec.trading_days_per_quarter',  '63',    'int',   'execution', 'Trading days per quarter (dividend prob)',   '63',    'EXEC_TRADING_DAYS_PER_QUARTER',  '50',    '70')
ON CONFLICT (key) DO NOTHING;

-- ─── Execution: Fill Probability Internals ───────────────────────────────────

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('exec.default_participation_rate', '0.01', 'float', 'execution', 'Default volume participation rate',         '0.01',  'EXEC_DEFAULT_PARTICIPATION_RATE', '0.001', '0.10'),
('exec.volume_penalty_multiplier',  '10.0', 'float', 'execution', 'Volume penalty scale factor',               '10.0',  'EXEC_VOLUME_PENALTY_MULTIPLIER',  '1.0',   '50.0'),
('exec.max_volume_penalty',         '0.3',  'float', 'execution', 'Max volume penalty fraction',                '0.3',   'EXEC_MAX_VOLUME_PENALTY',         '0.05',  '0.80'),
('exec.stop_min_fill_probability',  '0.90', 'float', 'execution', 'Min fill prob for stop orders',              '0.90',  'EXEC_STOP_MIN_FILL_PROBABILITY',  '0.50',  '1.0'),
('exec.stop_adverse_sigma',         '0.5',  'float', 'execution', 'Adverse selection sigma for stop orders',    '0.5',   'EXEC_STOP_ADVERSE_SIGMA',         '0.1',   '3.0'),
('exec.limit_adverse_sigma',        '0.2',  'float', 'execution', 'Adverse selection sigma for limit orders',   '0.2',   'EXEC_LIMIT_ADVERSE_SIGMA',        '0.05',  '2.0'),
('exec.fill_interp_high',           '0.8',  'float', 'execution', 'High-fill interpretation threshold',         '0.8',   'EXEC_FILL_INTERP_HIGH',           '0.5',   '1.0'),
('exec.fill_interp_mid',            '0.5',  'float', 'execution', 'Mid-fill interpretation threshold',          '0.5',   'EXEC_FILL_INTERP_MID',            '0.2',   '0.8'),
('exec.fill_interp_low',            '0.2',  'float', 'execution', 'Low-fill interpretation threshold',          '0.2',   'EXEC_FILL_INTERP_LOW',            '0.05',  '0.5')
ON CONFLICT (key) DO NOTHING;

-- ─── Stream: Subscriber Queue ───────────────────────────────────────────────

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('stream.subscriber_queue_size',    '500',   'int',   'stream', 'Per-subscriber asyncio.Queue maxsize',       '500',   'STREAM_SUBSCRIBER_QUEUE_SIZE',   '50',    '5000')
ON CONFLICT (key) DO NOTHING;

-- ─── Alpaca: WS Timeouts & Throttling ───────────────────────────────────────

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('alpaca.ws_close_timeout',                '5',    'int',   'alpaca', 'WS close handshake timeout (seconds)',      '5',     'ALPACA_WS_CLOSE_TIMEOUT',                '1',     '30'),
('alpaca.ws_backoff_cap',                  '8',    'int',   'alpaca', 'Max reconnect backoff exponent cap',        '8',     'ALPACA_WS_BACKOFF_CAP',                  '2',     '15'),
('alpaca.supabase_http_timeout',           '10.0', 'float', 'alpaca', 'Supabase credential lookup HTTP timeout',   '10.0',  'ALPACA_SUPABASE_HTTP_TIMEOUT',           '1.0',   '30.0'),
('alpaca.bootstrap_http_timeout',          '10.0', 'float', 'alpaca', 'Bootstrap REST API HTTP timeout',           '10.0',  'ALPACA_BOOTSTRAP_HTTP_TIMEOUT',          '1.0',   '30.0'),
('alpaca.bootstrap_throttle_timeout',      '10.0', 'float', 'alpaca', 'Bootstrap throttle acquire timeout',        '10.0',  'ALPACA_BOOTSTRAP_THROTTLE_TIMEOUT',      '1.0',   '30.0'),
('alpaca.position_refresh_http_timeout',   '5.0',  'float', 'alpaca', 'Position refresh REST HTTP timeout',        '5.0',   'ALPACA_POSITION_REFRESH_HTTP_TIMEOUT',   '1.0',   '15.0'),
('alpaca.position_refresh_throttle_timeout','5.0', 'float', 'alpaca', 'Position refresh throttle acquire timeout', '5.0',   'ALPACA_POSITION_REFRESH_THROTTLE_TIMEOUT','1.0','15.0'),
('alpaca.data_no_position_wait_timeout',   '5.0',  'float', 'alpaca', 'Wait timeout when no positions held',       '5.0',   'ALPACA_DATA_NO_POSITION_WAIT_TIMEOUT',   '1.0',   '30.0'),
('alpaca.data_tick_min_interval',          '1.0',  'float', 'alpaca', 'Min seconds between ticks per symbol',      '1.0',   'ALPACA_DATA_TICK_MIN_INTERVAL',          '0.1',   '10.0'),
('alpaca.data_resubscribe_interval',       '2.0',  'float', 'alpaca', 'Re-subscribe check interval (seconds)',     '2.0',   'ALPACA_DATA_RESUBSCRIBE_INTERVAL',       '0.5',   '10.0')
ON CONFLICT (key) DO NOTHING;

-- ─── Risk: Stress Test Extended ──────────────────────────────────────────────

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('risk.stress_min_returns',                '20',   'int',   'risk',   'Min returns required for stress tests',     '20',    'RISK_STRESS_MIN_RETURNS',                '5',     '100'),
('risk.stress_liquidity_downside_mult',    '1.5',  'float', 'risk',   'Liquidity crisis downside multiplier',      '1.5',   'RISK_STRESS_LIQUIDITY_DOWNSIDE_MULT',    '1.0',   '5.0'),
('risk.stress_liquidity_upside_mult',      '0.8',  'float', 'risk',   'Liquidity crisis upside multiplier',        '0.8',   'RISK_STRESS_LIQUIDITY_UPSIDE_MULT',      '0.1',   '1.0')
ON CONFLICT (key) DO NOTHING;
