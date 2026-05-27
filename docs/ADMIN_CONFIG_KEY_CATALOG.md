# Admin Configuration Key Catalog

**Version:** v7.0.0
**Last Updated:** 2026-05-24
**Total Keys:** 148 across 8 categories

---

## Overview

This document is the complete reference for every runtime-configurable parameter in the Noble Trader platform. Each key is stored in the Supabase `system_config` table, has a type, range constraints, a default value, and a human-readable description. Admins can modify any of these values through the Admin Configuration Panel without redeploying the backend.

Values take effect within 30 seconds (TTL cache) across all backend services. Use the "Reload from Database" button for immediate propagation.

---

## Category Summary

| Category | Key Prefix | Count | Section |
|----------|-----------|-------|---------|
| Renko | `renko.*` | 56 | [Renko](#1-renko-56-keys) |
| Execution | `exec.*` | 23 | [Execution](#2-execution-23-keys) |
| Alpaca | `alpaca.*` | 17 | [Alpaca](#3-alpaca-17-keys) |
| Risk | `risk.*` | 15 | [Risk](#4-risk-15-keys) |
| Sizing | `sizing.*` | 13 | [Sizing](#5-sizing-13-keys) |
| Stream | `stream.*` | 11 | [Stream](#6-stream-11-keys) |
| Auth | `auth.*` | 6 | [Auth](#7-auth-6-keys) |
| Regime | `regime.*` | 7 | [Regime](#8-regime-7-keys) |

---

## 1. Renko (56 keys)

### Brick Engine (5 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.brick_size` | float | 0.50 | [0.01, 100.0] | Fixed dollar brick size - the threshold for Renko brick construction |
| `renko.brick_size_mode` | str | "fixed" | allowed: ["fixed", "atr", "dynamic"] | Brick sizing mode: fixed uses a static dollar value, atr derives from Average True Range, dynamic adjusts based on volatility regime |
| `renko.atr_period` | int | 14 | [2, 100] | ATR lookback period for computing average true range when brick_size_mode is "atr" or "dynamic" |
| `renko.atr_multiplier` | float | 1.0 | [0.1, 10.0] | ATR multiplier for brick size - scales the ATR value to set the brick threshold |
| `renko.reversal_bricks` | int | 2 | [1, 5] | Number of bricks required for a reversal - a new brick in the opposite direction must exceed this threshold |

### Swing Classifier (2 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.swing_lookback` | int | 3 | [1, 20] | Number of consecutive bricks to confirm a swing point (higher values filter noise) |
| `renko.min_swing_distance` | int | 2 | [1, 10] | Minimum brick distance between swing highs and swing lows - prevents labeling minor fluctuations |

### Pattern Detector (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.bull_trigger_n` | int | 3 | [1, 10] | Number of consecutive HH/HL bricks required to trigger a bull pattern signal |
| `renko.bear_trigger_n` | int | 3 | [1, 10] | Number of consecutive LL/LH bricks required to trigger a bear pattern signal |
| `renko.double_top_bricks` | int | 6 | [2, 20] | Lookback window for detecting double-top and double-bottom reversal patterns |
| `renko.consolidation_max_mix` | float | 0.4 | [0.0, 1.0] | Maximum ratio of opposite-direction labels in a run before it is classified as consolidation (0.0 = pure run required, 1.0 = any mix allowed) |

### Session & Signal Filter (9 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.session_start` | str | "09:35" | - | Session start time in Eastern Time - signals before this time are filtered out |
| `renko.session_end` | str | "15:45" | - | Session end time in Eastern Time - signals after this time are filtered out |
| `renko.skip_lunch` | bool | true | - | Skip the 11:30-13:00 ET lunch period - reduces false signals during low-liquidity midday |
| `renko.lunch_start` | str | "11:30" | - | Lunch period start time in ET (only used when skip_lunch is true) |
| `renko.lunch_end` | str | "13:00" | - | Lunch period end time in ET (only used when skip_lunch is true) |
| `renko.max_trades_per_session` | int | 15 | [1, 100] | Hard cap on the number of trades per session - prevents overtrading |
| `renko.max_daily_loss_bricks` | float | 10.0 | [1.0, 100.0] | Stop trading after N bricks of cumulative loss - circuit breaker for drawdown control |
| `renko.max_consecutive_losses` | int | 3 | [1, 20] | Pause trading after N consecutive losing trades - cooldown mechanism for cold streaks |
| `renko.cooldown_seconds` | float | 30.0 | [0.0, 300.0] | Minimum seconds between trades - prevents rapid-fire entries on volatile data |
| `renko.regime_gate` | bool | true | - | Only allow trades when HMM regime aligns with signal direction - primary risk filter |

### Risk Manager (7 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.sl_bricks` | int | 3 | [1, 20] | Stop-loss distance in brick units - exit if price moves N bricks against position |
| `renko.tp_bricks` | int | 5 | [1, 50] | Take-profit distance in brick units - exit if price moves N bricks in favorable direction |
| `renko.trailing_stop` | bool | true | - | Enable trailing stop that moves the stop-loss up as the position moves into profit |
| `renko.trail_after_bricks` | int | 3 | [1, 20] | Start trailing after N bricks of profit - prevents premature trailing on small moves |
| `renko.trail_distance_bricks` | int | 2 | [1, 10] | Trail by N bricks behind the peak profit level - tighter trail = more protection, less room |
| `renko.time_stop_bricks` | int | 10 | [1, 50] | Close position if open for N+ bricks without hitting TP or SL - prevents capital stagnation |
| `renko.partial_exit_pct` | float | 0.0 | [0.0, 0.5] | Fraction of position to exit at partial_exit_bricks profit (0.0 = no partial exit) |
| `renko.partial_exit_bricks` | int | 3 | [1, 20] | Number of profit bricks at which to execute partial exit - locks in some profit early |

### Position Sizing (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.kelly_fraction` | float | 0.5 | [0.1, 1.0] | Fractional Kelly multiplier for Renko position sizing (0.5 = half-Kelly, conservative) |
| `renko.max_position_pct` | float | 0.10 | [0.01, 1.0] | Maximum percentage of equity per trade - hard cap prevents oversized positions |
| `renko.min_position_usd` | float | 50.0 | [0.0, 10000.0] | Minimum order value in USD - avoids positions too small to be meaningful |
| `renko.default_win_rate` | float | 0.55 | [0.01, 0.99] | Fallback win rate for Kelly sizing when no trade history is available |

### Transaction Costs (3 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.slippage_bps` | float | 2.0 | [0.0, 50.0] | Slippage in basis points - models the difference between expected and actual fill price |
| `renko.commission_bps` | float | 5.0 | [0.0, 100.0] | Commission in basis points - broker fee per trade |
| `renko.spread_bps` | float | 1.0 | [0.0, 50.0] | Bid-ask spread in basis points - cost of crossing the spread on market orders |

### Execution Modeling (7 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.oco_priority` | str | "sl_first" | allowed: ["sl_first", "tp_first"] | OCO (One-Cancels-Other) priority: which leg to submit first for bracket orders |
| `renko.market_impact_mode` | str | "none" | allowed: ["none", "almgren_chriss"] | Market impact model: none ignores impact, almgren_chriss applies the Almgren-Chriss model |
| `renko.adv_shares` | int | 10000000 | [100000, 1000000000] | Average daily volume in shares - used by market impact model to estimate price impact |
| `renko.fill_probability_mode` | str | "always_fill" | allowed: ["always_fill", "realistic"] | Fill probability model: always_fill assumes 100% fill, realistic models adverse selection |
| `renko.borrow_rate_bps` | float | 50.0 | [0.0, 5000.0] | Annualized short borrow cost in basis points - fee for borrowing shares to sell short |
| `renko.margin_rate_bps` | float | 150.0 | [0.0, 5000.0] | Annualized margin interest rate in basis points - cost of leveraged positions |
| `renko.is_hard_to_borrow` | bool | false | - | Whether the stock is hard-to-borrow - increases borrow cost and may limit short availability |
| `renko.dividend_yield_bps` | float | 200.0 | [0.0, 5000.0] | Annualized dividend yield in basis points - reduces short position cost (short seller receives dividends) |
| `renko.initial_capital` | float | 100000.0 | [1000.0, 100000000.0] | Starting capital for dollar P&L computation - sets the baseline equity for returns |
| `renko.confidence_level` | float | 0.95 | [0.80, 0.99] | Statistical confidence level for VaR/CVaR calculations (0.95 = 95%) |

### Pipeline (2 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.default_symbol` | str | "SPY" | - | Default trading symbol when none is specified |
| `renko.timezone` | str | "America/New_York" | - | Timezone for session filter calculations - all session times are interpreted in this timezone |

### Optimization (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.optimize_brick_sizes` | json | [0.25, 0.50, 1.00] | - | List of brick sizes to sweep during optimization runs |
| `renko.optimize_sl_range` | json | [2, 3, 4] | - | List of stop-loss brick values to sweep during optimization runs |
| `renko.optimize_tp_range` | json | [4, 5, 6] | - | List of take-profit brick values to sweep during optimization runs |
| `renko.multiple_testing_alpha` | float | 0.05 | [0.01, 0.10] | Significance threshold for multiple testing correction - controls false discovery rate |

### Backend (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `renko.snapshot_interval` | int | 100 | [10, 1000] | Save pipeline snapshot every Nth tick - controls persistence frequency |
| `renko.backtest_chunk_size` | int | 150 | [10, 1000] | Ticks per SSE chunk in streaming backtest - controls progress update granularity |
| `renko.loss_alert_bricks` | int | -5 | [-20, 0] | Brick loss threshold for Discord alert - fires when cumulative brick P&L drops below this level |
| `renko.batch_notify_min_ticks` | int | 50 | [1, 1000] | Minimum ticks processed before sending batch Discord notification - reduces notification spam |

---

## 2. Execution (23 keys)

### Core Market Impact (5 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `exec.sigma_daily` | float | 0.02 | [0.001, 0.10] | Default daily volatility assumption for Almgren-Chriss impact model |
| `exec.permanent_impact_coeff` | float | 0.1 | [0.01, 1.0] | Almgren-Chriss eta coefficient - permanent price impact per unit of participation |
| `exec.temporary_impact_coeff` | float | 0.142 | [0.01, 1.0] | Almgren-Chriss epsilon coefficient - temporary price impact that decays over time |
| `exec.default_adv_shares` | int | 10000000 | [100000, 1000000000] | Default average daily volume in shares - fallback when symbol-specific ADV is unknown |
| `exec.default_avg_price` | float | 450.0 | [1.0, 10000.0] | Default average stock price - used in impact calculations when real price is unavailable |

### Fill Probability (5 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `exec.fill_time_horizon_hours` | float | 6.5 | [1.0, 24.0] | Trading day hours for fill probability calculation - models the time window for execution |
| `exec.fill_sensitivity` | float | 5.0 | [0.1, 20.0] | Logit fill sensitivity coefficient k - higher values make fill probability more binary |
| `exec.fill_threshold` | float | 1.0 | [0.0, 5.0] | Logit fill threshold - the participation rate at which fill probability is 50% |
| `exec.htb_premium_bps` | float | 500.0 | [0.0, 5000.0] | Hard-to-borrow premium in basis points - additional cost for HTB stocks on top of borrow rate |
| `exec.trading_days_per_year` | int | 252 | [200, 260] | Trading days per year for annualizing execution costs |

### Financing (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `exec.borrow_rate_bps` | float | 50.0 | [0.0, 5000.0] | Annualized short borrow cost in basis points - base rate for borrowing shares |
| `exec.margin_rate_bps` | float | 150.0 | [0.0, 5000.0] | Annualized margin interest rate in basis points - cost of holding leveraged positions |
| `exec.dividend_yield_bps` | float | 200.0 | [0.0, 5000.0] | Annualized dividend yield for short cost calculation - shorts pay dividends to lenders |
| `exec.trading_days_per_quarter` | int | 63 | [50, 70] | Trading days per quarter - used for dividend probability and quarterly cost projection |

### Fill Probability Internals (9 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `exec.default_participation_rate` | float | 0.01 | [0.001, 0.10] | Default volume participation rate - fraction of daily volume the order is expected to consume |
| `exec.volume_penalty_multiplier` | float | 10.0 | [1.0, 50.0] | Volume penalty scale factor - increases cost when participation exceeds normal levels |
| `exec.max_volume_penalty` | float | 0.3 | [0.05, 0.80] | Maximum volume penalty fraction - caps the penalty to prevent unrealistic cost estimates |
| `exec.stop_min_fill_probability` | float | 0.90 | [0.50, 1.0] | Minimum fill probability for stop orders - stops with lower probability are flagged as risky |
| `exec.stop_adverse_sigma` | float | 0.5 | [0.1, 3.0] | Adverse selection sigma for stop orders - models the likelihood of stop running |
| `exec.limit_adverse_sigma` | float | 0.2 | [0.05, 2.0] | Adverse selection sigma for limit orders - models the cost of providing liquidity |
| `exec.fill_interp_high` | float | 0.8 | [0.5, 1.0] | High-fill interpretation threshold - fill probabilities above this are considered "likely filled" |
| `exec.fill_interp_mid` | float | 0.5 | [0.2, 0.8] | Mid-fill interpretation threshold - fill probabilities in this range are "partially filled" |
| `exec.fill_interp_low` | float | 0.2 | [0.05, 0.5] | Low-fill interpretation threshold - fill probabilities below this are "likely unfilled" |

---

## 3. Alpaca (17 keys)

### WebSocket Core (7 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `alpaca.ws_max_reconnects` | int | 20 | [1, 100] | Maximum WebSocket reconnection attempts before giving up - trades stop flowing after this limit |
| `alpaca.ws_base_backoff` | float | 1.0 | [0.1, 10.0] | WebSocket reconnection backoff base in seconds - exponential backoff: base * 2^attempt |
| `alpaca.ws_ping_interval` | int | 30 | [5, 120] | WebSocket keepalive ping interval in seconds - prevents connection timeout from inactivity |
| `alpaca.ws_ping_timeout` | int | 10 | [1, 30] | WebSocket ping timeout in seconds - if no pong received, connection is considered dead |
| `alpaca.snapshot_interval_sec` | int | 5 | [1, 60] | P&L snapshot frequency in seconds - controls how often portfolio state is persisted |
| `alpaca.stream_grace_period` | int | 30 | [5, 120] | Consumer disconnect grace period in seconds - allows temporary disconnects without session cleanup |
| `alpaca.sse_queue_size` | int | 500 | [50, 5000] | Per-consumer SSE queue size - larger buffers handle bursty data but use more memory |

### WebSocket Timeouts & Throttling (10 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `alpaca.ws_close_timeout` | int | 5 | [1, 30] | WebSocket close handshake timeout in seconds - how long to wait for clean close |
| `alpaca.ws_backoff_cap` | int | 8 | [2, 15] | Maximum reconnect backoff exponent cap - limits the maximum delay between reconnection attempts |
| `alpaca.supabase_http_timeout` | float | 10.0 | [1.0, 30.0] | Supabase credential lookup HTTP timeout in seconds - how long to wait for Alpaca API keys from DB |
| `alpaca.bootstrap_http_timeout` | float | 10.0 | [1.0, 30.0] | Bootstrap REST API HTTP timeout - timeout for initial account/position fetch on startup |
| `alpaca.bootstrap_throttle_timeout` | float | 10.0 | [1.0, 30.0] | Bootstrap throttle acquire timeout - max wait for rate-limited bootstrap operations |
| `alpaca.position_refresh_http_timeout` | float | 5.0 | [1.0, 15.0] | Position refresh REST API timeout - how long to wait for position data from Alpaca |
| `alpaca.position_refresh_throttle_timeout` | float | 5.0 | [1.0, 15.0] | Position refresh throttle timeout - max wait for rate-limited position refresh |
| `alpaca.data_no_position_wait_timeout` | float | 5.0 | [1.0, 30.0] | Wait timeout when no positions are held - controls how long to wait before rechecking |
| `alpaca.data_tick_min_interval` | float | 1.0 | [0.1, 10.0] | Minimum seconds between ticks per symbol - throttles high-frequency data to prevent overload |
| `alpaca.data_resubscribe_interval` | float | 2.0 | [0.5, 10.0] | Re-subscribe check interval in seconds - how often to verify WebSocket subscriptions are active |

---

## 4. Risk (15 keys)

### Core Risk (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `risk.annualise_factor` | int | 252 | [200, 260] | Trading days per year for annualizing volatility and return metrics |
| `risk.base_risk_limit` | float | 0.02 | [0.005, 0.10] | Default daily loss limit as a fraction (2% = 0.02) - maximum acceptable one-day loss |
| `risk.stop_cvar_multiplier` | int | 2 | [1, 5] | Stop-loss = N x CVaR95 - scales the stop-loss distance relative to tail risk |
| `risk.tp_cvar_multiplier` | int | 3 | [1, 10] | Take-profit = N x CVaR95 - sets the profit target at 3:1 reward-to-risk ratio by default |

### Stress Tests: 2008 Crash (3 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `risk.stress_crash_mean` | float | -0.02 | [-0.10, 0.0] | 2008 crash simulation mean daily return - models sustained negative returns |
| `risk.stress_crash_std` | float | 0.04 | [0.01, 0.20] | 2008 crash simulation daily standard deviation - models elevated volatility |
| `risk.stress_crash_seed` | int | 42 | - | Stress test RNG seed - ensures reproducible results across runs |

### Stress Tests: Flash Crash & Other (8 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `risk.stress_flash_drop` | float | -0.10 | [-0.50, -0.01] | Flash crash single-day drop as a fraction - models a sudden severe price decline |
| `risk.stress_vol_multiplier` | int | 3 | [1, 10] | Volatility spike factor - multiplies normal volatility to simulate a vol regime change |
| `risk.stress_vol_spike_bars` | int | 20 | [5, 100] | Duration of volatility spike in bars - how long the elevated vol persists |
| `risk.stress_rate_shock_bps` | float | 0.002 | [0.0001, 0.01] | Rate shock daily shift - models the impact of interest rate changes on equity returns |
| `risk.stress_liquidity_shift` | float | 0.001 | [0.0, 0.01] | Liquidity crisis extra cost - models wider spreads and higher transaction costs |
| `risk.stress_min_returns` | int | 20 | [5, 100] | Minimum number of returns required to run stress tests - ensures statistical validity |
| `risk.stress_liquidity_downside_mult` | float | 1.5 | [1.0, 5.0] | Liquidity crisis downside multiplier - amplifies losses during liquidity stress |
| `risk.stress_liquidity_upside_mult` | float | 0.8 | [0.1, 1.0] | Liquidity crisis upside multiplier - dampens gains during liquidity stress |

---

## 5. Sizing (13 keys)

### Masaniello Core (9 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `sizing.base_risk` | float | 0.005 | [0.001, 0.05] | Base risk fraction (beta in Masaniello formula) - the starting risk allocation per trade |
| `sizing.min_risk` | float | 0.0025 | [0.0001, 0.01] | Hard floor for risk fraction - prevents positions from becoming too small to be meaningful |
| `sizing.max_risk` | float | 0.010 | [0.001, 0.05] | Hard cap for risk fraction - prevents oversized positions even when all factors favor aggression |
| `sizing.min_prob` | float | 0.50 | [0.30, 0.80] | Minimum win probability to allow a trade - quality gate that blocks low-conviction setups |
| `sizing.min_rr` | float | 2.50 | [1.0, 10.0] | Minimum reward/risk ratio to allow a trade - ensures positive expected value |
| `sizing.max_drawdown` | float | 0.10 | [0.01, 0.50] | Maximum strategy drawdown for DD scaling - risk fraction reduced as drawdown approaches this limit |
| `sizing.batch_halt_dd` | float | 0.05 | [0.01, 0.20] | Halt batch at -N intraday drawdown - stops trading when losses exceed this threshold within a batch |
| `sizing.regime_floor` | float | 0.50 | [0.10, 1.0] | Minimum regime quality to allow trade - blocks entries when HMM regime confidence is too low |
| `sizing.use_kelly_overlay` | bool | false | - | Enable Kelly cap overlay on Masaniello - adds an additional risk ceiling based on Kelly criterion |

### Masaniello Batch & Kelly (4 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `sizing.kelly_fraction` | float | 0.25 | [0.05, 1.0] | Kelly fraction when overlay is active - scales the Kelly cap (0.25 = quarter-Kelly, very conservative) |
| `sizing.batch_size` | int | 5 | [2, 20] | Number of trades per Masaniello batch - the batch is the unit of position sizing progression |
| `sizing.target_wins` | int | 3 | [1, 20] | Target wins per batch - the Masaniello urgency factor increases when behind this target |
| `sizing.mc_simulations` | int | 1000 | [100, 10000] | Monte Carlo simulation count for batch probability estimation - more simulations = more accurate |

---

## 6. Stream (11 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `stream.min_prices_for_fit` | int | 81 | [20, 500] | Minimum prices required for HMM fit - ensures stable EM convergence (81 = 4-state HMM minimum) |
| `stream.window` | int | 500 | [50, 5000] | Price buffer maximum length - rolling window of prices maintained for each streaming session |
| `stream.kelly_fraction` | float | 0.5 | [0.1, 1.0] | Default Kelly fraction for streaming sessions - controls position sizing aggressiveness |
| `stream.target_vol` | float | 0.15 | [0.05, 0.50] | Default target annualized volatility - position sizes are scaled to target this vol level |
| `stream.base_risk_limit` | float | 0.02 | [0.005, 0.10] | Default daily risk limit for streaming - maximum fraction of equity at risk per day |
| `stream.refit_every` | int | 50 | [10, 200] | HMM refit frequency in ticks - how often to retrain the regime model on new data |
| `stream.regime_debounce` | int | 3 | [1, 10] | Regime change debounce bars - requires N consecutive bars in the new regime before alerting |
| `stream.subscriber_queue_size` | int | 500 | [50, 5000] | Per-subscriber asyncio.Queue maxsize - slow consumers drop ticks when queue is full |
| `stream.subscribe_timeout` | int | 30 | [5, 120] | WebSocket subscribe timeout in seconds - max wait for subscription confirmation |
| `stream.sse_heartbeat_timeout` | int | 20 | [5, 60] | SSE heartbeat timeout in seconds - connection closed if no heartbeat received |
| `stream.alert_queue_size` | int | 200 | [10, 1000] | SSE alert queue size - buffer for regime change and risk alerts |

---

## 7. Auth (6 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `auth.role_cache_ttl` | int | 300 | [10, 3600] | Clerk role cache TTL in seconds - how long to cache user role lookups before re-fetching |
| `auth.jwks_cache_ttl` | int | 3600 | [60, 86400] | JWKS cache TTL in seconds - how long to cache Clerk's JSON Web Key Set for JWT verification |
| `auth.circuit_failure_threshold` | int | 3 | [1, 20] | Auth circuit breaker failure threshold - opens the breaker after N consecutive auth failures |
| `auth.circuit_reset_timeout` | float | 60.0 | [5.0, 300.0] | Auth circuit breaker reset timeout in seconds - how long before retrying after the breaker opens |
| `auth.enrich_cache_ttl` | int | 300 | [10, 3600] | User enrichment cache TTL in seconds - how long to cache Clerk user profile data (email, name) |
| `auth.api_key_cache_ttl` | int | 60 | [10, 3600] | SaaS API key cache TTL in seconds - how long to cache API key lookups before re-hashing and re-checking |

---

## 8. Regime (7 keys)

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `regime.vol_window_long` | int | 20 | [5, 100] | Long volatility feature lookback window - rolling standard deviation calculation period |
| `regime.vol_window_short` | int | 5 | [2, 20] | Short volatility feature lookback window - captures recent vol changes more responsively |
| `regime.trend_window_short` | int | 10 | [2, 50] | Short momentum feature window - captures near-term trend direction |
| `regime.trend_window_long` | int | 30 | [10, 200] | Long momentum feature window - captures the broader trend structure |
| `regime.hmm_random_state` | int | 42 | - | HMM reproducibility seed - ensures consistent results across runs with the same data |
| `regime.hmm_n_iter` | int | 100 | [10, 1000] | HMM EM algorithm maximum iterations - more iterations improve convergence but increase compute time |
| `regime.stability_lookback` | int | 20 | [5, 100] | Regime stability lookback bars - how many recent bars to consider when assessing regime stability |

---

## Quick Reference: Most Frequently Adjusted Keys

These are the keys most likely to be adjusted in response to market conditions:

| Key | Typical Adjustment | When |
|-----|-------------------|------|
| `renko.brick_size` | Increase in high-vol, decrease in low-vol | Volatility regime change |
| `renko.max_daily_loss_bricks` | Tighten before earnings | Earnings season, event risk |
| `renko.kelly_fraction` | Reduce for conservative stance | Drawdown recovery, uncertainty |
| `renko.sl_bricks` / `renko.tp_bricks` | Wider stops in high-vol | Volatility increase |
| `risk.base_risk_limit` | Tighten in bear markets | Risk-off environment |
| `sizing.base_risk` | Reduce during drawdowns | Capital preservation |
| `sizing.batch_halt_dd` | Tighten for live trading | Going from paper to live |
| `stream.refit_every` | Increase for stability, decrease for responsiveness | Regime change speed |
| `regime.hmm_n_iter` | Increase for better convergence | Inaccurate regime detection |

---

## Adding New Config Keys

To add a new runtime-configurable parameter:

1. **Add the SQL seed** in a new migration file (e.g., `009_system_config_new_keys.sql`):

```sql
INSERT INTO system_config (key, value, value_type, category, description, default_value, env_var, min_value, max_value)
VALUES ('new_category.new_key', '42', 'int', 'new_category', 'Description of the new key', '42', 'NEW_CATEGORY_NEW_KEY', '1', '100')
ON CONFLICT (key) DO NOTHING;
```

2. **Register the default** in `runtime_config.py`:

```python
sync_config.register_defaults({
    "new_category.new_key": 42,
})
```

3. **Use in code** via the async interface:

```python
from regime_platform.core.runtime_config import runtime_config

new_value = await runtime_config.get_int("new_category.new_key", default=42)
```

4. **The admin panel** will automatically discover and display the new key on next cache refresh (within 30 seconds) - no frontend changes required.
