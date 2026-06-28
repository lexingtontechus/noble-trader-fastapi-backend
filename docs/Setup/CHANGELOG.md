## [8.0.0] - 2026-06-27

### Added — True Signal Gateway (Edge Function PUBLISH)

- **`PUBLISH agent:{symbol}` in edge function** (`sweep/index.deployed.ts` line 3609+) — after `redisSetConfig()` writes the full config hash, 4 True Signal gates are applied. If all pass, the config payload is published to `agent:{symbol}` via Redis PUBLISH. This eliminates the need for external agents to poll `HGETALL trading:config:{symbol}`.
- **`agent:{symbol}` Pub/Sub channel** — external agents subscribe and receive pushes only when a True Signal occurs. The edge function already has all data (regime, EV, TimesFM, signal) in local variables — no extra Redis reads needed.

### Changed — Decoupled Liveness from State

- **Removed per-tick `trading:heartbeat:{symbol}` writes** — the Python backend no longer writes heartbeat on every tick (lines 757-770 of `router.py` commented out). The heartbeat is now exclusively written by the edge function every 15 minutes (liveness-only).
- **Removed per-snapshot `trading:config:{symbol}` cache writes** — `_save_snapshot()` no longer calls `trading_heartbeat.cache_config()`. The config hash is updated by the edge function every cycle; the Python backend only reads it.
- **Removed periodic 100th-tick L1 snapshot checkpoint** — `_save_snapshot()` in `router.py` now only fires on position events (`result.get("filtered")` or `result.get("trade_result")`), not on `brick_count % 100 == 0`. Reduces Redis SET volume by ~90%.

### Optimized — Edge Function Config Writes

- **`redisUpdateSignal()` now conditional** — on regime shift or initial write cycles, `redisUpdateSignal()` is skipped because `redisSetConfig()` already writes all signal/EV/TimesFM fields. Only runs on non-shift cycles. Eliminates the duplicate HSET to `trading:config:{symbol}` per shift cycle (~50% reduction in edge function config write commands).

### The 4 Gates (Edge Function, Light-Regime Path Only)

1. **Regime Shift Hard Block** — no regime shift in last 300s (5-min cooldown)
2. **Shift Velocity Cap** — < 3 shifts in 24h (not turbulent)
3. **Regime Confidence** — `regime_conf > 0.40`
4. **Normalized EV** — `ev_per_dollar > 0.15`

### Changed — EV Engine v4 Refactor

- **Renamed `p_imbalance` → `p_ext_signal`** — honest naming: the slot accepts any external directional vote (L2 order book, sentiment, alternative data), not just L2 imbalance specifically
- **Markov chain relocated** — moved from P_win blend (where it double-counted directionality with HMM) to a position-sizing multiplier applied to `ev_scale`. Markov persistence now scales position size [0.4×–1.0×] instead of being blended into probability
- **Log-odds pooling** — replaced linear `Σ w_i * p_i` blending with `inv_logit(Σ w_i * logit(p_i))`. Respects probability bounds and prevents overconfidence when signals agree
- **New weights** — 40% Regime + 25% External + 35% TimesFM (with TimesFM); 55% + 45% (fallback)
- **Added `CalibrationTracker`** — decile-bin tracker with Brier score, reliability diagrams, and bias detection

### Added — L2 Order Book Data Pipeline

- **`AlpacaL2Adapter`** — WebSocket subscription to Alpaca IEX/SIP order book depth (5–10 levels, ~100ms updates for US equities/crypto)
- **`HyperliquidL2Adapter`** — WebSocket subscription to Hyperliquid l2Book channel (full depth for crypto perps, no API key required)
- **`L2ImbalanceComputer`** — multi-level weighted imbalance ratio with sigmoid confidence mapping and persistence tracking over a sliding window
- **`L2FeedManager`** — symbol→venue router (reads `nt_symbol.data_source` from Supabase, falls back to hardcoded map), manages adapter lifecycle, exposes `get_signal(symbol)` for EV engine consumption
- **App lifecycle** — L2 pipeline starts automatically at boot if `l2.enabled=true` in runtime config, shuts down gracefully

### Added — Hyperliquid Support

- **`HL_API_URL` and `HL_WS_URL`** settings in `settings.py` (default to Hyperliquid mainnet)
- **Runtime config keys** — `l2.levels`, `l2.level_weights`, `l2.imbalance_threshold`, `l2.persistence_window`, `l2.enabled`, `ev.calibration_enabled`, `ev.calibration_min_records`, `ev.calibration_over_threshold`

### Changed — Database

- **`nt_calibration_records.p_imbalance` renamed to `p_ext_signal`** — matches the EV engine rename
- **Added `predicted_p_win_raw` and `calibration_brier` columns** to `nt_calibration_records`
- **Migration `024_ev_calibration_l2_config.sql`** — seeds all new config keys into `nt_system_config`

### Infrastructure

- **No `.env` changes needed for LightningAI** — secrets injected as env vars are read automatically by `pydantic-settings`
- **Edge functions** — the Supabase Edge Functions (`sweep`, `heavy-sweep`, `light-regime`) contain a separate TypeScript EV engine implementation that still uses `p_imbalance` internally. These are independent Deno deployments and are unaffected by the Python backend changes

### Changed — Cron Schedules (Migrations 026 + 027)

- **Heavy sweeps moved to weekends only** — all `sweep-heavy-*` cron jobs now fire on Saturday/Sunday only (`0,6` DOW) at 20:00 UTC. Previously ran 6×/day for crypto and weekdays for stocks
- **Light stock sweeps reshaped to US market cycle** — split into peak (15min frequency 12:00-21:00 UTC) and off-peak (1hr frequency 21:00-12:00 UTC) windows. ~50% reduction in daily runs
- **Light crypto runs 24/7 on weekdays** — no suppression during US trading hours
- **`isHeavyHour()` day-aware** — the edge function guard now only suppresses light mode on weekends when heavy sweeps run. Weekdays never suppress
- **`sim_retry_stale_symbols()` F-Lite-1 check removed** — no longer skips retries during any hour since heavy sweeps don't run on weekdays

---

## [7.1.0] - 2026-05-24

### Fixed - Supabase Environment Variable Configuration

- **HTTP 500 on all `/renko/*` GET endpoints** - Root cause: `SUPABASE_URL` and `SUPABASE_KEY` were not configured on Render, causing `runtime_config` to fail when reading config values from Supabase `system_config` table. All Renko GET endpoints (`/renko/signals`, `/renko/state`, `/renko/bricks`, etc.) use `_get_pipeline_async()` which calls `RenkoConfig.from_runtime_config()`, requiring Supabase connectivity. Now returns proper 401 (not 500) after env vars are correctly set.

### Changed - Documentation & OpenAPI Spec Updates

- **OpenAPI spec v7.1.0** - Updated authentication description to reflect unified auth (Clerk JWT, Local JWT, SaaS API Key, Static API Key) instead of Clerk-only
- **OpenAPI spec** - Added `ApiKeyAuth` security scheme (`X-API-Key` header)
- **OpenAPI spec** - Corrected auth levels for `/renko/regime` and `/renko/equity` from "Write" to "Admin" (matching router code `require_admin`)
- **OpenAPI spec** - Added 401/403 response documentation for admin-only endpoints
- **OpenAPI spec** - Updated `/renko/snapshot/restore` to indicate "Write" access requirement
- **Renko README** - Corrected auth levels in endpoint table (`/renko/regime` and `/renko/equity` are Admin, not Write)
- **Renko README** - Added Render deployment note about Supabase env var configuration
- **Renko README** - Added Supabase config 500 error to bug fix history table

### Deployment Notes

- Required Render environment variables: `SUPABASE_URL` + `SUPABASE_KEY` (service role key)
- There is NO `SUPABASE_PUBLISHABLE_KEY` variable - the backend needs the service role key for full database access
- Verify connectivity with `GET /renko/health` (no auth required)

---

## [5.1.0] - 2026-05-20

### Phase 6+7: Frontend UI

- **StatisticalRigorPanel.jsx** - New component with 4 sub-cards:
  - DSR Detail Card (observed_sharpe, expected_max_sharpe, p_value, n_trials, sample_length, threshold)
  - Bootstrap CI Table (4 metrics × 2 methods: percentile + circular block)
  - Multiple Testing Corrections (Bonferroni, Holm-Bonferroni, BH-FDR with summary counts)
  - Significance Tests (White's Reality Check + Hansen's SPA with consensus alert)
- **ExecutionModelingPanel.jsx** - New component with 5 sub-cards:
  - Almgren-Chriss Market Impact (participation rate, permanent/temporary/total impact, composition bar)
  - Fill Probability (order type, distance metrics, adverse selection, gauge visualization)
  - Financing Costs (borrow/margin/dividend breakdown with HTB badge)
  - Enhanced Cost Breakdown (bar chart per cost type)
  - Execution Modeling Summary (market impact + fill probability + financing overview)
- **BacktestResults.jsx** - Tabbed navigation (Overview / Statistics / Execution),
  integrates both new panels with proper data extraction from both runtime field names
  (`bootstrap_ci` and `bootstrap_cis`, `deflated_sharpe` and `deflated_sharpe_result`)
- **BacktestPanel.jsx** - "Deep Analysis" button calling /renko/statistics/rigor and
  /renko/execution/model via bffFetch; new onSignificanceTests and onExecutionModelDetail callbacks
- **RenkoPage.jsx** - New state (significanceTests, executionModelDetail) wired to both panels
- **types/backtest.ts** - Fixed type/runtime mismatches, added 15 new interfaces
  (BootstrapCIMetric, SignificanceTestResults, MarketImpactResult, FillProbabilityResult,
  BorrowCostResult, MarginCostResult, DividendCostResult, FinancingCostsResult,
  ExecutionModelDetail, MultipleTestingSummary, BootstrapCIMethod, etc.)

## [5.0.0] - 2026-05-20

### Phase 6: Statistical Rigor
- **Deflated Sharpe Ratio** - Lopez de Prado DSR adjusts observed Sharpe for N independent trials
- **Bootstrap Confidence Intervals** - Percentile + circular block bootstrap CIs for Sharpe, win rate, profit factor, max drawdown
- **Multiple Testing Correction** - Bonferroni, Holm-Bonferroni, Benjamini-Hochberg FDR
- **White's Reality Check + Hansen's SPA** - Bootstrap-based significance tests for data snooping

### Phase 7: Advanced Execution Modeling
- **Almgren-Chriss Market Impact** - Square-root model with permanent + temporary impact components
- **Fill Probability Model** - Logit-based fill probability for limit/stop/market orders with adverse selection
- **Borrow & Financing Costs** - Short borrow costs, margin financing, dividend costs for shorts

### Pipeline Enhancements (Phase 2-3)
- **Transaction Cost Modeling** - Slippage, commission, and spread tracked separately per trade
- **Gap-Through Stop Handling** - Fills at brick open when price gaps past stop level
- **OCO Resolution** - Configurable SL-first or TP-first when both trigger in same brick

### New API Endpoints
- `POST /renko/backtest/run` - Full backtest with Phase 2-7 analytics
- `POST /renko/backtest/run/stream` - SSE streaming backtest
- `POST /renko/backtest/optimize` - Parameter sweep with multiple testing correction
- `POST /renko/statistics/rigor` - Standalone statistical rigor tests
- `POST /renko/execution/model` - Execution cost modeling

### Institutional Maturity Score
- Overall: **2.8 -> 3.7** (crosses 3.5 institutional threshold)
- Data Quality: 1.5 -> 4.0
- Statistical Validation: 2.5 -> 4.0
- Execution Modeling: 2.5 -> 4.0

---

## [5.4.0] - 2026-05-19

### Added - Progressive/Chunked Backtest Loading (SSE Streaming)

#### `renko/pipeline.py` - New Method
- `backtest_chunked(prices, timestamps, regimes, chunk_size=150)` - Generator that yields intermediate results after each chunk of ticks for SSE streaming
- Progress events: `{ type: "progress", chunk, total_chunks, percent, trades_so_far, stats_so_far, bricks_so_far, ticks_so_far }`
- Complete event: `{ type: "complete", stats, total_bricks, total_ticks, trades }` - identical shape to `backtest()` return value
- `chunk_size=150` matches the warmup route's `CHUNK_SIZE` pattern

#### `renko/router.py` - New Endpoint
- `POST /renko/backtest/run/stream` - SSE streaming backtest endpoint
- Uses `StreamingResponse` with `text/event-stream` media type
- Processes prices in 150-tick chunks via `backtest_chunked()`
- Emits `progress`, `complete`, and `error` SSE event types
- Cache integration: checks Redis before streaming; if HIT, emits `complete` immediately
- Final `complete` result is cached in Redis (1h TTL) for subsequent requests
- Headers: `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no` (disables nginx buffering)

#### Tests - `tests/test_renko_backtest.py` (6 new tests)
- `test_chunked_yields_progress_events` - verifies ≥2 progress + 1 complete event
- `test_chunked_progress_structure` - required keys in each progress event
- `test_chunked_percent_increases` - monotonically increasing percent
- `test_chunked_complete_matches_backtest` - chunked result matches regular `backtest()`
- `test_chunked_final_percent_is_100` - last progress = 100%
- `test_chunked_with_regimes` - regime labels passed through correctly

### Changed
- `renko/pipeline.py` - added `import math` and `Generator` type hint
- Total backend tests: 78 (25 general + 25 Renko + 28 cache)

---

## [5.3.0] - 2026-05-19

### Added - Redis Backtest Result Caching (1h TTL)

#### `services/backtest_cache.py` - NEW FILE
- `BacktestCache` - async Redis-backed cache for backtest results with 1h TTL
- Deterministic cache keys via SHA-256 of canonical JSON (sorted keys, price arrays replaced with length)
- Key format: `bt:{prefix}:{sha256_hash}` where prefix is `renko`, `renko:compare`, `renko:optimize`, or `hmm`
- Graceful fallback: if `REDIS_URL` is not configured or Redis is unavailable, all operations are no-ops
- Redis failures NEVER propagate to the caller - cache miss is returned instead
- `get(prefix, body)` -> cached result dict or None (cache miss)
- `set(prefix, body, result)` -> True/False (stores with 1h TTL)
- `invalidate(prefix, body)` -> removes specific cached result
- `clear_prefix(prefix)` -> removes all cached results for a prefix (admin/debug)
- `get_stats()` -> key counts, memory usage, TTL info
- `close()` -> graceful Redis connection cleanup
- Singleton `backtest_cache` shared across all endpoints

#### Cache Integration - Renko Endpoints
- `POST /renko/backtest/run` - checks cache before running; stores result after; `cached` field in response
- `POST /renko/backtest/compare` - checks cache before running; stores result after
- `POST /renko/backtest/optimize` - checks cache before running; stores result after

#### Cache Integration - HMM Endpoints
- `POST /backtest/run` - checks cache before running; stores result after; `cached` field in response

#### Response Model Updates
- `RenkoBacktestResponse.cached: bool` - True if served from Redis cache (default False)
- `BacktestResponse.cached: bool` - True if served from Redis cache (default False)

#### Tests - `tests/test_backtest_cache.py` (28 new tests)
- Cache key generation: deterministic hashing, param sensitivity, prefix isolation, price array length substitution, key order independence, input immutability
- Cache disabled: all operations return None/False when REDIS_URL not set
- Cache enabled (mocked Redis): GET hit/miss, SET with TTL, INVALIDATE, CLEAR_PREFIX, STATS
- Graceful fallback: Redis errors never propagate (GET returns None, SET returns False)
- Response model `cached` field: default False, accepts True

### Changed
- `main.py` version bumped to 5.3.0
- Total backend tests: 72 (25 general + 19 Renko + 28 cache)

---

## [5.2.0] - 2026-03-04

### Added - Renko Backtest Endpoints (Isolated Pipeline)

#### `renko/router.py` - 3 New Endpoints

Three isolated backtest endpoints that run on standalone `RenkoPipeline` instances, never affecting the live pipeline:

- `POST /renko/backtest/run` - Run a full Renko pipeline backtest on historical prices with configurable parameters. Returns comprehensive stats including win rate, profit factor, Sharpe estimate, max drawdown, per-pattern breakdowns, per-regime breakdowns, and Kelly inputs.
- `POST /renko/backtest/compare` - Compare 2–10 Renko pipeline configurations side-by-side on the same price data. Returns per-config results plus a diff summary highlighting key metric differences between the first config and the best (by Sharpe estimate).
- `POST /renko/backtest/optimize` - Grid-search parameter sweep for Renko pipeline optimisation. Accepts a `param_grid` dict mapping parameter names to lists of values. Returns summary rows with key metrics for each combination, plus `best_by_sharpe` and `best_by_return` highlights. Max 50 combinations per request.

#### Pydantic Models

- `RenkoBacktestRequest` - Full pipeline config + historical prices (min 50 ticks) with optional timestamps, regimes, and signal_confidence_min
- `RenkoBacktestResponse` - symbol, total_ticks, total_bricks, config_used, stats, trades
- `RenkoCompareConfig` - Labelled config subset for comparison (brick_size, sl_bricks, tp_bricks, trailing_stop, trail_after_bricks, trail_distance_bricks, bull_trigger_n, bear_trigger_n, regime_gate, max_trades_per_session, max_daily_loss_bricks, max_consecutive_losses)
- `RenkoBacktestCompareRequest` - prices + symbol + 2–10 configs + optional timestamps/regimes
- `RenkoBacktestCompareResponse` - comparisons list + diff dict (first vs best by Sharpe)
- `RenkoBacktestOptimizeRequest` - prices + symbol + param_grid + fixed default params + optional timestamps/regimes
- `RenkoBacktestOptimizeResponse` - results list (params + metrics per combo) + best_by_sharpe + best_by_return + n_combinations

#### Internal Helpers

- `_build_renko_config(req)` - Build a `RenkoConfig` from a backtest request body
- `_run_isolated_backtest(prices, config, ...)` - Run a backtest in an isolated pipeline (never touches live state)

### Changed
- `main.py` version bumped to 5.2.0
- All three backtest endpoints require authentication (`get_authed_user`) and are rate-limited under `RATE_LIMIT_WRITE`
- Backtest runs are executed in thread pools via `asyncio.to_thread` to avoid blocking the event loop

---

## [5.1.0] - 2026-05-19

### Added - MCP Server + Circuit Breakers + Renko Pipeline v2 + Discord

#### MCP Server (Model Context Protocol)
- `fastapi-mcp` integration mounted at `/mcp`
- 17 operations excluded for security (auth, feed start/stop, renko writes, stream injection, backtest delete)
- Configurable auth via `MCP_AUTH_ENABLED` and `MCP_ISSUER` settings
- Header forwarding for `authorization` and `x-api-key`

#### Circuit Breakers
- Three-tier circuit breaker: error rate (>50%), latency (>5s), concurrent (>100)
- 30-second cooldown with automatic recovery
- Stats exposed in `/health` endpoint
- Per-breaker state tracking (closed, open, half-open)

#### Renko HFT Pipeline v2
- 14 endpoints under `/renko/` prefix
- 6-layer pipeline: brick -> swing -> pattern -> signal -> risk -> execution
- Supabase caching with 4-hour TTL
- Yahoo Finance warm-up integration
- Pattern confidence scoring
- Signal-to-trade paper execution

#### Discord Notifications
- `notifications/discord_notifier.py` - webhook-based alert system
- Regime change alerts with severity coloring
- Circuit breaker trip notifications
- Trade execution confirmations
- Graceful no-op when `DISCORD_WEBHOOK_URL` not configured
- Async batched sending with retry

### Changed
- `main.py` version bumped to 5.1.0
- `/health` endpoint now includes MCP status, circuit breaker stats, and Discord status
- Lifespan: startup logs GPU capabilities, restores Redis sessions with HMM re-fitting

---

## [5.0.0] - 2026-05-15

### Added - Renko HFT Pipeline (Initial)

#### `renko/` - NEW MODULE
- Self-contained Renko HFT pipeline with 6 processing layers
- Brick construction from tick data (fixed and ATR-based)
- Swing classification (impulse/correction labelling)
- Pattern detection (double-top/bottom, head-shoulders, flags)
- Signal filtering with volume and trend confirmation
- Risk management with position sizing and stop/TP
- Paper trade execution with P&L tracking
- Supabase caching layer (4h TTL)

#### Endpoints
- `POST /renko/brick` - build Renko bricks
- `POST /renko/swing` - classify swings
- `POST /renko/pattern` - detect patterns
- `POST /renko/signal` - generate filtered signals
- `POST /renko/risk` - compute risk for signal
- `POST /renko/execute` - paper-trade execution
- `GET /renko/status` - pipeline status (all symbols)
- `GET /renko/status/{symbol}` - single symbol status
- `DELETE /renko/status/{symbol}` - clear state
- `POST /renko/warmup` - Yahoo Finance warm-up
- `GET /renko/trades` - list trades
- `GET /renko/trades/{symbol}` - symbol trades
- `GET /renko/cache` - cache statistics
- `DELETE /renko/cache` - clear cache

---

## [4.0.0] - 2026-05-10

### Added - Strategy Signals + TDA Features + Observation V2

#### Strategy Signal Pipeline
- `routers/strategy.py` - `POST /strategy/signal`
- Generates trading signal (long/short/flat) based on regime
- Combines regime detection, Kelly sizing, and risk guidance
- Signal strength scoring (0-1)
- Long: low/med-low vol + bull/strong_bull
- Short: high/med-high vol + bear/strong_bear (strength × 0.5)
- Flat: all other regimes

#### Topological Data Analysis (TDA)
- `routers/tda.py` - `POST /tda/features`
- Persistent homology feature extraction via gtda
- Takens embedding with configurable dimension and delay
- Persistence diagrams (0D and 1D)
- Betti numbers at each filtration step
- Persistence entropy for regime change detection
- Anomaly score (std deviations from baseline)
- 10-dimensional feature vector for ML pipeline
- Optional baseline comparison

#### Observation V2
- `POST /observation/build-v2` - enhanced observation vector with optional TDA
- Extends 24-feature vector to 34 features when `include_tda=true`
- TDA anomaly score and regime change probability fields
- Same Markov feature guarantees as v1

#### `models/schemas.py` - Updated
- `StrategySignalRequest` / `StrategySignalResponse`
- `TDARequest` / `TDAResponse`
- `ObservationV2Request` / `ObservationV2Response`

### Changed
- `main.py` version bumped to 4.0.0
- Backtest router registered in app factory

---

## [3.2.0] - 2026-05-19

### Added - Walk-Forward Backtest Engine + Persistence + Optimization

#### `core/backtester.py` - NEW FILE
- 503-line walk-forward HMM backtest engine
- Rolling HMM fit on trailing window with configurable refit interval
- Strategy signal generation based on detected regime
- Regime-gated Kelly position sizing
- Risk check gates (VaR, max drawdown)
- Trade execution with configurable commission and slippage
- 30+ performance metrics: win rate, Sharpe, Sortino, Calmar, profit factor,
  max drawdown, VaR/CVaR, avg hold bars, regime distribution, trades by regime
- Equity curve, drawdown curve, and full trade log output
- Optional date labels in trade log (pass `dates` array)

#### `services/backtest_store.py` - NEW FILE
- Supabase persistence layer for backtest results
- `save_backtest_result()` - persist to `ta_backtest_result` table (user-scoped)
- `get_backtest_history()` - paginated history with summary metrics only
- `get_backtest_detail()` - full result by ID (user-scoped)
- `delete_backtest()` - delete by ID (user-scoped)
- Graceful degradation when Supabase is not configured

#### `routers/backtest.py` - NEW FILE
- `POST /backtest/run` - run walk-forward backtest (rate-limited, Clerk/JWT auth)
- `GET /backtest/history` - paginated list of saved results
- `GET /backtest/{id}` - full result by ID
- `POST /backtest/compare` - side-by-side comparison with diff summary
- `POST /backtest/optimize` - parameter grid sweep (max 50 combos)
- `POST /backtest/export` - CSV/JSON export with section selection
- `DELETE /backtest/{id}` - delete saved result

#### `models/schemas.py` - Updated
- `BacktestRequest` - extended with `dates` and `save` fields
- `BacktestResponse` - full response with 30+ metric fields
- `BacktestSummary` - lightweight summary for list views
- `BacktestHistoryResponse` - paginated summary list
- `BacktestCompareRequest` / `BacktestCompareResponse` - comparison + diff
- `BacktestOptimizeRequest` / `BacktestOptimizeResponse` - grid sweep
- `BacktestExportRequest` - export format + section selection

#### `supabase/migrations/00000000000007_backtest_results.sql` - NEW FILE
- `ta_backtest_result` table with RLS policies (user-scoped access)
- Summary columns + JSONB columns for heavy data (equity curve, trade log, etc.)

### Fixed
- `pnl_pct` was always returning 0.0 in backtest trade log - now correctly computed

### Changed
- All backtest endpoints require Clerk/JWT authentication
- Rate-limited under `RATE_LIMIT_COMPUTE`

---

## [3.1.1] - 2026-Q2 (Testing & Tooling Release)

### Added - Testing Tools & Developer Experience

#### `curl_test.sh` - NEW FILE
Bash script: 6 assertions against a live server.
1. GET /health -> 200
2. POST /analyse/full with 200 synthetic SPY bars -> 200
3. All 12 response fields present (regime_label, confidence, risk_multiplier,
   recommended_f, sharpe_ratio, var_95, cvar_95, max_drawdown, suggested_stop,
   suggested_tp, etc.)
4. 6-bar payload -> 422 (validation rejection)
5. Negative price -> 422 (validation rejection)
Supports custom base URL: `./curl_test.sh http://my-host:8000`
Coloured output with jq if installed, falls back to python3 -m json.tool.

#### `stream_spy_ticks.py` - NEW FILE (423 lines)
Python script: seeds SPY with 150 bars, pushes 50 live ticks via POST /stream/tick,
prints a formatted TickResponse for each tick.
- Pure stdlib + numpy - no third-party HTTP client needed
- Formatted output: regime label (colour-coded), f*, mult, confidence, VaR95,
  stop/TP, prob bars for vol and trend posteriors
- Regime-change banner printed mid-stream
- Full summary table at end: regime sequence, f* range, VaR range, RTT latency
- CLI options: --url, --symbol, --seed-bars, --ticks, --interval, --regime,
  --mu, --sigma, --no-color, --json
- --json flag pipes raw TickResponse JSON for jq processing
- Buffering stub handled gracefully (warming-up message)

#### `sse_alert_monitor.html` - NEW FILE (368 lines)
Browser EventSource monitor for GET /sse/alerts.
- EventSource auto-reconnect with error distinction (never-connected vs dropped)
- Every raw message logged via console.log; alerts routed by severity:
  critical -> console.error · warning -> console.warn · info -> console.info
- Alert entry: coloured regime labels, mult/f* extracted from message string,
  critical badge pulses 3× via CSS animation
- Stats bar: live counts per severity, heartbeat count, uptime timer, last alert
- Log capped at 300 entries; all server strings pass through escHtml() (XSS guard)
- Enter key connects/disconnects; works with any CORS-enabled SSE endpoint

#### `payload_spy_200bars.json` - NEW FILE
Ready-made 200-bar SPY JSON payload for curl / Postman / Insomnia testing.
4-segment multi-regime series (low-vol bull -> med bull -> med bear -> high-vol bear).

### Changed
- `README.md` - full rewrite: complete project structure tree, all 21 endpoints,
  Masaniello formula table, 24-feature index table, testing sequence, auth/Redis/GPU
  quickstart commands

---

## [3.1.0] - 2026-Q2

### Changed - Dynamic Masaniello + Kelly Unified Sizing Engine

#### `core/position_sizer.py` - Full Refactor

Two complementary sizing paths now live in the same module, independently usable.

**Path A - Portfolio-fraction Kelly (preserved, interface unchanged)**
`PositionSizer`, `PositionRequest`, `KellyResult` - stateless, returns-based.
All existing callers continue to work without modification.

**Path B - Dynamic Masaniello Sizer (new)**

Implements the formula:

```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
```

New public symbols:

| Symbol | Purpose |
|--------|---------|
| `SizingConfig` | β, min/max risk, gates, kelly overlay, batch params |
| `TradeContext` | Per-trade inputs (equity, stop, point_value, p_win, R/R, regime fields) |
| `TradeContext.from_snapshot(snap, ...)` | Build context directly from `RegimeSnapshot` |
| `PositionSizeResult` | All intermediate factors + contracts + block reason |
| `PositionSizeResult.summary()` | One-line human-readable string |
| `BatchState` | Cumulative wins/losses/pnl/drawdown/peak_equity tracker |
| `DynamicMasanielloSizer` | Stateful sizer; `.batch` tracks inter-trade state |
| `.size_trade(ctx)` | Core formula computation |
| `.size_from_snapshot(snap, ...)` | Convenience wrapper - builds context + sizes |
| `.run_batch(contexts, outcomes, pnls)` | Size N trades sequentially with state injection |
| `monte_carlo_batch(sizer, ctx, n)` | Simulate N independent batches; return PnL/DD distribution |

**Pure factor functions (testable in isolation):**
`_masaniello_factor`, `_quality_factor`, `_drawdown_factor`, `_volatility_factor`,
`_fractional_kelly`, `_expected_edge`

**Factor details:**

`M_i = clip((W - w) / (N - i + 1), 0.0, 1.5)`
- Wins needed divided by trades remaining. Zero when target already met.
- `(0.5 + M_i)` term guarantees minimum 0.5× base risk even when M_i = 0.

`Q_i = prob_factor × regime_factor × conf_factor`
- `prob_factor  = clip((p_win - min_prob) / 0.10, 0.0, 1.5)`
- `regime_factor = clip(regime_quality, 0.0, 1.5)`  ← from `RegimeSnapshot.risk_multiplier`
- `conf_factor   = clip(state_confidence, 0.0, 1.0)` ← from `RegimeSnapshot.confidence`
- Q_i = 0 when any sub-factor is zero; acts as a quality gate.

`DD_i = clip(1 - dd / max_dd, 0.25, 1.0)`
- Floor at 0.25 preserves minimum position participation during drawdown.

`V_i = clip(ATR_baseline / ATR_current, 0.5, 1.5)`
- Inverted ATR ratio. High vol -> shrink; low vol -> mild expansion.

**Pre-flight gates (in priority order, fire before formula):**
1. equity > 0
2. stop_distance_price > 0
3. point_value > 0
4. regime_quality ≥ regime_floor
5. p_win ≥ min_prob
6. reward_risk ≥ min_rr
7. batch not halted
8. drawdown < batch_halt_dd (triggers halt on violation)
9. expected_edge > 0

**Kelly overlay:**
When `use_kelly_overlay=True`, the Masaniello fraction is capped at
`kelly_fraction × max(0, (b·p - q) / b)`. When kelly_cap = 0 (no edge),
the min_risk floor still applies (position is not zeroed out).

**Hard clamp:**
`risk_fraction = clip(f_i, min_risk, max_risk)` - always enforced last.

#### `test_position_sizer.py` - NEW FILE
95-test suite covering:
- Factor math boundary conditions (all 4 factors + Kelly + edge)
- Core formula with known inputs (manual verification)
- Gate ordering - each block reason fires correctly
- Kelly overlay (cap applied, cap=0 fallback)
- Hard clamp (upper and lower bounds)
- RegimeSnapshot integration (`from_snapshot`, `size_from_snapshot`)
- BatchState lifecycle (record, drawdown, reset)
- `run_batch` state injection and sequential progression
- Monte Carlo (p_target_hit ~69.5%, p_halt, mean/std/p10/p90 PnL)
- Path A interface preserved (KellyResult fields, regime gating, vol scaling)

### Fixed
- Uploaded `dataclass.md` bug: `_init_` (single underscores) corrected to `__init__` in `DynamicMasanielloSizer`
- Corrected Kelly gate test: `p=0.50, R=1.0` correctly yields `f=0.0` (not `p=0.50, R=2.0` which has positive edge)

---

## [3.0.0] - 2025-Q1

### Added - GPU HMM · Live Feeds · Correlation Regimes · Portfolio Optimisation

#### `core/gpu_engine.py` - NEW FILE
- `GpuRegimeHMM` - pomegranate PyTorch-backed 4-state HMM, identical interface to `RegimeHMM`
- `create_regime_hmm(force_cpu)` - auto-selecting factory: GPU if pomegranate + CUDA/MPS available, else hmmlearn CPU
- `gpu_capabilities()` - returns `{pomegranate_installed, cuda_available, mps_available, active_device, hmm_backend}`
- Transparent CPU fallback - zero code changes required when GPU unavailable
- Supports CUDA (NVIDIA), MPS (Apple Silicon), and CPU targets

#### `adapters/feed_adapters.py` - NEW FILE
- `FeedAdapter` - abstract base with exponential-backoff reconnect (max 10 attempts)
- `AlpacaFeedAdapter` - alpaca-py WebSocket, supports SIP and IEX feeds, equities + crypto
- `BinanceFeedAdapter` - native WebSocket kline stream, any interval (1m/5m/1h/...), mainnet + testnet
- `IBFeedAdapter` - ib_async TWS API, real-time 5-second bars, equities/futures/FX
- `OHLCVBar` - unified OHLCV dataclass with `source` field
- `FeedManager` - multi-adapter orchestrator, routes bars into `SessionRegistry`, chainable `.add()`

#### `core/correlation_regime.py` - NEW FILE
- `CorrelationRegimeDetector` - DCC-based portfolio-level correlation regime classifier
- Extracts rolling Pearson correlation matrices (configurable window, default 60 bars)
- Applies EWMA vol standardisation before correlation computation
- Fits a 4-state Gaussian HMM on upper-triangle correlation feature vectors
- States labelled by mean |ρ|: `low_corr` | `mid_corr` | `high_corr` | `crisis`
- `CorrelationSnapshot` - per-asset breakdowns + `blended_risk_multiplier` = mean(asset_mults) × corr_mult
- Correlation risk multipliers: `low_corr=1.00` · `mid_corr=0.85` · `high_corr=0.60` · `crisis=0.35`

#### `core/portfolio_optimiser.py` - NEW FILE
- `DrawdownOptimiser` - regime-aware max-Sharpe subject to drawdown constraint
- Three regime layers: (1) regime-adjusted μ, (2) per-asset weight bounds, (3) correlation exposure
- Drawdown constraint via Cornish-Fisher CVaR proxy (analytic - no simulation required)
- Correlation exposure scalar: `low_corr=1.00` · `mid_corr=0.90` · `high_corr=0.70` · `crisis=0.50`
- Weight bounds tightened per-asset: `high_vol_strong_bear -> max_weight × 0.25`
- Scipy SLSQP solver with configurable `max_iter`, `tol`, and `dd_penalty_lambda`
- `OptimisationResult` - weights, regime_adj_weights, expected return/vol/Sharpe/DD, convergence info

#### `routers/gpu.py` - NEW FILE
- `GET /gpu/capabilities` - hardware and backend report
- `POST /gpu/benchmark` - fit + predict latency benchmark on synthetic data, `force_cpu` flag

#### `routers/feeds.py` - NEW FILE
- `POST /feeds/start` - register and start feed adapters as background asyncio tasks
- `POST /feeds/stop` - graceful shutdown of all adapter tasks
- `GET /feeds/status` - adapter health + per-symbol bar counts

#### `routers/multi_asset.py` - NEW FILE
- `POST /correlation/detect` - fit DCC correlation HMM + return correlation regime snapshot
- `POST /optimise` - solve drawdown-controlled max-Sharpe with regime overlays
- `POST /optimise/full` - one-shot: correlation detect + portfolio optimise

#### `main_v4.py` - NEW FILE
- v3.0 app factory with GPU capability logging on startup
- `GET /health` now includes `gpu_backend` and `device` fields

### Changed
- `requirements.txt` - added optional GPU and feed adapter dependency comments
- `core/portfolio_optimiser.py` - fixed array ambiguity bug in `neg_sharpe_with_dd` (scipy `max()` applied to matrix)

### Architecture summary

```
GET  /gpu/capabilities       pomegranate install check -> CUDA/MPS/CPU selection
POST /gpu/benchmark          synthetic fit -> latency report

POST /feeds/start            FeedManager -> AlpacaFeedAdapter/BinanceFeedAdapter/IBFeedAdapter
                             -> SessionRegistry.get(symbol).tick(close, ts)

POST /correlation/detect     rolling corr features -> 4-state HMM -> corr regime label
POST /optimise               regime-adj μ -> SLSQP -> DD-constrained weights
POST /optimise/full          detect + optimise in one request
```

---

## [2.1.0] - 2024-Q4

### Added - Simulation · Portfolio · Redis Persistence · JWT Auth

#### `core/simulator.py` - NEW FILE
- `RegimeSimulator` - Markov-chain Monte Carlo regime transition simulator
- Builds a 16×16 combined transition matrix via Kronecker product of the fitted
  4-state vol and trend HMM transition matrices: `T_combined = T_vol ⊗ T_trend`
- Fits per-regime (μ, σ) return distributions from Viterbi-decoded historical paths
- Simulates `n_paths` forward trajectories of `horizon` steps with regime switching
- Returns: price fan (p5/p25/median/p75/p95), expected risk multiplier per step,
  terminal return distribution (mean, std, VaR95, CVaR95), most likely terminal
  regime, % paths with positive return, mean maximum drawdown across paths

#### `services/portfolio_service.py` - NEW FILE
- `PortfolioService` - aggregates regime + risk analytics across all active sessions
- Per-symbol: regime label, confidence, risk_multiplier, Kelly f*, VaR95, Sharpe
- Portfolio-level: dominant regime, regime consensus, avg risk multiplier,
  portfolio VaR (√ΣVaR² - independence assumption), portfolio CVaR
- Risk flags: `high_risk_count`, `concentration_flag`, `regime_divergence_flag`
- Regime divergence detection via normalised entropy of vol_state distribution

#### `services/redis_persistence.py` - NEW FILE
- `RedisPersistence` - optional Redis Streams price buffer persistence layer
- All methods are no-ops when `REDIS_URL` env var is not set (zero overhead)
- `restore(session)` - hydrates session price buffer from Redis on startup
- `append_price(symbol, price)` - appends to Redis Stream with MAXLEN cap
- `save_meta / load_meta` - stores session config (kelly_fraction, etc.)
- `list_persisted_symbols()` - returns all symbols with persisted streams
- Failure-tolerant: Redis errors never raise in the tick path (log + continue)

#### `auth/jwt_auth.py` - NEW FILE
- `create_access_token(data)` - creates signed HS256 JWT
- `decode_token(token)` - validates and decodes JWT -> `TokenData`
- `get_current_user` - FastAPI Depends for HTTP endpoint auth
- `require_write` - Depends requiring trader or admin role
- `require_admin` - Depends requiring admin role
- `ws_auth(websocket, token, api_key)` - WebSocket auth (closes 4001 on failure)
- `make_login_response(sub, role)` - standard token response helper
- API key fallback via `X-API-Key` header and `API_KEYS` env var
- `AUTH_ENABLED=false` disables auth entirely for development

#### `routers/simulate.py` - NEW FILE
- `POST /simulate/{symbol}` - fits HMM + simulates regime transition paths
- Parameters: `horizon` (1–252), `n_paths` (50–5000), `seed`, `current_price`

#### `routers/portfolio.py` - NEW FILE
- `GET /portfolio` - aggregated portfolio risk view across all active sessions
- Query params: `symbols` (CSV), `kelly_fraction`, `target_vol`, `base_risk_limit`
- `GET /portfolio/symbols` - list all seeded symbols

#### `routers/auth_router.py` - NEW FILE
- `POST /auth/token` - OAuth2 password flow, issues JWT
- `GET /auth/me` - returns current user info from token
- `POST /auth/refresh` - issues a fresh token (same sub/role)
- User store configured via `AUTH_USERS` env var (user:pass:role CSV)

#### `main_v3.py` - NEW FILE
- v2.1 app factory with `lifespan` context manager
- On startup: restores persisted sessions from Redis, re-fits HMMs
- All v2.0 endpoints unchanged; v2.1 routers added alongside
- `/health` returns Redis status and active session count

### Changed
- `requirements.txt` - added `python-jose[cryptography]`, `passlib[bcrypt]`, `redis`

### Architecture summary

```
POST /simulate/{symbol}    HMM fit -> 16-state Kronecker system -> Monte Carlo
GET  /portfolio            SessionRegistry -> per-symbol regime + risk -> aggregate
POST /auth/token           credential check -> JWT issue
WS   /ws/{symbol}          (auth-ready: pass ?token=<jwt> to authenticate)
REDIS_URL (env)            on startup -> restore buffers -> re-fit HMMs
```

---

## [2.2.0] - 2024-Q3

### Added - 24-Feature Observation Vector & Stateful Builder

#### `core/obs_builder.py` - NEW FILE
- `InferenceObservationBuilder` - stateful class that maintains a fitted `RegimeHMM`
  across calls, ensuring Markov features (indices 14–19) are always computed from
  live HMM posteriors, never from a uniform prior
- `F` - feature index namespace (single source of truth for all 24 indices)
- `Observation` - output dataclass with `.markov_features` slice property and
  `.is_markov_uniform(tol)` guard method
- `build(prices, high, low)` - full-window build; re-fits HMM every `refit_every` bars
- `build_from_tick(price, high, low)` - streaming single-bar variant
- `seed(prices, high, low)` - historical warm-up and initial HMM fit
- `update_recommended_f(f)` - injects current Kelly fraction for Masaniello feature

#### 24-Feature Vector

| Index | Feature | Notes |
|-------|---------|-------|
| 0 | `log_return_1bar` | 1-bar log return |
| 1 | `log_return_3bar` | 3-bar log return |
| 2 | `log_return_10bar` | 10-bar log return |
| 3 | `normalised_atr` | ATR(14) / price |
| 4 | `rolling_vol_20` | 20-bar σ (annualised) |
| 5 | `ema_distance` | (price − EMA20) / EMA20 |
| 6 | `hhll_score` | HH/HL structure score ∈ [−1, 1] |
| 7 | `vol_regime_percentile` | Vol percentile vs 252-bar history |
| 8 | `atr_vs_baseline` | ATR(14) / ATR(252-bar mean) |
| 9 | `vol_trend_slope` | Standardised 10-bar vol slope |
| 10–13 | `regime_prob_{0..3}` | Raw HMM state posteriors (unsorted) |
| **14** | **`vol_prob_low`** | P(low vol) - Markov feature |
| **15** | **`vol_prob_medium`** | P(med_low + med_high) - Markov feature |
| **16** | **`vol_prob_high`** | P(high vol) - Markov feature |
| **17** | **`trend_prob_down`** | P(strong_bear + ½·bear) - Markov feature |
| **18** | **`trend_prob_neutral`** | P(½·bear + ½·bull) - Markov feature |
| **19** | **`trend_prob_up`** | P(½·bull + strong_bull) - Markov feature |
| 20 | `regime_quality` | HMM confidence scalar |
| 21 | `state_confidence` | max(vol_probs) × max(trend_probs) |
| 22 | `masaniello_pressure` | f* × (1 − f*) - peaks at 0.25 when f=0.5 |
| 23 | `drawdown_factor` | Current DD from peak ∈ [−1, 0] |

#### `core/regime_engine.py` - Updated
- Added `_sanitize_model()` static method: fixes NaN `startprob_`, zero `transmat_`
  rows, NaN `means_`, and degenerate `_covars_` after EM convergence failure
- `fit()` calls `_sanitize_model()` on both sub-models after every EM run
- `predict()` adds NaN fallback: degenerate posteriors replaced with uniform, then
  re-normalised - guarantees finite output even on pathological price series

#### `test_obs_builder.py` - NEW FILE
- 7-test suite: shape, finite values, Markov non-uniformity, Masaniello response,
  drawdown sign, streaming tick path, and all-zero sentinel guard

### Fixed
- `RegimeHMM._sanitize_model()` writes to `_covars_` private attribute (not the
  property setter) to avoid hmmlearn's shape-validation error on `diag` covariance
- Uniform-prior bug documented and guarded: `is_markov_uniform()` method on
  `Observation` detects std < 0.05 across Markov features and warns

---

## [2.1.0] - 2024-Q3

### Changed - 4-State HMM Regime Detection

#### `core/regime_engine.py`
- **n_components: 3 -> 4** for both the volatility and trend HMMs
- Volatility states: `low | med_low | med_high | high`  (was `low | medium | high`)
- Trend states: `strong_bear | bear | bull | strong_bull`  (was `bear | neutral | bull`)
- Volatile feature set expanded: vol HMM now uses 3 features (20-bar RV, 5-bar RV, |return|); trend HMM uses 3 features (return, 10-bar cum, 30-bar cum)
- Trend state labelling now sorts on feature index 2 (30-bar cumulative return) for cleaner strong_bull/strong_bear separation
- Min bars requirement: 80 (was 50) to support stable 4-state EM convergence
- Risk multiplier range extended: **[0.10, 1.75]** (was [0.25, 1.50])
- 4×4 = 16 distinct regime cells (was 9)

#### Downstream changes (label references only)
- `routers/regime.py`, `routers/pipeline.py` - label zips use `RegimeHMM.VOL_LABELS` / `RegimeHMM.TREND_LABELS`
- `routers/stream_rest.py`, `routers/stream_ws.py` - same label zip update
- `models/schemas.py`, `models/stream_schemas.py` - `min_length` / `ge` validators updated to 81
- `services/stream_session.py` - `MIN_PRICES_FOR_FIT` updated from 51 -> 81

#### Added
- `test_platform_v4.py` - 4-state smoke test verifying 4 prob vectors, 16-cell multiplier table, and correct assertions

---

# Changelog

All notable changes to the Dynamic Regime Risk Management Platform are documented here.
Follows [Semantic Versioning](https://semver.org/).

---

## [2.0.0] - 2024-Q2

### Added - Live Streaming Layer

#### `services/stream_session.py` - `StreamSession`
- Per-symbol stateful streaming engine with rolling price deque (configurable window, default 500 bars)
- Background HMM re-fit via `asyncio.run_in_executor` every N bars (default 50) - non-blocking
- 3-bar alert debounce: regime must persist for 3 consecutive bars before a `RegimeAlert` fires
- NaN/zero-price guard: invalid prices are filtered from the buffer before every HMM call
- Multiple concurrent subscriber queues per session (bounded at 500 items; slow consumers drop ticks)
- `RegimeAlert` dataclass with `severity` field: `info` | `warning` | `critical`

#### `services/registry.py` - `SessionRegistry`
- Singleton session store (`asyncio.Lock`-protected)
- `get_or_create(symbol, ...)` - idempotent session creation with per-session config
- `stats()` - returns live metrics for all sessions (ready, n_bars, refit_count, tick_count, last_regime)

#### `routers/stream_rest.py` - REST streaming endpoints
- `POST /stream/seed` - seed a symbol session with historical prices
- `POST /stream/tick` - push one price tick, receive `TickResponse` or buffering stub
- `POST /stream/ticks` - batch tick ingestion / backtest replay
- `GET /stream/sessions` - list all active sessions + stats
- `GET /stream/session/{symbol}` - single session status
- `DELETE /stream/session/{symbol}` - remove a session

#### `routers/stream_ws.py` - WebSocket + SSE endpoints
- `WS /ws/{symbol}` - full-duplex WebSocket: seed / tick / subscribe / ping message types
- `GET /sse/{symbol}` - SSE stream pushing `TickResponse` JSON on every bar
- `GET /sse/alerts` - global SSE broadcast for all regime-change alerts across all symbols
- 30-second heartbeat in subscriber mode; 15-second SSE comment heartbeat

#### `models/stream_schemas.py` - Streaming Pydantic models
- `SeedRequest`, `TickIngest`, `TickResponse`, `SessionStatus`, `WsMessage`

#### `main_v2.py`
- Updated app factory including all streaming routers alongside existing batch routers

#### `test_streaming.py`
- Async smoke test: seed -> 160-tick stream -> subscriber queue -> regime transition assertions

### Fixed

- `RegimeHMM` covariance type changed from `"full"` to `"diag"` for numerical stability on short rolling windows (< 300 bars)
- NaN/zero-price guard added to `_refit()` and `_compute_tick()` in `StreamSession`
- Test data continuity bug fixed (live price segment now correctly continues from seed tail)

---

## [1.0.0] - 2024-Q1

### Added - Batch Risk Management API

#### `core/regime_engine.py` - `RegimeHMM`
- Dual 3-state Gaussian HMM for volatility and trend regime classification
- Forward-backward posterior probabilities (not Viterbi hard assignments)
- Semantic state labelling by sorting component means - no manual mapping
- `RegimeSnapshot` dataclass with `risk_multiplier` property [0.25 – 1.50]

#### `core/position_sizer.py` - `PositionSizer`
- Full Kelly Criterion: `f* = μ / σ²` (continuous log-return formula)
- Fractional Kelly: `f* × kelly_fraction` (default 0.5)
- Volatility-scaled Kelly: `fractional_f × (target_vol / realised_annual_vol)`
- Regime-gated sizing: `vol_scaled_f × risk_multiplier`
- Automatic diagnostic notes (over-Kelly, negative Sharpe, vol mismatch, regime warnings)

#### `core/risk_manager.py` - `RiskManager`
- Historical (empirical) VaR at 95% and 99%
- CVaR / Expected Shortfall at 95% and 99%
- Maximum drawdown (peak-to-trough)
- Annualised return, volatility, Sortino ratio, Calmar ratio
- Regime-adjusted max loss limit: `base_risk_limit × risk_multiplier`
- Suggested stop-loss (2× CVaR95) and take-profit (3:1 reward/risk)
- Risk budget utilisation: `recommended_f / max_safe_f`

#### `models/schemas.py` - Pydantic v2 models
- `PricePayload`, `RegimeRequest/Response`, `SizeRequest/Response`, `RiskRequest/Response`, `FullAnalysisRequest/Response`
- Field-level validation: `min_length=51`, `ge`, `le`, `gt`, all-positive price guard

#### Routers
- `POST /regime/detect`
- `POST /size/kelly`
- `POST /risk/analyse`
- `POST /analyse/full` - one-shot full pipeline

#### Infrastructure
- `main.py` - FastAPI app factory with CORS middleware
- `requirements.txt` - pinned dependency versions
- `test_platform.py` - batch pipeline smoke test with synthetic multi-regime prices

---

## Roadmap

### Planned for v2.1
- `POST /simulate/{symbol}` - ODE-based regime transition simulation (simupy integration)
- `GET /portfolio` - multi-symbol aggregated regime + risk summary
- Redis Streams persistence for price buffers (survive process restarts)
- JWT authentication middleware for WebSocket handshake

### Planned for v3.0
- GPU-accelerated HMM via `pomegranate` (PyTorch backend)
- Real-time OHLCV feed adapters (Alpaca, Interactive Brokers, Binance)
- Multi-asset correlation regime detection
- Drawdown-controlled portfolio optimisation layer
