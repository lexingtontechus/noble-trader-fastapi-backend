-- ══════════════════════════════════════════════════════════════════════════════
-- Noble Trader — System Configuration Table
-- ══════════════════════════════════════════════════════════════════════════════
-- Runtime-configurable parameters for admin adjustment without redeployment.
-- Supports hierarchical keys (e.g. "renko.brick_size"), typed values,
-- audit trail of changes, and category-based grouping.
--
-- Resolution order: DB override → env var → hardcoded default
-- ══════════════════════════════════════════════════════════════════════════════

-- Main config table
CREATE TABLE IF NOT EXISTS system_config (
    key           TEXT PRIMARY KEY,          -- e.g. "renko.brick_size"
    value         JSONB NOT NULL,            -- typed value (number, string, bool, array)
    value_type    TEXT NOT NULL DEFAULT 'float',  -- "float" | "int" | "bool" | "str" | "json"
    category      TEXT NOT NULL DEFAULT 'general', -- "renko" | "risk" | "regime" | "sizing" | "execution" | "stream" | "auth" | "general"
    description   TEXT NOT NULL DEFAULT '',  -- human-readable explanation
    default_value JSONB,                     -- the hardcoded default (for reference/reset)
    env_var       TEXT,                      -- corresponding env var name (e.g. "RENKO_BRICK_SIZE")
    min_value     JSONB,                     -- optional minimum for numeric types
    max_value     JSONB,                     -- optional maximum for numeric types
    allowed_values JSONB,                    -- optional enum list (e.g. ["fixed","atr","dynamic"])
    is_sensitive  BOOLEAN NOT NULL DEFAULT FALSE, -- hide value from non-admin reads
    requires_restart BOOLEAN NOT NULL DEFAULT FALSE, -- if true, change needs server restart
    updated_by    TEXT NOT NULL DEFAULT 'system',   -- who made the change
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Config change audit log
CREATE TABLE IF NOT EXISTS system_config_audit (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key         TEXT NOT NULL,
    old_value   JSONB,
    new_value   JSONB,
    changed_by  TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason      TEXT                     -- optional change reason
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_system_config_category ON system_config (category);
CREATE INDEX IF NOT EXISTS idx_system_config_audit_key ON system_config_audit (key);
CREATE INDEX IF NOT EXISTS idx_system_config_audit_time ON system_config_audit (changed_at DESC);

-- Enable RLS
ALTER TABLE system_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_config_audit ENABLE ROW LEVEL SECURITY;

-- RLS policies: authenticated users can read, only admins can mutate
CREATE POLICY "Authenticated users can read config"
    ON system_config FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "Service role full access to config"
    ON system_config FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

CREATE POLICY "Authenticated users can read config audit"
    ON system_config_audit FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "Service role full access to config audit"
    ON system_config_audit FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- Trigger: auto-update updated_at on row change
CREATE OR REPLACE FUNCTION update_system_config_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_system_config_updated_at
    BEFORE UPDATE ON system_config
    FOR EACH ROW
    EXECUTE FUNCTION update_system_config_updated_at();

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Renko configuration defaults
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
-- Brick Engine
('renko.brick_size',           '0.50',  'float', 'renko', 'Fixed dollar brick size',                 '0.50',  'RENKO_BRICK_SIZE',           '0.01',  '100.0'),
('renko.brick_size_mode',      '"fixed"','str',  'renko', 'Brick sizing mode: fixed|atr|dynamic',    '"fixed"','RENKO_BRICK_SIZE_MODE',       NULL,    NULL),
('renko.atr_period',           '14',    'int',   'renko', 'ATR lookback period',                     '14',    'RENKO_ATR_PERIOD',           '2',     '100'),
('renko.atr_multiplier',       '1.0',   'float', 'renko', 'ATR multiplier for brick size',           '1.0',   'RENKO_ATR_MULTIPLIER',       '0.1',   '10.0'),
('renko.reversal_bricks',      '2',     'int',   'renko', 'Bricks for reversal',                     '2',     'RENKO_REVERSAL_BRICKS',      '1',     '5'),
-- Swing Classifier
('renko.swing_lookback',       '3',     'int',   'renko', 'Bricks to confirm a swing point',         '3',     'RENKO_SWING_LOOKBACK',       '1',     '20'),
('renko.min_swing_distance',   '2',     'int',   'renko', 'Min bricks between swing highs/lows',     '2',     'RENKO_MIN_SWING_DISTANCE',   '1',     '10'),
-- Pattern Detector
('renko.bull_trigger_n',       '3',     'int',   'renko', 'N consecutive HH/HL for bull trigger',    '3',     'RENKO_BULL_TRIGGER_N',       '1',     '10'),
('renko.bear_trigger_n',       '3',     'int',   'renko', 'N consecutive LL/LH for bear trigger',    '3',     'RENKO_BEAR_TRIGGER_N',       '1',     '10'),
('renko.double_top_bricks',    '6',     'int',   'renko', 'Lookback for double-top/bottom patterns', '6',     'RENKO_DOUBLE_TOP_BRICKS',    '2',     '20'),
('renko.consolidation_max_mix','0.4',   'float', 'renko', 'Max ratio of opposite labels in a run',   '0.4',   'RENKO_CONSOLIDATION_MAX_MIX','0.0',   '1.0'),
-- Signal Filter / Session
('renko.session_start',        '"09:35"','str',  'renko', 'Session start time (ET)',                 '"09:35"','RENKO_SESSION_START',         NULL,    NULL),
('renko.session_end',          '"15:45"','str',  'renko', 'Session end time (ET)',                   '"15:45"','RENKO_SESSION_END',           NULL,    NULL),
('renko.skip_lunch',           'true',  'bool',  'renko', 'Skip 11:30-13:00 ET lunch period',       'true',  'RENKO_SKIP_LUNCH',           NULL,    NULL),
('renko.lunch_start',          '"11:30"','str',  'renko', 'Lunch period start (ET)',                 '"11:30"','RENKO_LUNCH_START',           NULL,    NULL),
('renko.lunch_end',            '"13:00"','str',  'renko', 'Lunch period end (ET)',                   '"13:00"','RENKO_LUNCH_END',             NULL,    NULL),
('renko.max_trades_per_session','15',   'int',   'renko', 'Hard cap on daily trades',                '15',    'RENKO_MAX_TRADES_PER_SESSION','1',     '100'),
('renko.max_daily_loss_bricks','10.0',  'float', 'renko', 'Stop trading after N bricks lost',        '10.0',  'RENKO_MAX_DAILY_LOSS_BRICKS','1.0',   '100.0'),
('renko.max_consecutive_losses','3',    'int',   'renko', 'Pause after N consecutive losses',        '3',     'RENKO_MAX_CONSECUTIVE_LOSSES','1',     '20'),
('renko.cooldown_seconds',    '30.0',   'float', 'renko', 'Min seconds between trades',              '30.0',  'RENKO_COOLDOWN_SECONDS',     '0.0',   '300.0'),
('renko.regime_gate',         'true',   'bool',  'renko', 'Only trade with HMM regime alignment',    'true',  'RENKO_REGIME_GATE',          NULL,    NULL),
-- Risk Manager
('renko.sl_bricks',           '3',      'int',   'renko', 'Stop-loss in brick units',                '3',     'RENKO_SL_BRICKS',            '1',     '20'),
('renko.tp_bricks',           '5',      'int',   'renko', 'Take-profit in brick units',              '5',     'RENKO_TP_BRICKS',            '1',     '50'),
('renko.trailing_stop',       'true',   'bool',  'renko', 'Enable trailing stop',                    'true',  'RENKO_TRAILING_STOP',        NULL,    NULL),
('renko.trail_after_bricks',  '3',      'int',   'renko', 'Start trailing after N bricks profit',    '3',     'RENKO_TRAIL_AFTER_BRICKS',   '1',     '20'),
('renko.trail_distance_bricks','2',     'int',   'renko', 'Trail by N bricks behind peak',           '2',     'RENKO_TRAIL_DISTANCE_BRICKS','1',     '10'),
('renko.time_stop_bricks',    '10',     'int',   'renko', 'Close if open N+ bricks without TP/SL',   '10',    'RENKO_TIME_STOP_BRICKS',     '1',     '50'),
('renko.partial_exit_pct',    '0.0',    'float', 'renko', 'Partial exit fraction (0 = off)',         '0.0',   'RENKO_PARTIAL_EXIT_PCT',     '0.0',   '0.5'),
('renko.partial_exit_bricks', '3',      'int',   'renko', 'Bricks of profit for partial exit',       '3',     'RENKO_PARTIAL_EXIT_BRICKS',  '1',     '20'),
-- Position Sizing
('renko.kelly_fraction',      '0.5',    'float', 'renko', 'Half-Kelly by default',                   '0.5',   'RENKO_KELLY_FRACTION',       '0.1',   '1.0'),
('renko.max_position_pct',    '0.10',   'float', 'renko', 'Max % of equity per trade',               '0.10',  'RENKO_MAX_POSITION_PCT',     '0.01',  '1.0'),
('renko.min_position_usd',    '50.0',   'float', 'renko', 'Minimum order value in USD',              '50.0',  'RENKO_MIN_POSITION_USD',     '0.0',   '10000.0'),
('renko.default_win_rate',    '0.55',   'float', 'renko', 'Fallback WR for Kelly when no history',   '0.55',  'RENKO_DEFAULT_WIN_RATE',     '0.01',  '0.99'),
-- Transaction Costs
('renko.slippage_bps',        '2.0',    'float', 'renko', 'Slippage in basis points',                '2.0',   'RENKO_SLIPPAGE_BPS',         '0.0',   '50.0'),
('renko.commission_bps',      '5.0',    'float', 'renko', 'Commission in basis points',              '5.0',   'RENKO_COMMISSION_BPS',       '0.0',   '100.0'),
('renko.spread_bps',          '1.0',    'float', 'renko', 'Bid-ask spread in basis points',          '1.0',   'RENKO_SPREAD_BPS',           '0.0',   '50.0'),
-- Execution Modeling
('renko.oco_priority',        '"sl_first"','str', 'renko', 'SL+TP priority: sl_first|tp_first',      '"sl_first"','RENKO_OCO_PRIORITY',        NULL,    NULL),
('renko.market_impact_mode',  '"none"','str',    'renko', 'Market impact: none|almgren_chriss',      '"none"','RENKO_MARKET_IMPACT_MODE',    NULL,    NULL),
('renko.adv_shares',          '10000000','int',  'renko', 'Average daily volume in shares',          '10000000','RENKO_ADV_SHARES',           '100000','1000000000'),
('renko.fill_probability_mode','"always_fill"','str','renko','Fill mode: always_fill|realistic',     '"always_fill"','RENKO_FILL_PROBABILITY_MODE',NULL, NULL),
('renko.borrow_rate_bps',     '50.0',   'float', 'renko', 'Annualized short borrow cost (bps)',      '50.0',  'RENKO_BORROW_RATE_BPS',      '0.0',   '5000.0'),
('renko.margin_rate_bps',     '150.0',  'float', 'renko', 'Annualized margin rate (bps)',            '150.0', 'RENKO_MARGIN_RATE_BPS',      '0.0',   '5000.0'),
('renko.is_hard_to_borrow',   'false',  'bool',  'renko', 'Whether stock is hard-to-borrow',        'false', 'RENKO_IS_HARD_TO_BORROW',    NULL,    NULL),
('renko.dividend_yield_bps',  '200.0',  'float', 'renko', 'Annualized dividend yield (bps)',         '200.0', 'RENKO_DIVIDEND_YIELD_BPS',   '0.0',   '5000.0'),
('renko.initial_capital',     '100000.0','float','renko',  'Starting capital for dollar P&L',         '100000.0','RENKO_INITIAL_CAPITAL',      '1000.0','100000000.0'),
('renko.confidence_level',    '0.95',   'float', 'renko', 'Statistical confidence level',            '0.95',  'RENKO_CONFIDENCE_LEVEL',     '0.80',  '0.99'),
-- Pipeline
('renko.default_symbol',      '"SPY"',  'str',   'renko', 'Default trading symbol',                  '"SPY"','RENKO_DEFAULT_SYMBOL',        NULL,    NULL),
('renko.timezone',            '"America/New_York"','str','renko','Timezone for session filters',    '"America/New_York"','RENKO_TIMEZONE',     NULL,    NULL),
-- Optimization
('renko.optimize_brick_sizes','[0.25, 0.50, 1.00]','json','renko','Brick sizes for optimization sweep','[0.25, 0.50, 1.00]',NULL,NULL,NULL),
('renko.optimize_sl_range',   '[2, 3, 4]','json','renko', 'SL bricks range for optimization',       '[2, 3, 4]',NULL,NULL,NULL),
('renko.optimize_tp_range',   '[4, 5, 6]','json','renko', 'TP bricks range for optimization',       '[4, 5, 6]',NULL,NULL,NULL),
('renko.multiple_testing_alpha','0.05', 'float', 'renko', 'Significance threshold for multi-test',  '0.05',  'RENKO_MULTIPLE_TESTING_ALPHA','0.01',  '0.10'),
-- Backend
('renko.snapshot_interval',   '100',    'int',   'renko', 'Save snapshot every Nth tick',            '100',   'RENKO_SNAPSHOT_INTERVAL',    '10',    '1000'),
('renko.backtest_chunk_size', '150',    'int',   'renko', 'Ticks per SSE chunk in streaming',        '150',   'RENKO_BACKTEST_CHUNK_SIZE',  '10',    '1000'),
('renko.loss_alert_bricks',   '-5',     'int',   'renko', 'Brick loss threshold for Discord alert',  '-5',    'RENKO_LOSS_ALERT_BRICKS',    '-20',   '0'),
('renko.batch_notify_min_ticks','50',   'int',   'renko', 'Min ticks for batch Discord notification','50',    'RENKO_BATCH_NOTIFY_MIN_TICKS','1',     '1000')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Position Sizing (Masaniello) configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('sizing.base_risk',       '0.005', 'float', 'sizing', 'Base risk fraction (beta in Masaniello formula)','0.005','SIZING_BASE_RISK',     '0.001',  '0.05'),
('sizing.min_risk',        '0.0025','float', 'sizing', 'Hard floor for risk fraction',                   '0.0025','SIZING_MIN_RISK',      '0.0001', '0.01'),
('sizing.max_risk',        '0.010', 'float', 'sizing', 'Hard cap for risk fraction',                     '0.010', 'SIZING_MAX_RISK',      '0.001',  '0.05'),
('sizing.min_prob',        '0.50',  'float', 'sizing', 'Minimum win probability to trade',               '0.50',  'SIZING_MIN_PROB',      '0.30',   '0.80'),
('sizing.min_rr',          '2.50',  'float', 'sizing', 'Minimum reward/risk ratio to trade',             '2.50',  'SIZING_MIN_REWARD_RISK','1.0',   '10.0'),
('sizing.max_drawdown',    '0.10',  'float', 'sizing', 'Max strategy DD for DD scaling',                 '0.10',  'SIZING_MAX_DRAWDOWN',  '0.01',   '0.50'),
('sizing.batch_halt_dd',   '0.05',  'float', 'sizing', 'Halt batch at -N intraday drawdown',             '0.05',  'SIZING_BATCH_HALT_DD', '0.01',   '0.20'),
('sizing.regime_floor',    '0.50',  'float', 'sizing', 'Min regime quality to allow trade',              '0.50',  'SIZING_REGIME_FLOOR',  '0.10',   '1.0'),
('sizing.use_kelly_overlay','false','bool',  'sizing', 'Enable Kelly cap overlay',                       'false', 'SIZING_USE_KELLY_OVERLAY',NULL,   NULL),
('sizing.kelly_fraction',  '0.25',  'float', 'sizing', 'Kelly fraction when overlay active',             '0.25',  'SIZING_KELLY_FRACTION','0.05',   '1.0'),
('sizing.batch_size',      '5',     'int',   'sizing', 'N trades per Masaniello batch',                  '5',     'SIZING_BATCH_SIZE',    '2',      '20'),
('sizing.target_wins',     '3',     'int',   'sizing', 'Target wins per batch',                          '3',     'SIZING_TARGET_WINS',   '1',      '20'),
('sizing.mc_simulations',  '1000',  'int',   'sizing', 'Monte Carlo simulation count',                   '1000',  'SIZING_MC_SIMULATIONS','100',    '10000')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Risk analysis configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('risk.annualise_factor',   '252',   'int',   'risk',   'Trading days per year for annualization',        '252',  'RISK_ANNUALISE_FACTOR','200',    '260'),
('risk.base_risk_limit',    '0.02',  'float', 'risk',   'Default daily loss limit (2%)',                  '0.02', 'RISK_BASE_RISK_LIMIT', '0.005',  '0.10'),
('risk.stop_cvar_multiplier','2',    'int',   'risk',   'Stop = N x CVaR95',                             '2',    'RISK_STOP_CVAR_MULTIPLIER','1',  '5'),
('risk.tp_cvar_multiplier', '3',     'int',   'risk',   'TP = N x CVaR95 (3:1 R:R)',                     '3',    'RISK_TP_CVAR_MULTIPLIER','1',    '10'),
('risk.stress_crash_mean',  '-0.02', 'float', 'risk',   '2008 crash simulation mean return',             '-0.02','RISK_STRESS_CRASH_MEAN','-0.10', '0.0'),
('risk.stress_crash_std',   '0.04',  'float', 'risk',   '2008 crash simulation std',                     '0.04', 'RISK_STRESS_CRASH_STD', '0.01',  '0.20'),
('risk.stress_crash_seed',  '42',    'int',   'risk',   'Stress test RNG seed',                          '42',   'RISK_STRESS_CRASH_SEED',NULL,    NULL),
('risk.stress_flash_drop',  '-0.10', 'float', 'risk',   'Flash crash single-day drop',                  '-0.10','RISK_STRESS_FLASH_DROP','-0.50', '-0.01'),
('risk.stress_vol_multiplier','3',   'int',   'risk',   'Vol spike factor',                              '3',    'RISK_STRESS_VOL_MULTIPLIER','1',  '10'),
('risk.stress_vol_spike_bars','20',  'int',   'risk',   'Vol spike duration in bars',                    '20',   'RISK_STRESS_VOL_SPIKE_BARS','5',  '100'),
('risk.stress_rate_shock_bps','0.002','float','risk',   'Rate shock daily shift',                        '0.002','RISK_STRESS_RATE_SHOCK_BPS','0.0001','0.01'),
('risk.stress_liquidity_shift','0.001','float','risk',  'Liquidity crisis extra cost',                   '0.001','RISK_STRESS_LIQUIDITY_SHIFT','0.0','0.01')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Regime engine configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('regime.vol_window_long',  '20',    'int',   'regime', 'Long vol lookback window',                      '20',   'REGIME_VOL_FEATURE_WINDOW_LONG','5',  '100'),
('regime.vol_window_short', '5',     'int',   'regime', 'Short vol lookback window',                     '5',    'REGIME_VOL_FEATURE_WINDOW_SHORT','2', '20'),
('regime.trend_window_short','10',   'int',   'regime', 'Short momentum window',                         '10',   'REGIME_TREND_FEATURE_WINDOW_SHORT','2','50'),
('regime.trend_window_long', '30',   'int',   'regime', 'Long momentum window',                          '30',   'REGIME_TREND_FEATURE_WINDOW_LONG','10','200'),
('regime.hmm_random_state', '42',    'int',   'regime', 'HMM reproducibility seed',                      '42',   'REGIME_HMM_RANDOM_STATE', NULL,    NULL),
('regime.hmm_n_iter',       '100',   'int',   'regime', 'HMM EM iterations',                             '100',  'REGIME_HMM_N_ITER',     '10',     '1000'),
('regime.stability_lookback','20',   'int',   'regime', 'Regime stability lookback bars',                '20',   'REGIME_STABILITY_LOOKBACK','5',   '100')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Execution modeling configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('exec.sigma_daily',              '0.02',  'float', 'execution', 'Default daily volatility assumption',    '0.02',  'EXEC_DEFAULT_SIGMA_DAILY',       '0.001','0.10'),
('exec.permanent_impact_coeff',   '0.1',   'float', 'execution', 'Almgren-Chriss eta coefficient',        '0.1',   'EXEC_PERMANENT_IMPACT_COEFF',    '0.01', '1.0'),
('exec.temporary_impact_coeff',   '0.142', 'float', 'execution', 'Almgren-Chriss epsilon coefficient',    '0.142', 'EXEC_TEMPORARY_IMPACT_COEFF',    '0.01', '1.0'),
('exec.default_adv_shares',       '10000000','int', 'execution', 'Default average daily volume',          '10000000','EXEC_DEFAULT_ADV_SHARES',       '100000','1000000000'),
('exec.default_avg_price',        '450.0', 'float', 'execution', 'Default average stock price',           '450.0', 'EXEC_DEFAULT_AVG_PRICE',         '1.0',   '10000.0'),
('exec.fill_time_horizon_hours',  '6.5',   'float', 'execution', 'Trading day hours for fill prob',       '6.5',   'EXEC_FILL_TIME_HORIZON_HOURS',   '1.0',   '24.0'),
('exec.fill_sensitivity',         '5.0',   'float', 'execution', 'Logit fill sensitivity k',              '5.0',   'EXEC_FILL_SENSITIVITY',          '0.1',   '20.0'),
('exec.fill_threshold',           '1.0',   'float', 'execution', 'Logit fill threshold',                  '1.0',   'EXEC_FILL_THRESHOLD',            '0.0',   '5.0'),
('exec.htb_premium_bps',          '500.0', 'float', 'execution', 'Hard-to-borrow premium (bps)',          '500.0', 'EXEC_HTB_PREMIUM_BPS',           '0.0',   '5000.0'),
('exec.trading_days_per_year',    '252',   'int',   'execution', 'Trading days per year',                 '252',   'EXEC_TRADING_DAYS_PER_YEAR',     '200',   '260')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Stream / WebSocket configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('stream.min_prices_for_fit', '81',     'int',   'stream', 'Min prices for HMM fit',               '81',   'STREAM_MIN_PRICES_FOR_FIT','20',   '500'),
('stream.window',             '500',    'int',   'stream', 'Price buffer max length',               '500',  'STREAM_DEFAULT_WINDOW',    '50',    '5000'),
('stream.kelly_fraction',     '0.5',    'float', 'stream', 'Default Kelly fraction',                '0.5',  'STREAM_DEFAULT_KELLY_FRACTION','0.1','1.0'),
('stream.target_vol',         '0.15',   'float', 'stream', 'Default target volatility',             '0.15', 'STREAM_DEFAULT_TARGET_VOL','0.05', '0.50'),
('stream.base_risk_limit',    '0.02',   'float', 'stream', 'Default risk limit',                    '0.02', 'STREAM_DEFAULT_BASE_RISK_LIMIT','0.005','0.10'),
('stream.refit_every',        '50',     'int',   'stream', 'HMM refit frequency (ticks)',           '50',   'STREAM_DEFAULT_REFIT_EVERY','10',   '200'),
('stream.regime_debounce',    '3',      'int',   'stream', 'Regime change debounce bars',           '3',    'STREAM_REGIME_DEBOUNCE_BARS','1',   '10'),
('stream.subscribe_timeout',  '30',     'int',   'stream', 'WS subscribe timeout (seconds)',        '30',   'STREAM_WS_SUBSCRIBE_TIMEOUT','5',   '120'),
('stream.sse_heartbeat_timeout','20',   'int',   'stream', 'SSE heartbeat timeout (seconds)',       '20',   'STREAM_SSE_PNL_TIMEOUT',   '5',     '60'),
('stream.alert_queue_size',   '200',    'int',   'stream', 'SSE alert queue size',                  '200',  'STREAM_SSE_ALERT_QUEUE_SIZE','10',  '1000')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Alpaca connection configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('alpaca.ws_max_reconnects',   '20',   'int',   'alpaca', 'Max WS reconnection attempts',          '20',  'ALPACA_WS_MAX_RECONNECTS',   '1',     '100'),
('alpaca.ws_base_backoff',     '1.0',  'float', 'alpaca', 'WS reconnection backoff base (seconds)','1.0', 'ALPACA_WS_BASE_BACKOFF',     '0.1',   '10.0'),
('alpaca.ws_ping_interval',    '30',   'int',   'alpaca', 'WS keepalive ping interval (seconds)',  '30',  'ALPACA_WS_PING_INTERVAL',    '5',     '120'),
('alpaca.ws_ping_timeout',     '10',   'int',   'alpaca', 'WS ping timeout (seconds)',             '10',  'ALPACA_WS_PING_TIMEOUT',     '1',     '30'),
('alpaca.snapshot_interval_sec','5',   'int',   'alpaca', 'P&L snapshot frequency (seconds)',      '5',   'ALPACA_SNAPSHOT_INTERVAL_SEC','1',     '60'),
('alpaca.stream_grace_period', '30',   'int',   'alpaca', 'Consumer disconnect grace (seconds)',   '30',  'ALPACA_STREAM_GRACE_PERIOD', '5',     '120'),
('alpaca.sse_queue_size',      '500',  'int',   'alpaca', 'Per-consumer SSE queue size',           '500', 'ALPACA_SSE_QUEUE_SIZE',      '50',    '5000')
ON CONFLICT (key) DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- Seed: Auth / infrastructure configuration
-- ══════════════════════════════════════════════════════════════════════════════

INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value) VALUES
('auth.role_cache_ttl',        '300',   'int',   'auth',   'Clerk role cache TTL (seconds)',        '300', 'CLERK_ROLE_CACHE_TTL',        '10',    '3600'),
('auth.jwks_cache_ttl',        '3600',  'int',   'auth',   'JWKS cache TTL (seconds)',              '3600','CLERK_JWKS_CACHE_TTL',        '60',    '86400'),
('auth.circuit_failure_threshold','3',  'int',   'auth',   'Auth circuit breaker failure threshold','3',   'CLERK_CIRCUIT_FAILURE_THRESHOLD','1',  '20'),
('auth.circuit_reset_timeout',  '60.0', 'float', 'auth',   'Auth circuit breaker reset (seconds)', '60.0', 'CLERK_CIRCUIT_RESET_TIMEOUT', '5.0',   '300.0'),
('auth.enrich_cache_ttl',       '300',  'int',   'auth',   'User enrichment cache TTL (seconds)',  '300', 'CLERK_ENRICH_CACHE_TTL',      '10',    '3600'),
('auth.api_key_cache_ttl',      '60',   'int',   'auth',   'SaaS API key cache TTL (seconds)',     '60',  'AUTH_API_KEY_CACHE_TTL',      '10',    '3600')
ON CONFLICT (key) DO NOTHING;
