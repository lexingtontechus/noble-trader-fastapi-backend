# Renko HFT Pipeline — Noble Trader Agent

A 6-layer Renko-based high-frequency trading pipeline that processes price ticks through brick construction, swing classification, pattern detection, signal filtering, order execution, and risk management.

## Architecture

```
Tick → Brick Engine → Swing Classifier → Pattern Detector
     → Signal Filter → Risk Manager → Executor → Trade Journal
```

| Layer | Module | Purpose |
|-------|--------|---------|
| 1 | `brick_engine.py` | Renko brick construction with 2-brick reversal rule |
| 2 | `swing_classifier.py` | Labels bricks HH/HL/LH/LL, detects swing points |
| 3 | `pattern_detector.py` | Detects bull_run, bear_run, reversal, double_top, double_bottom, consolidation_break |
| 4 | `signal_filter.py` | 7-layer risk governance: session window, max trades, daily loss, consecutive losses, cooldown, velocity, HMM regime gate |
| 5 | `risk_manager.py` | Brick-denominated SL/TP/trailing stop/time-stop/partial-exit |
| 6 | `executor.py` | Alpaca order executor with bracket orders (dry-run simulation mode when no API keys) |

Supporting modules:
- `pipeline.py` — Orchestrates the full pipeline flow
- `trade_journal.py` — Records trades, computes win rate, profit factor, Sharpe, max drawdown, Kelly estimates
- `config.py` — `RenkoConfig` dataclass with 30+ tunable parameters
- `router.py` — 22 FastAPI REST endpoints
- `integration.py` — Bridge to Noble Trader Agent backend (regime/Kelly) and Supabase trade push
- `backtest.py` / `tests.py` — Backtesting and unit tests

## API Endpoints

All endpoints are mounted under `/renko/` on the FastAPI backend.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/renko/health` | Diagnostic health check (no auth required) | None |
| `POST` | `/renko/tick` | Process single tick through pipeline | Write |
| `POST` | `/renko/tick/batch` | Batch tick processing (warm-up/backtest) | Write |
| `GET` | `/renko/state` | Current pipeline state snapshot | Any |
| `GET` | `/renko/stats` | Comprehensive statistics | Any |
| `GET` | `/renko/bricks` | Recent Renko bricks (last N) | Any |
| `GET` | `/renko/classified` | Bricks with swing labels | Any |
| `GET` | `/renko/signals` | Pattern signals | Any |
| `GET` | `/renko/trades` | Trade journal entries | Any |
| `GET` | `/renko/swing-points` | Detected swing points | Any |
| `POST` | `/renko/regime` | Update HMM regime for gate filter | Write |
| `POST` | `/renko/equity` | Update account equity | Write |
| `POST` | `/renko/config` | Update pipeline configuration (resets pipeline) | Admin |
| `POST` | `/renko/reset` | Reset pipeline for a symbol | Admin |
| `GET` | `/renko/backtest/stats` | Backtest/trading statistics | Any |
| `POST` | `/renko/backtest/run` | Full backtest with Phase 2-7 analytics | Write |
| `POST` | `/renko/backtest/run/stream` | SSE streaming backtest with progressive results | Write |
| `POST` | `/renko/backtest/optimize` | Grid-search parameter sweep with statistical rigor (max 50 combos) | Write |
| `POST` | `/renko/backtest/compare` | Compare 2-10 Renko configs side-by-side | Any |
| `POST` | `/renko/statistics/rigor` | Statistical rigor analysis (bootstrap, Monte Carlo) | Any |
| `POST` | `/renko/execution/model` | Execution modeling (slippage, fill simulation) | Any |
| `GET` | `/renko/live/status` | Check live execution mode status | Any |
| `POST` | `/renko/live/toggle` | Enable/disable live execution | Admin |
| `POST` | `/renko/snapshot/restore` | Restore pipeline from Supabase snapshot | Write |

### Backtest Endpoints (Isolated — Never Affect Live Pipeline)

These endpoints create standalone `RenkoPipeline` instances for each request. They never read from or write to the live pipeline registry, making them safe for experimentation and parameter tuning.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/renko/backtest/run` | Run a full Renko pipeline backtest on historical prices | Any role |
| `POST` | `/renko/backtest/compare` | Compare 2–10 Renko configs side-by-side on the same price data | Any role |
| `POST` | `/renko/backtest/optimize` | Grid-search parameter sweep for Renko pipeline optimisation (max 50 combos) | Any role |

All three endpoints are rate-limited under `RATE_LIMIT_WRITE` and executed in thread pools via `asyncio.to_thread` to avoid blocking the event loop.

#### POST /renko/backtest/run

Run a complete backtest through the full 6-layer pipeline on historical prices. Accepts all `RenkoConfig` parameters plus the price series and optional timestamps/regimes/signal_confidence_min.

**Request body (`RenkoBacktestRequest`):**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Historical price series (min 50 ticks) |
| `symbol` | `string` | `"SPY"` | Trading symbol |
| `brick_size` | `float` | `0.50` | Brick size (>0) |
| `brick_size_mode` | `string` | `"fixed"` | `"fixed"`, `"atr"`, or `"dynamic"` |
| `reversal_bricks` | `int` | `2` | Reversal threshold (≥1) |
| `bull_trigger_n` | `int` | `3` | Bull trigger (≥1) |
| `bear_trigger_n` | `int` | `3` | Bear trigger (≥1) |
| `sl_bricks` | `int` | `3` | Stop-loss bricks (≥1) |
| `tp_bricks` | `int` | `5` | Take-profit bricks (≥1) |
| `trailing_stop` | `bool` | `true` | Trailing stop enabled |
| `trail_after_bricks` | `int` | `3` | Trail after N bricks profit |
| `trail_distance_bricks` | `int` | `2` | Trail distance in bricks |
| `max_trades_per_session` | `int` | `15` | Max trades per session |
| `max_daily_loss_bricks` | `float` | `10.0` | Max daily loss in bricks |
| `max_consecutive_losses` | `int` | `3` | Max consecutive losses |
| `cooldown_seconds` | `float` | `30.0` | Min seconds between trades |
| `regime_gate` | `bool` | `true` | HMM regime alignment gate |
| `timestamps` | `list[float]` | `null` | Matching timestamps |
| `regimes` | `list[str]` | `null` | Pre-computed regime labels |
| `signal_confidence_min` | `float` | `null` | Min confidence to trade (0–1) |

**Response (`RenkoBacktestResponse`):**

```json
{
  "symbol": "SPY",
  "total_ticks": 5000,
  "total_bricks": 342,
  "config_used": { "brick_size": 0.50, "...": "..." },
  "stats": {
    "total_trades": 28,
    "win_rate": 0.607,
    "profit_factor": 2.35,
    "total_pnl_bricks": 12.5,
    "sharpe_estimate": 1.42,
    "max_drawdown_bricks": 5.0,
    "avg_pnl_bricks": 0.45,
    "...": "per-pattern, per-regime, Kelly inputs"
  },
  "trades": ["...full trade records..."]
}
```

#### POST /renko/backtest/compare

Run multiple labelled configs on the same price data and compare results. Returns a diff summary of key metrics (first config vs best by Sharpe estimate).

**Request body (`RenkoBacktestCompareRequest`):**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Historical price series (min 50 ticks) |
| `symbol` | `string` | `"SPY"` | Trading symbol |
| `configs` | `list[RenkoCompareConfig]` | *required* | 2–10 labelled configs |
| `timestamps` | `list[float]` | `null` | Matching timestamps |
| `regimes` | `list[str]` | `null` | Pre-computed regime labels |

Each `RenkoCompareConfig` has a required `label` field plus standard pipeline params (brick_size, sl_bricks, tp_bricks, trailing_stop, trail_after_bricks, trail_distance_bricks, bull_trigger_n, bear_trigger_n, regime_gate, max_trades_per_session, max_daily_loss_bricks, max_consecutive_losses).

**Response (`RenkoBacktestCompareResponse`):**

```json
{
  "comparisons": ["...RenkoBacktestResponse per config..."],
  "diff": {
    "total_trades": { "first": 28, "best": 28, "delta": 0 },
    "win_rate": { "first": 0.65, "best": 0.65, "delta": 0.0 },
    "profit_factor": { "first": 2.85, "best": 2.85, "delta": 0.0 },
    "total_pnl_bricks": { "first": 14.5, "best": 14.5, "delta": 0.0 },
    "avg_pnl_bricks": { "first": 0.52, "best": 0.52, "delta": 0.0 },
    "max_drawdown_bricks": { "first": 4.0, "best": 4.0, "delta": 0.0 },
    "sharpe_estimate": { "first": 1.52, "best": 1.52, "delta": 0.0 }
  }
}
```

#### POST /renko/backtest/optimize

Grid-search parameter sweep across a `param_grid` of parameter names → value lists. Each combination runs a full isolated backtest. Max 50 combinations per request.

**Request body (`RenkoBacktestOptimizeRequest`):**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Historical price series (min 50 ticks) |
| `symbol` | `string` | `"SPY"` | Trading symbol |
| `param_grid` | `dict[str, list]` | *required* | Map of parameter → values to sweep. Keys: `brick_size`, `sl_bricks`, `tp_bricks`, `bull_trigger_n`, `bear_trigger_n`, `trailing_stop`, `trail_after_bricks`, `trail_distance_bricks`, `regime_gate`, `max_trades_per_session`, `max_daily_loss_bricks`, `max_consecutive_losses` |
| `brick_size` | `float` | `0.50` | Fixed default brick size |
| `...` | `...` | `...` | All other `RenkoConfig` params as fixed defaults |
| `timestamps` | `list[float]` | `null` | Matching timestamps |
| `regimes` | `list[str]` | `null` | Pre-computed regime labels |

**Response (`RenkoBacktestOptimizeResponse`):**

```json
{
  "results": [
    {
      "params": { "brick_size": 0.25, "sl_bricks": 2, "tp_bricks": 4 },
      "total_pnl_bricks": 8.5,
      "win_rate": 0.55,
      "sharpe_estimate": 1.15,
      "profit_factor": 1.85,
      "max_drawdown_bricks": 6.0,
      "total_trades": 42
    }
  ],
  "best_by_sharpe": { "...best combo by Sharpe..." },
  "best_by_return": { "...best combo by total PnL..." },
  "n_combinations": 18
}
```

The backend maintains an in-memory pipeline registry: one `RenkoPipeline` instance per symbol. Pipelines are created on first request and persist for the lifetime of the server process.

## Data Persistence & Freshness

### Supabase Cache (`ta_renko_snapshot`)

Warm-up results are saved to Supabase for instant loading on subsequent visits. Each row is keyed by `(symbol, brick_size)` and stores:

- `bricks` (JSONB) — Renko brick array
- `classified` (JSONB) — Bricks with swing labels
- `signals` (JSONB) — Pattern signals
- `trades` (JSONB) — Trade records
- `stats` (JSONB) — Pipeline statistics
- `config` (JSONB) — Pipeline configuration
- `price_range` (JSONB) — Min/max prices from warm-up data
- `updated_at` (TIMESTAMPTZ) — Last refresh timestamp

### Cache TTL: 4 Hours

Staleness is calculated as `age = now - updated_at`. Data older than 4 hours is considered **stale**.

### Data Flow Decision Tree

```
+---------------------------------------------------+
|              Load Renko Data                       |
|  (Page load / Symbol switch / Manual Warm Up)     |
+------------------------+--------------------------+
                         |
                         v
              +----------------------+
              | Supabase Cache Check |
              | GET /api/renko/warmup|
              +----------+-----------+
                         |
              +----------+----------+
              |         |           |
              v         v           v
           FRESH       STALE     MISSING
           (<4h)       (>4h)     (no row)
              |         |           |
              v         v           v
        Show instantly  Show old    Backend fetch
        DONE            data NOW   /renko/state
                        + fire         |
                        background     v
                        warmup    +-----------+
                       (non-      | Has data? |
                        blocking) +-----+-----+
                                        |
                                 +------+------+
                                 v             v
                                YES           NO
                                 |             |
                                 v             v
                           Show from      Auto-warmup
                           pipeline       (Yahoo Finance
                           DONE           -> feed pipeline
                                          -> save Supabase)
```

### Trigger Behavior Matrix

| Trigger | Fresh Cache (<4h) | Stale Cache (>4h) | No Cache |
|---------|-------------------|-------------------|----------|
| **Page Load** | Instant from Supabase | Old data shown + background refresh | Backend fetch -> auto-warm if empty |
| **Symbol Switch** | Instant from Supabase | Old data shown + background refresh | Backend fetch -> auto-warm if empty |
| **Warm Up Button** | Returns cached (skips Yahoo fetch) | Re-feeds Yahoo -> saves new snapshot | Feeds Yahoo -> saves snapshot |
| **5s Auto-refresh** | Polls backend pipeline directly (bypasses Supabase) | Same | Same |

### Key Design Choice: Stale = Show Now + Refresh Later

When cached data is stale (>4 hours old), the UI does **not** block with a loading spinner. Instead:

1. **Immediately** renders the stale Supabase snapshot so the user sees bricks, signals, and trades right away
2. **Fires a background warm-up** request that re-fetches 6 months of Yahoo Finance data, feeds it through the pipeline, and upserts the new snapshot to Supabase
3. When the warm-up completes, the UI **updates in place** with fresh data

This avoids a blank screen during the 10-30 second warm-up process and provides the best user experience for returning visitors.

## Warm-up Process

The warm-up flow feeds 6 months of historical Yahoo Finance data through the pipeline so it has enough bricks to detect patterns and generate signals.

### BFF Warm-up Endpoint (`/api/renko/warmup`)

**POST** — Warm up pipeline + save to Supabase:
1. Check Supabase for fresh cache (<4h) — if found, return it directly
2. Fetch historical prices from Yahoo Finance (`yahoo-finance2`)
3. Reset the backend pipeline for the target symbol
4. Set regime to `low_vol_bull` (enables signal generation during warm-up)
5. Feed prices in chunks of 150 via `POST /renko/tick/batch` (avoids Render timeout)
6. Fetch full pipeline state (bricks, classified, signals, trades, stats)
7. Upsert snapshot to Supabase `ta_renko_snapshot` on `(symbol, brick_size)`

**GET** — Load cached snapshot:
1. Query Supabase by `(symbol, brick_size)`
2. Return `{ cached: true, stale: true/false, ... }` or `{ cached: false }`

### Auto-warm-up Triggers

Warm-up is automatically triggered when:
- Page loads and no Supabase cache exists for the symbol
- Page loads and Supabase cache is stale (>4h old)
- User switches to a new symbol with no cached data and empty backend pipeline
- User clicks the "Warm Up" button manually

## Frontend Components

| File | Description |
|------|-------------|
| `src/components/renko/RenkoPage.jsx` | Main page with symbol selector, metric cards, sub-tabs, warm-up/tick buttons |
| `src/components/renko/BrickChart.jsx` | CSS-based Renko brick visualization with swing labels |
| `src/components/renko/SignalsPanel.jsx` | Active position, signals table, filter status, Kelly fraction |
| `src/components/renko/TradesPanel.jsx` | Trade journal, session stats, equity curve |
| `src/components/renko/ConfigPanel.jsx` | Pipeline configuration with save/reset |
| `src/app/api/renko/[action]/route.js` | BFF proxy to backend (handles auth, cold starts, retries) |
| `src/app/api/renko/warmup/route.js` | BFF warm-up endpoint (Yahoo Finance + Supabase persistence) |
| `src/lib/supabase/db.js` | Prisma-like Supabase wrapper with upsert support |

## Configuration

Default pipeline parameters (can be changed via the Config panel or `/renko/config`):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `brick_size` | 0.50 | Fixed brick size in dollars |
| `brick_size_mode` | fixed | `fixed`, `atr`, or `dynamic` |
| `reversal_bricks` | 2 | Bricks needed for direction reversal |
| `bull_trigger_n` | 3 | Consecutive HH/HL for bull trigger |
| `bear_trigger_n` | 3 | Consecutive LL/LH for bear trigger |
| `sl_bricks` | 3 | Stop-loss distance in bricks |
| `tp_bricks` | 5 | Take-profit distance in bricks |
| `trailing_stop` | true | Enable trailing stop |
| `trail_after_bricks` | 3 | Activate trail after N bricks profit |
| `trail_distance_bricks` | 2 | Trail distance in bricks |
| `max_trades_per_session` | 15 | Maximum trades per session |
| `max_daily_loss_bricks` | 10.0 | Maximum daily loss in bricks |
| `max_consecutive_losses` | 3 | Pause after N consecutive losses |
| `cooldown_seconds` | 30.0 | Minimum seconds between trades |
| `regime_gate` | true | Only trade with HMM regime alignment |

## Deployment

- **Backend**: FastAPI on Render — `https://noble-trader-fastapi-backend.onrender.com`
- **Frontend**: NextJS v16 on Vercel — `https://noble-trader-agent-frontend.vercel.app`
- **Database**: Supabase (PostgreSQL) — `ta_renko_snapshot` table
- **GitHub**: `lexingtontechus/noble-trader-fastapi-backend` and `lexingtontechus/noble-trader-agent-frontend`

## Required Environment Variables (Render)

The backend requires two Supabase environment variables on Render for full functionality:

| Variable | Description | Example |
|----------|-------------|---------|
| `SUPABASE_URL` | Supabase project URL | `https://pcvscowltlrxzgxjurcr.supabase.co` |
| `SUPABASE_KEY` | Supabase **service_role** key (NOT the anon/publishable key) | `eyJhbG...` |

> **IMPORTANT**: There is no `SUPABASE_PUBLISHABLE_KEY` variable. The backend uses `SUPABASE_KEY`
> which must be the **service role** key because it needs full database access for:
> - Reading/writing `system_config` table (runtime config)
> - Reading/writing `ta_renko_snapshot` table (pipeline persistence)
> - Looking up `api_keys` table (SaaS API key authentication)
> - Writing to `system_config_audit` table (config change logs)
> - Writing to `rate_limit_violations` table
>
> The anon/publishable key is for frontend use only (`NEXT_PUBLIC_SUPABASE_URL` + `NEXT_PUBLIC_SUPABASE_ANON_KEY`)
> and does NOT have sufficient permissions for backend operations.

When these variables are not configured, the `/renko/health` endpoint will report:
- `runtime_config.db_available: false` — Supabase DB not connected
- `supabase_snapshot.ok: false, note: "SUPABASE_URL/KEY not configured"`
- The pipeline still works with hardcoded defaults, but runtime config changes are not persisted

## Bug Fix History

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| HTTP 500 on all `/renko/*` GET endpoints | `runtime_config.get_str()` called `str()` on `BrickSizeMode.FIXED` enum default, producing `'BrickSizeMode.FIXED'` instead of `'fixed'`. `BrickSizeMode()` constructor then raised `ValueError` | `get_str()` now checks for Enum and returns `.value`; `from_runtime_config()` strips `ClassName.` prefix from enum strings (commit `0735a6d`) |
| No bricks on frontend | Tick button sent random prices, direction case mismatch, no warm-up | Real price fetch + `.toUpperCase()` + Warm Up button |
| Symbol switching shows no data | `fetchAllData()` used stale `symbol` from React closure | Added `symbolOverride` parameter |
| All symbols feed SPY pipeline | Backend `/tick/batch` hardcoded `_get_pipeline("SPY")` | Added `symbol` field to `BatchTickInput` |
| Supabase `ta_renko_snapshot` never saves | `db.js` `findFirst` defaulted `orderBy` to `"createdAt"` (camelCase) but table uses `created_at` (snake_case) | Added explicit `orderBy` + replaced with Supabase `upsert` |
| Stale cache never refreshes | `loadCachedSnapshot` ignored `stale: true` flag | Returns `"fresh"/"stale"/false` + background refresh on stale |
