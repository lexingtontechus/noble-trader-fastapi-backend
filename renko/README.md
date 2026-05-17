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
- `router.py` — 14 FastAPI REST endpoints
- `integration.py` — Bridge to Noble Trader Agent backend (regime/Kelly) and Supabase trade push
- `backtest.py` / `tests.py` — Backtesting and unit tests

## API Endpoints

All endpoints are mounted under `/renko/` on the FastAPI backend.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/renko/tick` | Process single tick through pipeline |
| `POST` | `/renko/tick/batch` | Batch tick processing (warm-up/backtest) |
| `GET` | `/renko/state` | Current pipeline state snapshot |
| `GET` | `/renko/stats` | Comprehensive statistics |
| `GET` | `/renko/bricks` | Recent Renko bricks (last N) |
| `GET` | `/renko/classified` | Bricks with swing labels |
| `GET` | `/renko/signals` | Pattern signals |
| `GET` | `/renko/trades` | Trade journal entries |
| `GET` | `/renko/swing-points` | Detected swing points |
| `POST` | `/renko/regime` | Update HMM regime for gate filter |
| `POST` | `/renko/equity` | Update account equity |
| `POST` | `/renko/config` | Update pipeline configuration (resets pipeline) |
| `POST` | `/renko/reset` | Reset pipeline for a symbol |
| `GET` | `/renko/backtest/stats` | Backtest/trading statistics |

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

## Bug Fix History

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| No bricks on frontend | Tick button sent random prices, direction case mismatch, no warm-up | Real price fetch + `.toUpperCase()` + Warm Up button |
| Symbol switching shows no data | `fetchAllData()` used stale `symbol` from React closure | Added `symbolOverride` parameter |
| All symbols feed SPY pipeline | Backend `/tick/batch` hardcoded `_get_pipeline("SPY")` | Added `symbol` field to `BatchTickInput` |
| Supabase `ta_renko_snapshot` never saves | `db.js` `findFirst` defaulted `orderBy` to `"createdAt"` (camelCase) but table uses `created_at` (snake_case) | Added explicit `orderBy` + replaced with Supabase `upsert` |
| Stale cache never refreshes | `loadCachedSnapshot` ignored `stale: true` flag | Returns `"fresh"/"stale"/false` + background refresh on stale |
