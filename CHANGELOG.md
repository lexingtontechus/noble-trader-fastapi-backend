## [7.0.0] — 2026-05-24

### Added — Runtime Configuration Engine + Admin API

#### `core/runtime_config.py` — NEW FILE
- `RuntimeConfig` singleton with 30-second TTL in-memory cache backed by Supabase `system_config` table
- Resolution order: DB override (highest) → env var → hardcoded default (lowest)
- Async-safe via `asyncio.Lock` with double-checked locking pattern
- Type-strict getters: `get_float()`, `get_int()`, `get_bool()`, `get_str()`, `get_json()`
- `set_value()` with validation (type coercion, range check, enum check), DB persist, audit logging, and on-change callbacks
- `reset_value()` to revert a key to its hardcoded default
- `reload()` to force full cache refresh from DB (bypasses TTL)
- `_SyncProxy` / `sync_config` for non-async contexts (env var → default only, no DB)
- 148 default values registered across 8 categories (renko, execution, alpaca, risk, sizing, stream, auth, regime)

#### `routers/config.py` — NEW FILE
- 9 REST endpoints under `/config/`:
  - `GET /config/` — List all entries grouped by category
  - `GET /config/categories` — List category names
  - `GET /config/{category}` — Get entries for a category
  - `GET /config/key/{key}` — Get single entry
  - `PATCH /config/key/{key}` — Update value (admin only, validated)
  - `POST /config/key/{key}/reset` — Reset to default (admin only)
  - `GET /config/reload` — Force cache reload (admin only)
  - `GET /config/schema` — Full schema with types/ranges
  - `GET /config/audit` — Config change audit log (admin only)
- Sensitive value masking for non-admin readers
- Pydantic models: `ConfigValueUpdate`, `ConfigEntryResponse`, `ConfigCategoryResponse`, `ConfigListResponse`, `ConfigReloadResponse`, `ConfigAuditEntry`

#### `supabase/migrations/007_system_config.sql` — NEW FILE
- `system_config` table with JSONB values, type/category metadata, range constraints, RLS policies
- `system_config_audit` table with immutable append-only change log
- Auto-updating `updated_at` trigger
- Seeds for 139 config keys across renko, sizing, risk, regime, execution, stream, alpaca, auth

#### `supabase/migrations/008_system_config_extended.sql` — NEW FILE
- Adds 9 extended config keys: execution financing (4), fill probability internals (5), stream subscriber queue (1), Alpaca WS timeouts/throttling (10), risk stress test extended (3)
- Uses `ON CONFLICT (key) DO NOTHING` — safe to re-run

#### Documentation — NEW FILES
- `docs/ADMIN_CONFIG_API.md` — Full API reference for all 9 config endpoints
- `docs/ADMIN_CONFIG_KEY_CATALOG.md` — Complete catalog of all 148 config keys with types, defaults, ranges, and descriptions

### Fixed — Deployment Import Errors
- `routers/backtest.py` — Added missing `raise_http_exception` import
- `routers/regime.py` — Added missing `limiter`, `RATE_LIMIT_COMPUTE`, `raise_http_exception`, `Request` imports; fixed `@limiter.limit` decorator order
- `routers/risk.py` — Added missing `limiter`, `RATE_LIMIT_COMPUTE`, `RATE_LIMIT_DEFAULT`, `raise_http_exception`, `Request`, `Query`, `RiskDashboardResponse`; fixed decorator order
- `routers/sizing.py` — Added missing `limiter`, `RATE_LIMIT_COMPUTE`, `raise_http_exception`, `Request`, all Masaniello schema imports; fixed import sources
- `routers/strategy.py`, `routers/observation.py`, `routers/pipeline.py`, `routers/tda.py` — Added missing `raise_http_exception` import
- `routers/auth_router.py` — Added missing `import os`
- `routers/stream_ws.py` — Added missing `import logging` and `_log = logging.getLogger(__name__)`
- `renko/router.py` — Added `Request` import and `request: Request` parameter to 3 endpoints (`process_tick`, `process_batch`, `reset_pipeline`); fixed `@limiter.limit` decorator order
- `main.py` — Added missing `import os` (used by `os.getenv("CORS_ALLOWED_ORIGINS")` and `os.getenv("DEPLOY_ENV")`)

### Changed
- All backend hardcoded parameters are now runtime-configurable via `runtime_config.get_*()` calls
- Config changes propagate to all services within 30 seconds (TTL cache)
- Admin role required for all config mutations (`require_admin` dependency)
- Auth level summary updated: 4 admin-only config endpoints added (PATCH, POST reset, GET reload, GET audit)

---

## [3.4.0] — 2026-05-21

### Fixed — Auth Hardening (All 57 Endpoints Now Protected)

#### Critical: SSE Endpoints Had No Authentication
- `GET /sse/{symbol}` — was completely unauthenticated; now requires `get_authed_user`
- `GET /sse/alerts` — was completely unauthenticated; now requires `get_authed_user`
- These endpoints exposed real-time regime data, risk metrics, and position info to anyone

#### Renko Write Operations Upgraded from Viewer to Trader/Admin
- `POST /renko/tick` — `get_authed_user` → `require_write` (viewers can no longer inject ticks)
- `POST /renko/tick/batch` — `get_authed_user` → `require_write`
- `POST /renko/backtest/run` — `get_authed_user` → `require_write`
- `POST /renko/backtest/run/stream` — `get_authed_user` → `require_write`
- `POST /renko/backtest/optimize` — `get_authed_user` → `require_write`

#### `regime_platform/routers/stream_ws.py` — Updated
- Added `Depends` import and `get_authed_user`, `TokenData` imports from `jwt_auth`
- Both SSE endpoints now accept `user: TokenData = Depends(get_authed_user)`

#### `renko/router.py` — Updated
- All mutation endpoints upgraded to `require_write` dependency
- Updated docstring to reflect new auth levels

### Added — Clerk JWT Template System + Backend Enrichment

#### `regime_platform/auth/clerk_auth.py` — Updated
- New `_enrich_from_clerk_api(sub)` function: calls `GET /v1/users/{sub}` when JWT claims are null
- Extracts email, first_name, last_name, username from Clerk user profile
- Reads role from `private_metadata.role` (matching frontend convention)
- Results cached 5 minutes per user to limit Clerk API calls
- Integrated into `verify_clerk_token()`: when email/name/username are all null, enrichment triggers automatically
- This ensures `/auth/clerk/me` and `/auth/clerk/verify` always return complete user info

#### Frontend JWT Resolution
- `getClerkJWT()` in `fastapi-auth.js` now tries `"server"` JWT template first
- Falls back to default JWT if template not configured
- Updated all 7 `getClerkJWT()` locations across the codebase
- Template adds email, first_name, last_name, username, role claims to the JWT

### Changed
- Auth level summary: 4 public (by design), 27 any-role, 22 write (admin/trader), 4 admin-only
- All docs updated: API.md (v5.5.0), openapi.yaml (v5.5.0), CHANGELOG.md

---

## [3.3.0] — 2026-05-19

### Added — SSE Streaming for Progressive/Chunked Backtest Loading

#### `renko/pipeline.py` — Updated
- `backtest_chunked()` — new generator method that yields intermediate results after every N ticks
- Yields `progress` events with: chunk number, total chunks, percent complete, trades so far, running stats, bricks/ticks processed
- Final `complete` event matches the shape of `backtest()` return value for seamless compatibility
- Configurable `chunk_size` parameter (default 150 ticks)
- Supports `timestamps` and `regimes` parameters like the regular `backtest()`

#### `renko/router.py` — Updated
- `POST /renko/backtest/run/stream` — new SSE endpoint with `StreamingResponse`
- Streams `progress` → `progress` → ... → `complete` events as `data: {...}\n\n` SSE format
- `error` event type for pipeline failures
- Redis cache check on entry — if cache HIT, emits single `complete` event immediately
- Final complete result is cached in Redis (1h TTL) for subsequent non-streaming requests
- Rate-limited under `RATE_LIMIT_WRITE`
- Requires authentication via `get_authed_user`

#### `tests/test_renko_backtest.py` — Updated
- `TestRenkoBacktestChunked` — 6-test suite covering:
  - Progress events are yielded with correct count (>= 2 progress + 1 complete)
  - Each progress event has required keys (chunk, total_chunks, percent, trades_so_far, stats_so_far, etc.)
  - Percent increases monotonically across progress events
  - Complete event matches regular `backtest()` result (same total_ticks, total_bricks, trades count)
  - Last progress event has percent = 100
  - Chunked backtest accepts regime labels

### Changed
- Total backend test count: 25 Renko backtest tests (19 existing + 6 new chunked)

---

## [3.2.0] — 2026-05-19

### Added — Walk-Forward Backtest Engine + Persistence + Optimization

#### `core/backtester.py` — NEW FILE
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

#### `services/backtest_store.py` — NEW FILE
- Supabase persistence layer for backtest results
- `save_backtest_result()` — persist to `ta_backtest_result` table (user-scoped)
- `get_backtest_history()` — paginated history with summary metrics only
- `get_backtest_detail()` — full result by ID (user-scoped)
- `delete_backtest()` — delete by ID (user-scoped)
- Graceful degradation when Supabase is not configured

#### `routers/backtest.py` — NEW FILE
- `POST /backtest/run` — run walk-forward backtest (rate-limited, Clerk/JWT auth)
- `GET /backtest/history` — paginated list of saved results
- `GET /backtest/{id}` — full result by ID
- `POST /backtest/compare` — side-by-side comparison with diff summary
- `POST /backtest/optimize` — parameter grid sweep (max 50 combos)
- `POST /backtest/export` — CSV/JSON export with section selection
- `DELETE /backtest/{id}` — delete saved result

#### `models/schemas.py` — Updated
- `BacktestRequest` — extended with `dates` and `save` fields
- `BacktestResponse` — full response with 30+ metric fields
- `BacktestSummary` — lightweight summary for list views
- `BacktestHistoryResponse` — paginated summary list
- `BacktestCompareRequest` / `BacktestCompareResponse` — comparison + diff
- `BacktestOptimizeRequest` / `BacktestOptimizeResponse` — grid sweep
- `BacktestExportRequest` — export format + section selection

#### `supabase/migrations/00000000000007_backtest_results.sql` — NEW FILE
- `ta_backtest_result` table with RLS policies (user-scoped access)
- Summary columns + JSONB columns for heavy data (equity curve, trade log, etc.)

### Fixed
- `pnl_pct` was always returning 0.0 in backtest trade log — now correctly computed

### Changed
- All backtest endpoints require Clerk/JWT authentication
- Rate-limited under `RATE_LIMIT_COMPUTE`

---

## [3.1.0] — 2026-Q2

### Changed — Dynamic Masaniello + Kelly Unified Sizing Engine

#### `core/position_sizer.py` — Full Refactor

Two complementary sizing paths now live in the same module, independently usable.

**Path A — Portfolio-fraction Kelly (preserved, interface unchanged)**
`PositionSizer`, `PositionRequest`, `KellyResult` — stateless, returns-based.
All existing callers continue to work without modification.

**Path B — Dynamic Masaniello Sizer (new)**

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
| `.size_from_snapshot(snap, ...)` | Convenience wrapper — builds context + sizes |
| `.run_batch(contexts, outcomes, pnls)` | Size N trades sequentially with state injection |
| `monte_carlo_batch(sizer, ctx, n)` | Simulate N independent batches; return PnL/DD distribution |

**Pure factor functions (testable in isolation):**
`_masaniello_factor`, `_quality_factor`, `_drawdown_factor`, `_volatility_factor`,
`_fractional_kelly`, `_expected_edge`

**Factor details:**

`M_i = clip((W - w) / (N - i + 1), 0.0, 1.5)`
— Wins needed divided by trades remaining. Zero when target already met.
— `(0.5 + M_i)` term guarantees minimum 0.5× base risk even when M_i = 0.

`Q_i = prob_factor × regime_factor × conf_factor`
— `prob_factor  = clip((p_win - min_prob) / 0.10, 0.0, 1.5)`
— `regime_factor = clip(regime_quality, 0.0, 1.5)`  ← from `RegimeSnapshot.risk_multiplier`
— `conf_factor   = clip(state_confidence, 0.0, 1.0)` ← from `RegimeSnapshot.confidence`
— Q_i = 0 when any sub-factor is zero; acts as a quality gate.

`DD_i = clip(1 - dd / max_dd, 0.25, 1.0)`
— Floor at 0.25 preserves minimum position participation during drawdown.

`V_i = clip(ATR_baseline / ATR_current, 0.5, 1.5)`
— Inverted ATR ratio. High vol → shrink; low vol → mild expansion.

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
`risk_fraction = clip(f_i, min_risk, max_risk)` — always enforced last.

#### `test_position_sizer.py` — NEW FILE
95-test suite covering:
- Factor math boundary conditions (all 4 factors + Kelly + edge)
- Core formula with known inputs (manual verification)
- Gate ordering — each block reason fires correctly
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

## [3.0.0] — 2025-Q1

### Added — GPU HMM · Live Feeds · Correlation Regimes · Portfolio Optimisation

#### `core/gpu_engine.py` — NEW FILE
- `GpuRegimeHMM` — pomegranate PyTorch-backed 4-state HMM, identical interface to `RegimeHMM`
- `create_regime_hmm(force_cpu)` — auto-selecting factory: GPU if pomegranate + CUDA/MPS available, else hmmlearn CPU
- `gpu_capabilities()` — returns `{pomegranate_installed, cuda_available, mps_available, active_device, hmm_backend}`
- Transparent CPU fallback — zero code changes required when GPU unavailable
- Supports CUDA (NVIDIA), MPS (Apple Silicon), and CPU targets

#### `adapters/feed_adapters.py` — NEW FILE
- `FeedAdapter` — abstract base with exponential-backoff reconnect (max 10 attempts)
- `AlpacaFeedAdapter` — alpaca-py WebSocket, supports SIP and IEX feeds, equities + crypto
- `BinanceFeedAdapter` — native WebSocket kline stream, any interval (1m/5m/1h/...), mainnet + testnet
- `IBFeedAdapter` — ib_async TWS API, real-time 5-second bars, equities/futures/FX
- `OHLCVBar` — unified OHLCV dataclass with `source` field
- `FeedManager` — multi-adapter orchestrator, routes bars into `SessionRegistry`, chainable `.add()`

#### `core/correlation_regime.py` — NEW FILE
- `CorrelationRegimeDetector` — DCC-based portfolio-level correlation regime classifier
- Extracts rolling Pearson correlation matrices (configurable window, default 60 bars)
- Applies EWMA vol standardisation before correlation computation
- Fits a 4-state Gaussian HMM on upper-triangle correlation feature vectors
- States labelled by mean |ρ|: `low_corr` | `mid_corr` | `high_corr` | `crisis`
- `CorrelationSnapshot` — per-asset breakdowns + `blended_risk_multiplier` = mean(asset_mults) × corr_mult
- Correlation risk multipliers: `low_corr=1.00` · `mid_corr=0.85` · `high_corr=0.60` · `crisis=0.35`

#### `core/portfolio_optimiser.py` — NEW FILE
- `DrawdownOptimiser` — regime-aware max-Sharpe subject to drawdown constraint
- Three regime layers: (1) regime-adjusted μ, (2) per-asset weight bounds, (3) correlation exposure
- Drawdown constraint via Cornish-Fisher CVaR proxy (analytic — no simulation required)
- Correlation exposure scalar: `low_corr=1.00` · `mid_corr=0.90` · `high_corr=0.70` · `crisis=0.50`
- Weight bounds tightened per-asset: `high_vol_strong_bear → max_weight × 0.25`
- Scipy SLSQP solver with configurable `max_iter`, `tol`, and `dd_penalty_lambda`
- `OptimisationResult` — weights, regime_adj_weights, expected return/vol/Sharpe/DD, convergence info

#### `routers/gpu.py` — NEW FILE
- `GET /gpu/capabilities` — hardware and backend report
- `POST /gpu/benchmark` — fit + predict latency benchmark on synthetic data, `force_cpu` flag

#### `routers/feeds.py` — NEW FILE
- `POST /feeds/start` — register and start feed adapters as background asyncio tasks
- `POST /feeds/stop` — graceful shutdown of all adapter tasks
- `GET /feeds/status` — adapter health + per-symbol bar counts

#### `routers/multi_asset.py` — NEW FILE
- `POST /correlation/detect` — fit DCC correlation HMM + return correlation regime snapshot
- `POST /optimise` — solve drawdown-controlled max-Sharpe with regime overlays
- `POST /optimise/full` — one-shot: correlation detect + portfolio optimise

#### `main_v4.py` — NEW FILE
- v3.0 app factory with GPU capability logging on startup
- `GET /health` now includes `gpu_backend` and `device` fields

### Changed
- `requirements.txt` — added optional GPU and feed adapter dependency comments
- `core/portfolio_optimiser.py` — fixed array ambiguity bug in `neg_sharpe_with_dd` (scipy `max()` applied to matrix)

### Architecture summary

```
GET  /gpu/capabilities       pomegranate install check → CUDA/MPS/CPU selection
POST /gpu/benchmark          synthetic fit → latency report

POST /feeds/start            FeedManager → AlpacaFeedAdapter/BinanceFeedAdapter/IBFeedAdapter
                             → SessionRegistry.get(symbol).tick(close, ts)

POST /correlation/detect     rolling corr features → 4-state HMM → corr regime label
POST /optimise               regime-adj μ → SLSQP → DD-constrained weights
POST /optimise/full          detect + optimise in one request
```

---

## [2.1.0] — 2024-Q4

### Added — Simulation · Portfolio · Redis Persistence · JWT Auth

#### `core/simulator.py` — NEW FILE
- `RegimeSimulator` — Markov-chain Monte Carlo regime transition simulator
- Builds a 16×16 combined transition matrix via Kronecker product of the fitted
  4-state vol and trend HMM transition matrices: `T_combined = T_vol ⊗ T_trend`
- Fits per-regime (μ, σ) return distributions from Viterbi-decoded historical paths
- Simulates `n_paths` forward trajectories of `horizon` steps with regime switching
- Returns: price fan (p5/p25/median/p75/p95), expected risk multiplier per step,
  terminal return distribution (mean, std, VaR95, CVaR95), most likely terminal
  regime, % paths with positive return, mean maximum drawdown across paths

#### `services/portfolio_service.py` — NEW FILE
- `PortfolioService` — aggregates regime + risk analytics across all active sessions
- Per-symbol: regime label, confidence, risk_multiplier, Kelly f*, VaR95, Sharpe
- Portfolio-level: dominant regime, regime consensus, avg risk multiplier,
  portfolio VaR (√ΣVaR² — independence assumption), portfolio CVaR
- Risk flags: `high_risk_count`, `concentration_flag`, `regime_divergence_flag`
- Regime divergence detection via normalised entropy of vol_state distribution

#### `services/redis_persistence.py` — NEW FILE
- `RedisPersistence` — optional Redis Streams price buffer persistence layer
- All methods are no-ops when `REDIS_URL` env var is not set (zero overhead)
- `restore(session)` — hydrates session price buffer from Redis on startup
- `append_price(symbol, price)` — appends to Redis Stream with MAXLEN cap
- `save_meta / load_meta` — stores session config (kelly_fraction, etc.)
- `list_persisted_symbols()` — returns all symbols with persisted streams
- Failure-tolerant: Redis errors never raise in the tick path (log + continue)

#### `auth/jwt_auth.py` — NEW FILE
- `create_access_token(data)` — creates signed HS256 JWT
- `decode_token(token)` — validates and decodes JWT → `TokenData`
- `get_current_user` — FastAPI Depends for HTTP endpoint auth
- `require_write` — Depends requiring trader or admin role
- `require_admin` — Depends requiring admin role
- `ws_auth(websocket, token, api_key)` — WebSocket auth (closes 4001 on failure)
- `make_login_response(sub, role)` — standard token response helper
- API key fallback via `X-API-Key` header and `API_KEYS` env var
- `AUTH_ENABLED=false` disables auth entirely for development

#### `routers/simulate.py` — NEW FILE
- `POST /simulate/{symbol}` — fits HMM + simulates regime transition paths
- Parameters: `horizon` (1–252), `n_paths` (50–5000), `seed`, `current_price`

#### `routers/portfolio.py` — NEW FILE
- `GET /portfolio` — aggregated portfolio risk view across all active sessions
- Query params: `symbols` (CSV), `kelly_fraction`, `target_vol`, `base_risk_limit`
- `GET /portfolio/symbols` — list all seeded symbols

#### `routers/auth_router.py` — NEW FILE
- `POST /auth/token` — OAuth2 password flow, issues JWT
- `GET /auth/me` — returns current user info from token
- `POST /auth/refresh` — issues a fresh token (same sub/role)
- User store configured via `AUTH_USERS` env var (user:pass:role CSV)

#### `main_v3.py` — NEW FILE
- v2.1 app factory with `lifespan` context manager
- On startup: restores persisted sessions from Redis, re-fits HMMs
- All v2.0 endpoints unchanged; v2.1 routers added alongside
- `/health` returns Redis status and active session count

### Changed
- `requirements.txt` — added `python-jose[cryptography]`, `passlib[bcrypt]`, `redis`

### Architecture summary

```
POST /simulate/{symbol}    HMM fit → 16-state Kronecker system → Monte Carlo
GET  /portfolio            SessionRegistry → per-symbol regime + risk → aggregate
POST /auth/token           credential check → JWT issue
WS   /ws/{symbol}          (auth-ready: pass ?token=<jwt> to authenticate)
REDIS_URL (env)            on startup → restore buffers → re-fit HMMs
```

---

## [2.2.0] — 2024-Q3

### Added — 24-Feature Observation Vector & Stateful Builder

#### `core/obs_builder.py` — NEW FILE
- `InferenceObservationBuilder` — stateful class that maintains a fitted `RegimeHMM`
  across calls, ensuring Markov features (indices 14–19) are always computed from
  live HMM posteriors, never from a uniform prior
- `F` — feature index namespace (single source of truth for all 24 indices)
- `Observation` — output dataclass with `.markov_features` slice property and
  `.is_markov_uniform(tol)` guard method
- `build(prices, high, low)` — full-window build; re-fits HMM every `refit_every` bars
- `build_from_tick(price, high, low)` — streaming single-bar variant
- `seed(prices, high, low)` — historical warm-up and initial HMM fit
- `update_recommended_f(f)` — injects current Kelly fraction for Masaniello feature

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
| **14** | **`vol_prob_low`** | P(low vol) — Markov feature |
| **15** | **`vol_prob_medium`** | P(med_low + med_high) — Markov feature |
| **16** | **`vol_prob_high`** | P(high vol) — Markov feature |
| **17** | **`trend_prob_down`** | P(strong_bear + ½·bear) — Markov feature |
| **18** | **`trend_prob_neutral`** | P(½·bear + ½·bull) — Markov feature |
| **19** | **`trend_prob_up`** | P(½·bull + strong_bull) — Markov feature |
| 20 | `regime_quality` | HMM confidence scalar |
| 21 | `state_confidence` | max(vol_probs) × max(trend_probs) |
| 22 | `masaniello_pressure` | f* × (1 − f*) — peaks at 0.25 when f=0.5 |
| 23 | `drawdown_factor` | Current DD from peak ∈ [−1, 0] |

#### `core/regime_engine.py` — Updated
- Added `_sanitize_model()` static method: fixes NaN `startprob_`, zero `transmat_`
  rows, NaN `means_`, and degenerate `_covars_` after EM convergence failure
- `fit()` calls `_sanitize_model()` on both sub-models after every EM run
- `predict()` adds NaN fallback: degenerate posteriors replaced with uniform, then
  re-normalised — guarantees finite output even on pathological price series

#### `test_obs_builder.py` — NEW FILE
- 7-test suite: shape, finite values, Markov non-uniformity, Masaniello response,
  drawdown sign, streaming tick path, and all-zero sentinel guard

### Fixed
- `RegimeHMM._sanitize_model()` writes to `_covars_` private attribute (not the
  property setter) to avoid hmmlearn's shape-validation error on `diag` covariance
- Uniform-prior bug documented and guarded: `is_markov_uniform()` method on
  `Observation` detects std < 0.05 across Markov features and warns

---

## [2.1.0] — 2024-Q3

### Changed — 4-State HMM Regime Detection

#### `core/regime_engine.py`
- **n_components: 3 → 4** for both the volatility and trend HMMs
- Volatility states: `low | med_low | med_high | high`  (was `low | medium | high`)
- Trend states: `strong_bear | bear | bull | strong_bull`  (was `bear | neutral | bull`)
- Volatile feature set expanded: vol HMM now uses 3 features (20-bar RV, 5-bar RV, |return|); trend HMM uses 3 features (return, 10-bar cum, 30-bar cum)
- Trend state labelling now sorts on feature index 2 (30-bar cumulative return) for cleaner strong_bull/strong_bear separation
- Min bars requirement: 80 (was 50) to support stable 4-state EM convergence
- Risk multiplier range extended: **[0.10, 1.75]** (was [0.25, 1.50])
- 4×4 = 16 distinct regime cells (was 9)

#### Downstream changes (label references only)
- `routers/regime.py`, `routers/pipeline.py` — label zips use `RegimeHMM.VOL_LABELS` / `RegimeHMM.TREND_LABELS`
- `routers/stream_rest.py`, `routers/stream_ws.py` — same label zip update
- `models/schemas.py`, `models/stream_schemas.py` — `min_length` / `ge` validators updated to 81
- `services/stream_session.py` — `MIN_PRICES_FOR_FIT` updated from 51 → 81

#### Added
- `test_platform_v4.py` — 4-state smoke test verifying 4 prob vectors, 16-cell multiplier table, and correct assertions

---

# Changelog

All notable changes to the Dynamic Regime Risk Management Platform are documented here.
Follows [Semantic Versioning](https://semver.org/).

---

## [2.0.0] — 2024-Q2

### Added — Live Streaming Layer

#### `services/stream_session.py` — `StreamSession`
- Per-symbol stateful streaming engine with rolling price deque (configurable window, default 500 bars)
- Background HMM re-fit via `asyncio.run_in_executor` every N bars (default 50) — non-blocking
- 3-bar alert debounce: regime must persist for 3 consecutive bars before a `RegimeAlert` fires
- NaN/zero-price guard: invalid prices are filtered from the buffer before every HMM call
- Multiple concurrent subscriber queues per session (bounded at 500 items; slow consumers drop ticks)
- `RegimeAlert` dataclass with `severity` field: `info` | `warning` | `critical`

#### `services/registry.py` — `SessionRegistry`
- Singleton session store (`asyncio.Lock`-protected)
- `get_or_create(symbol, ...)` — idempotent session creation with per-session config
- `stats()` — returns live metrics for all sessions (ready, n_bars, refit_count, tick_count, last_regime)

#### `routers/stream_rest.py` — REST streaming endpoints
- `POST /stream/seed` — seed a symbol session with historical prices
- `POST /stream/tick` — push one price tick, receive `TickResponse` or buffering stub
- `POST /stream/ticks` — batch tick ingestion / backtest replay
- `GET /stream/sessions` — list all active sessions + stats
- `GET /stream/session/{symbol}` — single session status
- `DELETE /stream/session/{symbol}` — remove a session

#### `routers/stream_ws.py` — WebSocket + SSE endpoints
- `WS /ws/{symbol}` — full-duplex WebSocket: seed / tick / subscribe / ping message types
- `GET /sse/{symbol}` — SSE stream pushing `TickResponse` JSON on every bar
- `GET /sse/alerts` — global SSE broadcast for all regime-change alerts across all symbols
- 30-second heartbeat in subscriber mode; 15-second SSE comment heartbeat

#### `models/stream_schemas.py` — Streaming Pydantic models
- `SeedRequest`, `TickIngest`, `TickResponse`, `SessionStatus`, `WsMessage`

#### `main_v2.py`
- Updated app factory including all streaming routers alongside existing batch routers

#### `test_streaming.py`
- Async smoke test: seed → 160-tick stream → subscriber queue → regime transition assertions

### Fixed

- `RegimeHMM` covariance type changed from `"full"` to `"diag"` for numerical stability on short rolling windows (< 300 bars)
- NaN/zero-price guard added to `_refit()` and `_compute_tick()` in `StreamSession`
- Test data continuity bug fixed (live price segment now correctly continues from seed tail)

---

## [1.0.0] — 2024-Q1

### Added — Batch Risk Management API

#### `core/regime_engine.py` — `RegimeHMM`
- Dual 3-state Gaussian HMM for volatility and trend regime classification
- Forward-backward posterior probabilities (not Viterbi hard assignments)
- Semantic state labelling by sorting component means — no manual mapping
- `RegimeSnapshot` dataclass with `risk_multiplier` property [0.25 – 1.50]

#### `core/position_sizer.py` — `PositionSizer`
- Full Kelly Criterion: `f* = μ / σ²` (continuous log-return formula)
- Fractional Kelly: `f* × kelly_fraction` (default 0.5)
- Volatility-scaled Kelly: `fractional_f × (target_vol / realised_annual_vol)`
- Regime-gated sizing: `vol_scaled_f × risk_multiplier`
- Automatic diagnostic notes (over-Kelly, negative Sharpe, vol mismatch, regime warnings)

#### `core/risk_manager.py` — `RiskManager`
- Historical (empirical) VaR at 95% and 99%
- CVaR / Expected Shortfall at 95% and 99%
- Maximum drawdown (peak-to-trough)
- Annualised return, volatility, Sortino ratio, Calmar ratio
- Regime-adjusted max loss limit: `base_risk_limit × risk_multiplier`
- Suggested stop-loss (2× CVaR95) and take-profit (3:1 reward/risk)
- Risk budget utilisation: `recommended_f / max_safe_f`

#### `models/schemas.py` — Pydantic v2 models
- `PricePayload`, `RegimeRequest/Response`, `SizeRequest/Response`, `RiskRequest/Response`, `FullAnalysisRequest/Response`
- Field-level validation: `min_length=51`, `ge`, `le`, `gt`, all-positive price guard

#### Routers
- `POST /regime/detect`
- `POST /size/kelly`
- `POST /risk/analyse`
- `POST /analyse/full` — one-shot full pipeline

#### Infrastructure
- `main.py` — FastAPI app factory with CORS middleware
- `requirements.txt` — pinned dependency versions
- `test_platform.py` — batch pipeline smoke test with synthetic multi-regime prices

---

## Roadmap

### Planned for v2.1
- `POST /simulate/{symbol}` — ODE-based regime transition simulation (simupy integration)
- `GET /portfolio` — multi-symbol aggregated regime + risk summary
- Redis Streams persistence for price buffers (survive process restarts)
- JWT authentication middleware for WebSocket handshake

### Planned for v3.0
- GPU-accelerated HMM via `pomegranate` (PyTorch backend)
- Real-time OHLCV feed adapters (Alpaca, Interactive Brokers, Binance)
- Multi-asset correlation regime detection
- Drawdown-controlled portfolio optimisation layer
