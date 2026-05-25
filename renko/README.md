# Renko HFT Pipeline — Noble Trader Agent

A 6-layer Renko-based high-frequency trading pipeline that processes price ticks through brick construction, swing classification, pattern detection, signal filtering, order execution, and risk management.

## Architecture

```
Tick → Brick Engine → Swing Classifier → Pattern Detector
     → Signal Filter (Kelly ─or─ Masaniello) → Risk Manager → Executor → Trade Journal
```

| Layer | Module | Purpose |
|-------|--------|---------|
| 1 | `brick_engine.py` | Renko brick construction with 2-brick reversal rule |
| 2 | `swing_classifier.py` | Labels bricks HH/HL/LH/LL, detects swing points |
| 3 | `pattern_detector.py` | Detects bull_run, bear_run, reversal, double_top, double_bottom, consolidation_break |
| 4 | `signal_filter.py` | 7-layer risk governance + **position sizing** (Kelly or Dynamic Masaniello) |
| 5 | `risk_manager.py` | Brick-denominated SL/TP/trailing stop/time-stop/partial-exit |
| 6 | `executor.py` | Alpaca order executor with bracket orders (dry-run simulation mode when no API keys) |

The Signal Filter layer includes a pluggable position-sizing sub-layer. When `sizing_mode=kelly` (default), it computes a simple equity-fraction size. When `sizing_mode=masaniello`, it routes through the `DynamicMasanielloSizer` from `regime_platform/core/position_sizer.py`, which produces a stateful, adaptive size based on batch progress, regime quality, drawdown, and volatility. If Masaniello gates reject a trade (e.g. win probability below threshold), the filter falls back to Kelly sizing.

Supporting modules:
- `pipeline.py` — Orchestrates the full pipeline flow
- `trade_journal.py` — Records trades, computes win rate, profit factor, Sharpe, max drawdown, Kelly estimates
- `config.py` — `RenkoConfig` dataclass with 30+ tunable parameters
- `router.py` — 24 FastAPI REST endpoints
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
| `POST` | `/renko/regime` | Update HMM regime for gate filter | Admin |
| `POST` | `/renko/equity` | Update account equity | Admin |
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
| `POST` | `/renko/snapshot/save` | Explicitly save pipeline state to Supabase | Write |
| `POST` | `/renko/warmup` | Backend-native warmup (Yahoo fetch + snapshot save) | Write |
| `GET` | `/renko/sizing/model` | Get current position sizing model and parameters | Any |
| `POST` | `/renko/sizing/model` | Set position sizing model (Kelly ↔ Masaniello) | Admin |

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
- **MCP agent calls** `POST /renko/warmup` (backend-native, no BFF dependency)

### Backend Warm-up Endpoint (`POST /renko/warmup`)

v7.1 introduces a **backend-native warmup endpoint** that eliminates the need for the Next.js BFF layer. MCP agents can now fully operate the pipeline without any frontend dependency.

**Flow:**
1. Check Supabase for a fresh snapshot (<4h) — if found, restore it directly (instant, <500ms)
2. Fetch historical prices from Yahoo Finance (`yfinance` library or Yahoo v8 chart API fallback)
3. Create a fresh pipeline with runtime config
4. Set regime to `low_vol_bull` (enables signal generation during warmup)
5. Feed prices through the pipeline via backtest (warmup mode)
6. Save snapshot to Supabase (including full brick history, classified bricks, trades)
7. Return the pipeline state

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symbol` | `str` | `"SPY"` | Trading symbol |
| `period` | `str` | `"6mo"` | Yahoo Finance period: 1mo, 3mo, 6mo, 1y, 2y, 5y, max |
| `chunk_size` | `int` | `150` | Internal batch size for processing |

**Response includes:** `source` (snapshot_restore / yahoo_fetch / stale_snapshot_restore), `brick_count`, `state`, `elapsed_ms`

### Snapshot Save Endpoint (`POST /renko/snapshot/save`)

Explicitly saves the current pipeline state to Supabase. Useful for MCP agents that want to persist state after making changes (setting regime, equity, or after processing a series of ticks).

**Response includes:** `brick_count`, `total_trades`, `bricks_serialized`, `trades_serialized`, `classified_serialized`, `snapshot_ts`

## MCP Agent Workflow

MCP (Model Context Protocol) agents connect to `/mcp` and discover available tools automatically. As of v7.1, the Renko pipeline is fully autonomous via MCP — no frontend or BFF dependency required.

### MCP Tool Categories

| Category | Tools | Autonomy Level |
|----------|-------|---------------|
| **State & Health** | `renko_health`, `get_state`, `get_stats`, `get_live_status` | Read-only |
| **Brick Data** | `get_bricks`, `get_classified`, `get_swing_points` | Read-only |
| **Signals & Trades** | `get_signals`, `get_trades` | Read-only |
| **Sizing** | `get_sizing_model`, `set_sizing_model` | Read + Write |
| **Warmup & Persistence** | `warmup_pipeline`, `save_snapshot`, `restore_snapshot` | Write (stateful) |
| **Pipeline Control** | `set_regime`, `set_equity`, `process_batch` | Write (sync) |
| **Backtesting** | `run_backtest`, `optimize_backtest`, `backtest_stats`, `statistical_rigor` | Write (isolated) |
| **Execution** | `execution_model`, `toggle_live_execution` | Write (critical) |
| **Analysis** | `statistical_rigor`, `execution_model` | Read/Compute |

### MCP Agent Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                    MCP AGENT RENKO WORKFLOW                         │
│                                                                     │
│  1. COLD START (no pipeline state)                                 │
│     renko_health()        → verify backend is alive                │
│     warmup_pipeline()     → fetch Yahoo data + hydrate pipeline    │
│                             (or restore snapshot if <4h old)       │
│                                                                     │
│  2. MARKET HOURS MONITORING                                        │
│     set_regime()          → feed HMM regime detection output       │
│     set_equity()          → sync account equity from broker        │
│     get_bricks()          → latest brick direction + velocity      │
│     get_classified()      → pattern detection (impulse/correction) │
│     get_signals()         → active entry/exit signals              │
│     get_trades()          → recent trade performance               │
│                                                                     │
│  3. SIZING DECISION                                                │
│     get_sizing_model()    → Kelly or Masaniello? Check params      │
│     set_sizing_model()    → switch model if regime warrants it     │
│                                                                     │
│  4. BACKTEST & OPTIMIZATION                                        │
│     run_backtest()        → test strategy on historical data       │
│     optimize_backtest()   → parameter sweep for best config        │
│     statistical_rigor()   → is the edge statistically significant? │
│                                                                     │
│  5. STATE PERSISTENCE                                              │
│     save_snapshot()       → persist state after changes            │
│     restore_snapshot()    → recover from cold start                │
│                                                                     │
│  6. EXECUTION CONTROL                                              │
│     execution_model()     → compute slippage, fill, financing      │
│     toggle_live_execution() → enable/disable real order submission │
│     get_live_status()     → check if live execution is active      │
└─────────────────────────────────────────────────────────────────────┘
```

### Excluded from MCP (require direct REST API)

These endpoints remain excluded from MCP for safety — they are destructive or could be used for manipulation:

| Endpoint | Reason | Alternative |
|----------|--------|-------------|
| `POST /renko/tick` | Single tick injection — manipulation risk | Use `POST /renko/tick/batch` or `POST /renko/warmup` |
| `POST /renko/reset` | Destructive — loses all brick history | Use `POST /renko/warmup` to rehydrate |
| `POST /renko/config` | Destructive — rebuilds pipeline from scratch | Use runtime config API (`/config/`) |


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
| `sizing_mode` | `kelly` | Position sizing model: `kelly` or `masaniello` |
| `kelly_fraction` | 0.50 | Half-Kelly multiplier (Kelly mode) |
| `max_position_pct` | 0.10 | Max 10% of equity per trade (Kelly mode) |
| `min_position_usd` | 50.0 | Minimum order value |
| `default_win_rate` | 0.55 | Fallback win rate for Kelly when no history |

## Position Sizing Models

The pipeline supports two position sizing modes, selected via the `sizing_mode` parameter on `RenkoConfig`.

### Kelly (Default)

```
position_usd = equity × min(kelly_fraction, 0.10)
```

- **Simple and stateless** — each trade's size depends only on current equity and the Kelly fraction.
- The Kelly fraction is derived from the trade journal's historical win rate and profit factor, or falls back to `default_win_rate` (0.55) when no history exists.
- Hard cap at `max_position_pct` (10% of equity) prevents over-concentration.
- Best suited for strategies with stable edge and no need for adaptive sizing.

### Dynamic Masaniello

```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
```

- **Stateful and adaptive** — sizing changes across trades within a batch based on cumulative results.
- The formula decomposes into four independent factors:

| Factor | Symbol | Formula | Range | Purpose |
|--------|--------|---------|-------|---------|
| Base risk | β | config value | 0.25%–1.00% | Starting risk fraction |
| Masaniello progress | M_i | `(W - w) / (N - i + 1)` | [0.0, 1.5] | Urgency: wins needed vs trades remaining |
| Model quality | Q_i | `prob_factor × regime_factor × conf_factor` | [0.0, 2.25] | Edge quality gate (probability × regime × confidence) |
| Drawdown scaling | DD_i | `clip(1 - dd/max_dd, 0.25, 1.0)` | [0.25, 1.0] | Shrink size as drawdown grows |
| Volatility adjustment | V_i | `clip(ATR_baseline / ATR_current, 0.5, 1.5)` | [0.5, 1.5] | Shrink in high vol, expand in low vol |

- The `(0.5 + M_i)` term guarantees a minimum 0.5× multiplier even when the batch target is already met.
- Pre-flight gates block trades entirely when win probability, reward/risk, or regime quality fall below their minimums.
- Optional Kelly overlay caps the Masaniello fraction at the fractional Kelly bound.
- Batch state (wins, losses, drawdown, peak equity) is tracked automatically within the `SignalFilter`.
- Best suited for strategies that benefit from adaptive sizing — e.g. pressing when behind in a batch, pulling back during drawdowns, or scaling with regime confidence.

## Sizing Mode Configuration

The sizing mode can be set in four ways (highest priority wins):

### 1. RenkoConfig Constructor

```python
from renko.config import RenkoConfig, SizingMode

config = RenkoConfig(
    sizing_mode=SizingMode.MASANIELLO,
    masaniello_base_risk=0.005,
    masaniello_batch_size=5,
    masaniello_target_wins=3,
)
```

### 2. POST /renko/sizing/model (Admin)

Switch the live pipeline's sizing model at runtime:

```bash
curl -X POST https://backend/renko/sizing/model?symbol=SPY \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"sizing_mode": "masaniello", "base_risk": 0.005, "batch_size": 5}'
```

### 3. Runtime Config Key

Set `renko.sizing_mode` in the `system_config` Supabase table (or via `POST /renko/config`):

```bash
curl -X POST https://backend/renko/config \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"sizing_mode": "masaniello"}'
```

### 4. POST /renko/config with sizing_mode in body

Same as #3 — include `sizing_mode` and any `masaniello_*` overrides in the config body. This triggers a full pipeline reset.

## Masaniello Parameters

All parameters live on `RenkoConfig` with the `masaniello_` prefix. They are only used when `sizing_mode=masaniello`.

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `masaniello_base_risk` | 0.005 | (0, 1) | **β** — base risk fraction of equity (0.50%) |
| `masaniello_min_risk` | 0.0025 | (0, 1) | Hard floor for risk fraction after clamping (0.25%) |
| `masaniello_max_risk` | 0.010 | (0, 1) | Hard cap for risk fraction after clamping (1.00%) |
| `masaniello_min_prob` | 0.50 | [0, 1] | Minimum win probability to allow a trade |
| `masaniello_min_rr` | 2.50 | > 0 | Minimum reward/risk ratio to allow a trade |
| `masaniello_max_drawdown` | 0.10 | (0, 1) | Max strategy drawdown for DD_i linear scaling (10%) |
| `masaniello_regime_floor` | 0.50 | [0, 1] | Minimum regime quality (risk_multiplier) to allow a trade |
| `masaniello_use_kelly_overlay` | false | — | When true, cap Masaniello fraction at fractional Kelly bound |
| `masaniello_batch_size` | 5 | ≥ 1 | **N** — number of trades per batch |
| `masaniello_target_wins` | 3 | ≥ 0 | **W** — target wins per batch for M_i urgency factor |

All parameters can be overridden via runtime config keys `renko.masaniello_*` (e.g. `renko.masaniello_base_risk`).

## Sizing Model Endpoints

### GET /renko/sizing/model

Returns the active sizing model and its parameters for a given pipeline.

**Request:**

```bash
curl https://backend/renko/sizing/model?symbol=SPY \
  -H "Authorization: Bearer <token>"
```

**Response (Kelly mode):**

```json
{
  "symbol": "SPY",
  "sizing_mode": "kelly",
  "kelly": {
    "kelly_fraction": 0.5,
    "max_position_pct": 0.10,
    "min_position_usd": 50.0,
    "default_win_rate": 0.55
  }
}
```

**Response (Masaniello mode):**

```json
{
  "symbol": "SPY",
  "sizing_mode": "masaniello",
  "kelly": {
    "kelly_fraction": 0.5,
    "max_position_pct": 0.10,
    "min_position_usd": 50.0,
    "default_win_rate": 0.55
  },
  "masaniello": {
    "base_risk": 0.005,
    "min_risk": 0.0025,
    "max_risk": 0.01,
    "min_prob": 0.5,
    "min_rr": 2.5,
    "max_drawdown": 0.1,
    "regime_floor": 0.5,
    "use_kelly_overlay": false,
    "batch_size": 5,
    "target_wins": 3,
    "batch_state": {
      "wins": 2,
      "losses": 1,
      "trade_index": 4,
      "initial_equity": 100000.0,
      "peak_equity": 100500.0,
      "pnl": 350.0
    }
  }
}
```

### POST /renko/sizing/model

Switch the sizing model for a live pipeline. **Admin only.** Rebuilds the pipeline with the new configuration.

**Request body (`SizingModelRequest`):**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sizing_mode` | `string` | `"kelly"` | `"kelly"` or `"masaniello"` |
| `base_risk` | `float?` | `null` | Override β (default 0.005) |
| `min_risk` | `float?` | `null` | Override floor (default 0.0025) |
| `max_risk` | `float?` | `null` | Override cap (default 0.010) |
| `min_prob` | `float?` | `null` | Override min win prob (default 0.50) |
| `min_rr` | `float?` | `null` | Override min R:R (default 2.50) |
| `max_drawdown` | `float?` | `null` | Override max DD (default 0.10) |
| `regime_floor` | `float?` | `null` | Override regime floor (default 0.50) |
| `use_kelly_overlay` | `bool?` | `null` | Enable Kelly cap |
| `batch_size` | `int?` | `null` | Override batch size N (default 5) |
| `target_wins` | `int?` | `null` | Override target wins W (default 3) |

Only `sizing_mode` is required. All Masaniello-specific fields are optional overrides; omitted fields fall back to runtime config or hardcoded defaults.

**Example — switch to Masaniello with custom batch params:**

```bash
curl -X POST "https://backend/renko/sizing/model?symbol=SPY" \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "sizing_mode": "masaniello",
    "base_risk": 0.007,
    "batch_size": 7,
    "target_wins": 4
  }'
```

**Response:**

```json
{
  "status": "ok",
  "symbol": "SPY",
  "sizing_mode": "masaniello",
  "config": { "...full RenkoConfig dict..." }
}
```

## Masaniello in Backtests

### Using /renko/backtest/run with Masaniello

Include `sizing_mode` and any `masaniello_*` overrides in the backtest request body:

```bash
curl -X POST https://backend/renko/backtest/run \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "prices": [450.10, 450.55, ...],
    "symbol": "SPY",
    "sizing_mode": "masaniello",
    "masaniello_base_risk": 0.005,
    "masaniello_batch_size": 5,
    "masaniello_target_wins": 3
  }'
```

Omitted Masaniello parameters fall back to `RenkoConfig` defaults.

### Comparing Kelly vs Masaniello

Use `/renko/backtest/compare` to run the same price series with both sizing models side-by-side:

```bash
curl -X POST https://backend/renko/backtest/compare \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "prices": [450.10, 450.55, ...],
    "symbol": "SPY",
    "configs": [
      {"label": "Kelly Default", "brick_size": 0.50},
      {"label": "Masaniello β=0.5%", "brick_size": 0.50}
    ]
  }'
```

The response includes a `diff` object comparing key metrics (total PnL, Sharpe, drawdown, win rate) across both sizing models.

### Optimizing Masaniello Parameters

Use `/renko/backtest/optimize` to sweep Masaniello parameters:

```bash
curl -X POST https://backend/renko/backtest/optimize \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "prices": [450.10, 450.55, ...],
    "symbol": "SPY",
    "sizing_mode": "masaniello",
    "param_grid": {
      "masaniello_base_risk": [0.003, 0.005, 0.007],
      "masaniello_batch_size": [3, 5, 7],
      "masaniello_target_wins": [2, 3, 4]
    }
  }'
```

The optimizer returns results for each combination plus `best_by_sharpe` and `best_by_return` summaries. Maximum 50 combinations per request.

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

> **Render Deployment Note**: Missing or misconfigured `SUPABASE_URL` / `SUPABASE_KEY` on Render
> causes HTTP 500 on all `/renko/*` GET endpoints because `runtime_config.get_str()` cannot connect
> to Supabase to resolve config values. Ensure both variables are set in Render Environment settings.
> The `/renko/health` endpoint (no auth required) can be used to verify connectivity.

## Bug Fix History

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| HTTP 500 on all `/renko/*` GET endpoints (Supabase config) | `SUPABASE_URL` / `SUPABASE_KEY` not configured on Render, causing `runtime_config` to fail when reading config from Supabase `system_config` table | Set both `SUPABASE_URL` and `SUPABASE_KEY` (service role) in Render Environment settings; verify with `GET /renko/health` |
| HTTP 500 on all `/renko/*` GET endpoints (Enum bug) | `runtime_config.get_str()` called `str()` on `BrickSizeMode.FIXED` enum default, producing `'BrickSizeMode.FIXED'` instead of `'fixed'`. `BrickSizeMode()` constructor then raised `ValueError` | `get_str()` now checks for Enum and returns `.value`; `from_runtime_config()` strips `ClassName.` prefix from enum strings (commit `0735a6d`) |
| No bricks on frontend | Tick button sent random prices, direction case mismatch, no warm-up | Real price fetch + `.toUpperCase()` + Warm Up button |
| Symbol switching shows no data | `fetchAllData()` used stale `symbol` from React closure | Added `symbolOverride` parameter |
| All symbols feed SPY pipeline | Backend `/tick/batch` hardcoded `_get_pipeline("SPY")` | Added `symbol` field to `BatchTickInput` |
| Supabase `ta_renko_snapshot` never saves | `db.js` `findFirst` defaulted `orderBy` to `"createdAt"` (camelCase) but table uses `created_at` (snake_case) | Added explicit `orderBy` + replaced with Supabase `upsert` |
| Stale cache never refreshes | `loadCachedSnapshot` ignored `stale: true` flag | Returns `"fresh"/"stale"/false` + background refresh on stale |
