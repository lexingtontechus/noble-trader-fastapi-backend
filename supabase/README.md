# Noble Trader Admin

**Intelligent Trading Dashboard — HMM Regime Detection, EV Engine v3, TimesFM Foundation Model, Parameter Optimization & AI-Powered Market Intelligence**

Noble Trader Admin is a production-grade Next.js dashboard that orchestrates a three-tier Intelligence system across 14 symbols (3 HyperLiquid perps + 2 Alpaca crypto spot + 9 Alpaca stocks/ETFs). It combines Hidden Markov Model regime detection with a blended EV Engine v3 (regime + L2 imbalance + Markov transition + TimesFM foundation model), automated parameter sweeps with vectorized GPU inference, Kelly fraction sizing, aggression dispatching, AI-generated market briefings, a calibration loop for bias detection, lead/lag correlation adjustment, tail-risk overlays, and shift-aware wakeups. SPY and QQQ ETFs provide broad-market regime context.

---

## Architecture Overview

```
  ┌───────────────┐  ┌───────────────┐  ┌──────────────────────┐
  │ Noble Trader  │  │  Upstash Redis│  │  HyperLiquid +       │
  │  FastAPI      │  │  (Config +    │  │  Yahoo Finance       │
  │  Backend      │  │   Heartbeat + │  │  (Market Data)       │
  │  + EV v3      │  │   EV State +  │  │                      │
  │  + Calibration│  │   Allowlists) │  │                      │
  │  + Venue Mgmt │  │               │  │                      │
  └───────────────┘  └───────┬───────┘  └──────────────────────┘
          │                  ▲
          ▼                  │
  ┌──────────────────────────────────────────────────────────┐
  │          Supabase (PostgreSQL + Edge Functions            │
  │  ┌─────────────┐  ┌─────────────────────────────────┐   │
  │  │  pg_cron +   │  │  Edge Functions                  │   │
  │  │  pg_net      │  │  • sweep (unified)               │   │
  │  │  (Scheduler) │  │    - mode=heavy                  │   │
  │  └──────┬───────┘  │    - mode=light                  │   │
  │         │          │    - action=sync-allowlist       │   │
  │         │          │    - action=sync-calibration     │   │
  │         └──────────│    - vectorized TimesFM batch    │   │
  │                    │    - calibration bias queries    │   │
  │                    │    - sweep fatigue skip logic    │   │
  │                    │    - shift-aware wakeups         │   │
  │                    └──────────┬──────────────────────┘   │
  │                               ▼                          │
  │  ┌─────────────────────────────────────────────────┐    │
  │  │  Lightning AI — TimesFM T4 GPU Instance          │    │
  │  │  • /forecast/batch  (batched 14-symbol inference)│    │
  │  │  • /forecast        (single-symbol fallback)     │    │
  │  │  • /health          (status check)               │    │
  │  └─────────────────────────────────────────────────┘    │
  └──────────────────────────────────────────────────────────┘
                       │
                       │ Redis writes signals + cal bias
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │       Trading Bot (mini-services/trading-bot)       │
  │  ┌──────────┐  ┌──────────┐  ┌────────────────┐   │
  │  │  Redis   │  │  Risk    │  │  Telegram       │   │
  │  │  Poller  │  │  Manager │  │  Commands       │   │
  │  │  + Tail  │  │         │  │                │   │
  │  │  Risk    │  │         │  │                │   │
  │  └────┬─────┘  └────┬─────┘  └────────────────┘   │
  │       ▼              ▼                               │
  │  ┌────────────────────────────────────────────────┐ │
  │  │          Executor Registry                      │ │
  │  │  ┌───────────┐  ┌────────────────────────┐    │ │
  │  │  │ HLExecutor│  │  AlpacaExecutor         │    │ │
  │  │  │ (24/7)    │  │  (market hours)         │    │ │
  │  │  └─────┬─────┘  └──────────┬─────────────┘    │ │
  │  └────────┼───────────────────┼──────────────────┘ │
  │           ▼                   ▼                    │
  │  HyperLiquid API       Alpaca API                 │
  │  (perp orders)         (equity/forex orders)      │
  └───────────────────────────────────────────────────┘
```

---

## Features

### EV Engine v3 (Market Pulse — TimesFM-Enhanced)
- **Blended P_win** — Weighted probability from **4 independent signal sources**:
  - `P_regime` (35%) — HMM posterior probabilities (bull/bear/neutral from trend_probs)
  - `P_imbalance` (20%) — L2 order book imbalance confidence × persistence
  - `P_markov` (20%) — Real Markov transition matrix (UP/DOWN/FLAT states)
  - `P_timesfm` (25%) — **TimesFM foundation model** zero-shot forecast (v3)
  - Falls back to 3-source (40/30/30) when TimesFM unavailable
- **Cold-Start Path** — Symbols with <81 bars get a `warming_up` regime with TimesFM-only forecast (no HMM needed), automatically resolving once bars accumulate
- **Lead/Lag Correlation** — 20-bar rolling Pearson matrix adjusts each symbol's p_timesfm based on top-2 correlated leaders (e.g. BTC → ETH, SPY → QQQ)
- **Fat-Tail Risk Overlay** — Extreme p_timesfm (<0.2 or >0.8) + high-confidence + high-vol regime triggers skip/reduce signal written to Redis as `tail_risk_score`
- **Regime Shift Meta-Signal** — HMM-detected shifts validated against TimesFM; unconfirmed shifts preserve previous regime
- **EV Computation** — `EV = (P_win × profit) - (P_loss × stake)` with normalized EV per dollar risked
- **Aggression Dispatcher** — EV-driven execution strategy:
  - EV > 0.40 → **Passive Limit** (deep entry, capture the spread)
  - 0.15 < EV ≤ 0.40 → **Mid-Point Limit** (balanced fill)
  - EV ≤ 0.15 → **Aggressive / Market** (fill immediately)
- **TTM Decay** — Time-to-Market patience scaling by volatility regime
- **Bayesian Updater** — Beta-Binomial P_win posterior updates with session loss decay
- **EV-Scaled Kelly** — `final_f = base_kelly × ev_scale` (proportional sizing, not binary)
- **Market Pulse View** — Single-number conviction indicator across all symbols

### Trade Journal
- **Account Summary** — Wallet balance, margin usage, P&L overview from HyperLiquid
- **Open Positions** — Live positions with entry price, unrealized P&L, leverage, liquidation price
- **Fill History** — Detailed trade fills with side, price, size, fee, and P&L
- **Open Orders** — Active limit/stop orders
- **Daily P&L** — Day-by-day profit/loss breakdown
- **Per-Coin Summary** — Aggregate P&L by trading pair

---

## Redis Fields — v3 Additions

The `trading:config:{symbol}` hash in Upstash Redis has the following new fields:

| Field | Type | Source | Description |
|---|---|---|---|
| `p_timesfm` | float | TimesFM batched inference | Foundation model forecast probability (0-1) |
| `timesfm_horizon` | string | TimesFM | Forecast window label (e.g. "12h") |
| `timesfm_conf` | float | TimesFM | Model confidence (0-1) |
| `calibration_bias` | float | Sweep calibration query | avg(predictedPWin) - actual_win_rate |
| `calibration_status` | string | Sweep calibration query | CALIBRATED / OVERCONFIDENT / UNDERCONFIDENT |
| `sweep_skip_until` | timestamp | Heavy sweep | Heavy sweeps skipped until this Unix ms (Sweep Fatigue) |
| `tail_risk_score` | float | Light regime | 0.0 (none) - 1.0 (critical) tail risk |
| `tail_risk_action` | string | Light regime | none / reduce_25 / reduce_50 / skip |

---

## Calibration Loop (v3)

The Calibration Loop pairs every trade's predicted P_win with its actual outcome, enabling bias detection and dynamic weight adjustment:

1. **Trade close** → Renko `TradeJournal.record()` → fire-and-forget POST to `nt_calibration_records` in Supabase
2. **Heavy sweep** → queries `AVG(predicted_p_win)` vs `actual_win_rate` per symbol → writes `calibration_bias` + `calibration_status` to Redis
3. **Sweep Fatigue** → if status is `CALIBRATED`, sets `sweep_skip_until` (12h ahead) to skip unnecessary re-optimizations
4. **`sync-calibration`** admin action → iterates all active symbols, recomputes calibration bias, writes to Redis

Bias thresholds:
- `\|bias\| < 0.05` → **CALIBRATED** (predictions match outcomes)
- `bias >= 0.05` → **OVERCONFIDENT** (too optimistic)
- `bias <= -0.05` → **UNDERCONFIDENT** (too pessimistic)
- < 20 records → **INSUFFICIENT_DATA**

---

## Shift-Aware Wakeups (v3)

When the light regime detects a **volatility regime shift** (e.g. `low_vol` → `high_vol`), it fire-and-forget triggers an emergency heavy sweep for that symbol:

```typescript
// In runLightRegimeForSymbol():
if (isVolatilityShift(prevRegime, currentRegime)) {
  fetch(`/functions/v1/sweep`, { body: { mode: "heavy", symbol } });
}
```

This reduces heavy sweeps by ~30-40% by only re-optimizing when market microstructure actually changes, rather than strictly every 4 hours.

---

## Three-Tier Intelligence System

> **Updated June 17, 2026.** The two edge functions (`heavy-sweep` + `light-regime`)
> have been merged into a single unified `sweep` function (Option F). Both tiers are
> now modes of the same function, switched via request body. The old functions are
> still deployed but no longer referenced by any active code path.

### Heavy Sweep (Full Parameter Optimization)
Runs a full grid search across brick sizes, SL/TP ratios, and HMM parameters. Scheduled via `pg_cron` on an asset-class-aware cadence (migration 018):

| Asset Class | Cron (UTC) | Cadence | Rationale |
|---|---|---|---|
| **Crypto** (BTC/ETH/SOL/BTCUSD/ETHUSD) | `X 2,6,10,14,18,22 * * *` | 6×/day | 24/7 market — no off-hours concept |
| **Stocks/ETFs** (AAPL/AMZN/GOOGL/META/MSFT/NVDA/TSLA/SPY/QQQ) | `XX 21 * * 1-5` | 1×/day | Post US close, inside the global dead zone (20:00→23:00 UTC) |

Moving stocks/ETFs heavy sweeps out of US/EU market hours eliminated the leading cause of recurring Supabase 5xx errors (heavy invocations/day: 84 → 39, −54%; market-hours heavy invocations for stocks/ETFs: ~42 → 0, −100%). The edge function also enforces this defensively: heavy sweeps for non-crypto symbols outside the 20:00–23:00 UTC window return HTTP 202 `{status:"deferred", reason:"outside-dead-zone"}` (see `_shared/utils.ts:isOffHoursForAssetClass()`).

- Optimizes for **Sharpe ratio** with minimum trade count filters
- Computes **blended EV** with P_regime from HMM posteriors + P_imbalance from L2 depth + P_markov from transition matrix
- Writes optimal config + EV state to `trading:config:{symbol}` in Redis
- Logs results to `nt_sweep_results` table in Supabase (via hardened `writeWithDeadLetter()` — 3× exponential backoff + jitter, dead-letter fallback to `nt_write_failures` on terminal failure)
- **130s deadline guard** — skips remaining symbols if approaching Supabase's 150s limit
- Per-symbol lock TTL: 360s (prevents same-symbol double-fire across heavy + light)
- **Calibration write** — queries `nt_calibration_records` for each symbol and writes `calibration_bias` + `calibration_status` to Redis
- **Sweep Fatigue** — when a symbol is `CALIBRATED`, sets `sweep_skip_until` (12h future), skipping heavy sweeps for that symbol until the timer expires. OVERCONFIDENT / UNDERCONFIDENT clears the skip. Reduces heavy sweeps by ~30-40%.

### Light Regime (HMM Detection + TimesFM)
Fast HMM-only run that detects the current market regime without re-optimizing parameters. Runs every 15 minutes during the 18 non-heavy hours (F-Lite-1 rule — light is suppressed during heavy hours to avoid collision with the heavy sweep cron).

- Detects **5-state trend** (strong_bull, bull, neutral, bear, strong_bear) + **2-state volatility** (low_vol, high_vol)
- Produces composite labels like `low_vol_bull`, `high_vol_bear`
- **Vectorized TimesFM Inference** — prices for all symbols in the batch are fetched simultaneously, then a **single batched** `timesfmPost()` call is made to Lightning AI T4 GPU (96 calls/day vs 1,344)
- **Lead/Lag Adjustment** — 20-bar Pearson correlation matrix adjusts each symbol's p_timesfm based on top-2 correlated leaders
- Updates regime_label, confidence, Markov probabilities, Kelly fraction, **EV Engine v3 outputs** (4-source blend with p_timesfm), and **tail_risk_score** in Redis
- Logs regime shifts to `nt_regime_log` table
- **Shift-Aware Wakeups** — on volatility regime shift (e.g. `low_vol` → `high_vol`), triggers an emergency heavy sweep for that symbol instead of waiting for the next cron
- **Cold Start** — symbols with <81 bars but ≥20 bars + TimesFM available get `warming_up` regime with TimesFM-only forecast (signal=neutral, no trades)
- **Tail Risk** — extreme p_timesfm (<0.2 or >0.8) + high confidence + high_vol → writes `tail_risk_score: 0.85, tail_risk_action: skip` to Redis
- **130s deadline guard** — skips remaining symbols if approaching Supabase's 150s limit
- Per-symbol lock TTL: 90s

### Unified `sweep` Function

Both tiers are modes of `supabase/functions/sweep/index.ts`:

```json
{"mode":"heavy","symbol":"BTC"}              // single-symbol heavy sweep
{"mode":"light","symbol":"BTCUSD"}           // single-symbol light regime
{"mode":"light","batch":["BTC","ETH","SOL"]} // batch light regime
{"action":"sync-allowlist"}                  // admin: sync Redis allowlist
{"action":"sync-calibration"}                // admin: compute + write calibration bias for all symbols
{"warmup":true}                              // pre-boot the worker
```

**Two-layer concurrency control** (see [Cron Schedule Reference](#4-cron-schedule-reference) for details):
1. **Per-symbol lock** (`SET lock:sweep:{sym} NX EX`) — kills same-symbol double-fire
2. **Global concurrency cap** (sorted set `sweep:active`, max 4) — kills 504s from runtime exhaustion

**F-Lite-1 enforcement** (defense in depth, 3 layers):
- **Cron schedule** — light jobs scheduled only during non-heavy hours
- **Fix A** (SQL) — `sim_retry_stale_symbols()` skips light during heavy hours
- **Fix B** (edge fn) — `Deno.serve` returns `202 suppressed` for light during heavy hours

---

## EV Engine Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. DATA INGESTION                                               │
│   OHLCV candles  ──→ HMM Regime Detector  ──→ regimeProbs       │
│   L2 Book snap   ──→ Imbalance Engine    ──→ ImbalanceSignal    │
│   Price state    ──→ Markov Chain         ──→ transitionMatrix  │
│   Prices batched ──→ TimesFM GPU (T4)     ──→ p_timesfm         │
│   (14-symbols)   ──→ Correlation Matrix  ──→ lead/lag adjust    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ 2. EV COMPUTATION v3 (Market Pulse)                             │
│   P_win = 0.35×P_regime + 0.20×P_imbalance + 0.20×P_markov + 0.25×P_timesfm│
│   EV    = (P_win × profit) - (P_loss × stake)                  │
│   EV/RR = EV / stake  (normalized edge per dollar risked)       │
│                                                                 │
│   🟢 EV/RR > 0.40  → High Conviction                          │
│   🟡 0.15–0.40     → Marginal Edge                             │
│   🔴 < 0.15        → Thin Edge / Reject                        │
│   (falls back to 40/30/30 blend when TimesFM unavailable)       │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ 3. POSITION SIZING (EV-scaled Kelly)                            │
│   base_kelly = /size/kelly → recommended_f                     │
│   ev_scale   = min(1.0, EV_per_dollar / 0.50)                 │
│   final_f    = base_kelly × ev_scale                            │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ 4. AGGRESSION DISPATCHER                                        │
│   EV > 0.40 → PASSIVE LIMIT (deep entry)                       │
│   0.15–0.40 → MID-POINT LIMIT (balanced fill)                  │
│   EV ≤ 0.15 → AGGRESSIVE / MARKET (fill immediately)           │
│   + TTM Decay: vol-scaled patience for limit order management   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ 5. BAYESIAN LIVE UPDATE (TimesFM-Enhanced Prior)                │
│   Beta-Binomial posterior updates after each bar                │
│   Prior = (1-w) × default + w × TimesFM forecast               │
│   Session decay: penalize P_win after 3+ consecutive losses    │
│   Early exit: if EV drops below min_ev mid-trade, tighten stop │
└─────────────────────────────────────────────────────────────────┘
```

---

## Symbols Coverage

> **Updated June 17, 2026 (post-sweep-unification).** System simplified from 28 → 14
> symbols. Forex (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD), commodities (GOLD, SILVER,
> OIL, COPPER), and small-cap crypto (AVAX, ARB, DOGE, LINK, XRP, WIF) were retired
> to focus on the highest-liquidity instruments. SOLUSD (Alpaca crypto spot) was
> removed last to eliminate same-symbol double-fire with SOL (HyperLiquid perp).

| Asset Class | Symbols | Count | Data Source |
|---|---|---|---|
| **Crypto (HyperLiquid perps)** | BTC, ETH, SOL | 3 | HyperLiquid |
| **Crypto (Alpaca spot)** | BTCUSD, ETHUSD | 2 | Alpaca |
| **Stocks & ETFs** | AAPL, AMZN, GOOGL, META, MSFT, NVDA, TSLA, SPY, QQQ | 9 | Alpaca |
| | **Total** | **14** | |

> **SPY** (S&P 500 ETF) and **QQQ** (Nasdaq-100 ETF) are included as broad-market
> regime indicators, providing macro context alongside individual stock regimes.
>
> **Retired symbols** (still in `sim_symbols` with `active = false` for historical
> P&L attribution): SOLUSD, AVAX, ARB, DOGE, LINK, XRP, WIF, EURUSD, GBPUSD, USDJPY,
> AUDUSD, USDCAD, GOLD, SILVER, OIL, COPPER.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Database** | Prisma ORM (SQLite) + Supabase (PostgreSQL) |
| **Cache/Config** | Upstash Redis (REST API) |
| **Scheduler** | pg_cron + pg_net (Supabase) |
| **Edge Functions** | Supabase Edge Functions (Deno runtime) |
| **Trading Engine** | Noble Trader FastAPI Backend (Render) |
| **Market Data** | HyperLiquid API (perpetual futures), Yahoo Finance (stocks/forex/commodities) |
| **Foundation Model** | TimesFM (Google) on Lightning AI T4 GPU — zero-shot forecasting |
| **EV Engine** | v3 — Blended P_win (regime + imbalance + Markov + TimesFM), Aggression Dispatcher, Bayesian Updater w/ TimesFM prior, Tail Risk Overlay, Regime Shift Meta-Signal |
| **AI Commentary** | z-ai-web-dev-sdk (LLM) |

---

## Project Structure

```
├── supabase/
│   ├── functions/
│   │   ├── sweep/
│   │   │   ├── index.ts            # Unified sweep fn (mode=heavy|light|warmup|sync-allowlist|sync-calibration)
│   │   │   │                      #   + vectorized TimesFM batch inference
│   │   │   │                      #   + lead/lag correlation adjustment
│   │   │   │                      #   + cold-start path (warming_up)
│   │   │   │                      #   + shift-aware wakeups
│   │   │   │                      #   + sweep fatigue skip logic
│   │   │   │                      #   + tail risk assessment
│   │   │   │                      #   + calibration bias query + write
│   │   │   └── index.deployed.ts   # Self-contained bundle (utils.ts inlined) for deploy
│   │   ├── heavy-sweep/index.ts    # [DEPRECATED] replaced by sweep, kept for rollback
│   │   ├── light-regime/index.ts   # [DEPRECATED] replaced by sweep, kept for rollback
│   │   └── _shared/
│   │       └── utils.ts            # Noble Trader client, Redis, PostgREST, EV + precision helpers
│   │                              #   + timesfmPost() — batched GPU inference
│   │                              #   + computeCalibrationBias() — Supabase PostgREST query
│   │                              #   + computeCorrelationMatrix() — 20-bar Pearson
│   │                              #   + adjustLeadLag() — leader-adjusted forecast
│   │                              #   + computeTailRisk() — tail risk score
│   │                              #   + isVolatilityShift() — vol change detection
│   └── migrations/
│       ├── 001_sim_tables.sql               # Schema: sim_symbols, sim_sweep_results, sim_regime_log
│       ├── 002_add_spy_qqq.sql              # Add SPY & QQQ ETFs
│       ├── 003_add_copper.sql               # Add COPPER commodity
│       ├── 004_stagger_cron_batches.sql     # Stagger cron into 3 asset-class batches
│       ├── 005_fix_cron_auth.sql            # Vault-based auth, SECURITY DEFINER invoker
│       ├── 006_stagger_cron_subbatches.sql  # 3-symbol sub-batches, 1-min gaps
│       ├── 007_enable_weekend_regime.sql    # Weekend regime detection for crypto/forex
│       ├── 008_add_cron_retry_on_503.sql    # Retry logic for 503 cold-start errors
│       ├── 009_warmup_and_batch.sql         # Warmup calls + batch optimization
│       ├── 010_verified_warmup_batch.sql    # Verified warmup batch scheduling
│       ├── 011_fix_xrp_link_testnet.sql     # Fix XRP/LINK testnet data source
│       ├── 012_add_btcusd_ethusd.sql        # Add BTCUSD/ETHUSD Alpaca crypto spot
│       ├── 013_mainnet_pricing_alpaca_datasource.sql  # Alpaca mainnet pricing
│       ├── 014_simplify_to_15_symbols.sql   # Simplify to 15 symbols
│       ├── 015_sweep_function.sql           # Unified sweep fn + 24 cron jobs cutover
│       ├── 016_fix_retry_stale_symbols.sql  # Fix retry_stale_symbols() to call sweep
│       ├── 017_f_lite_1_enforcement.sql     # F-Lite-1: suppress light during heavy hours
│       ├── 018_off_hours_heavy_sweep.sql    # P1: stocks/ETFs heavy → 21:00 UTC off-hours
│       ├── 019_asset_aware_stale_retry.sql  # P1: asset-class-aware retry-stale skip
│       ├── 020_write_hardening.sql          # P2: sim_write_failures DLQ + countShifts24h index
│       └── 100_nt_calibration_account_venue.sql  # NEW: nt_venues, nt_calibration_records, nt_account_snapshots (v3)
├── Caddyfile                      # Gateway configuration
└── (project root)
```

---

## Getting Started

### Prerequisites
- Node.js 18+ or Bun runtime
- Supabase project with pg_cron and pg_net extensions
- Upstash Redis instance
- Noble Trader FastAPI backend running

### Environment Variables

Create a `.env.local` file:

```env
# Noble Trader Backend
NEXT_PUBLIC_NOBLE_TRADER_URL=https://noble-trader-fastapi-backend.onrender.com

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE=your-service-role-key

# Upstash Redis
UPSTASH_REDIS_REST_URL=https://your-redis.upstash.io
UPSTASH_REDIS_REST_TOKEN=your-token

# HyperLiquid (Trade Journal)
HL_WALLET_ADDRESS=0x...

# Auth
AUTH_SECRET=your-auth-secret

# TimesFM Foundation Model (v3)
TIMESFM_INFERENCE_URL=https://your-lightning-instance.xyz/forecast
TIMESFM_API_KEYS=your-api-key
```

### Installation

```bash
# Install dependencies
bun install

# Set up database
bun run db:push

# Start development server
bun run dev
```

The app will be available at `http://localhost:3000`.

---

## Auto-Trading Bot

The Noble Trader Auto-Trading Bot is a standalone Bun service that monitors the intelligence pipeline (Redis signals) and auto-executes trades on multiple venues with full circuit breakers, risk management, and Telegram control.

### Architecture

```
Redis (signals) → Venue Router → Risk Gate → Executor → Telegram
                                                              → Journal (Prisma)

Venue Router: symbol → executor (Redis override or DEFAULT_VENUE_MAP)
├── BTC, ETH, SOL, ... → HLExecutor (HyperLiquid Perps, 24/7)
├── AAPL, MSFT, SPY, ... → AlpacaExecutor (US Equities, 9:30–16:00 ET)
├── EURUSD, GBPUSD, ... → AlpacaExecutor (Forex, 24h weekdays)
└── GOLD, SILVER, ... → AlpacaExecutor (CME Commodities, 23h weekdays)
```

### Features

- **Multi-Venue Execution** — HyperLiquid (crypto perps, 24/7) + Alpaca (equities, forex, commodities)
- **VenueExecutor Interface** — Pluggable executor pattern, easy to add IBKR or other venues
- **Circuit Breakers** — Max trades/session, daily drawdown limit, consecutive loss cooldown, kill switch
- **Redis-Backed Allowlist** — Per-venue allowlist/blocklist, configurable at runtime via Telegram
- **Market Hours Gating** — Won't trade equities outside 9:30–16:00 ET, respects CME forex/commodity schedules
- **Telegram Bot** — Notifications for every trade + command handlers for remote control
- **Trade Journal** — Writes Signal, Trade, and AccountSnapshot records to Prisma for calibration
- **Position Management** — SL/TP monitoring, signal flip detection, adverse regime shift auto-close
- **EV Engine Integration** — Reads blended P_win, EV per dollar, aggression, effective Kelly from Redis

### Telegram Commands

| Command | Description |
|---|---|
| `/status` | Bot status + session stats + venue connections |
| `/pnl` | Realized P&L summary |
| `/positions` | Open positions with entry, SL/TP, duration |
| `/market` | Market hours status (equities, forex, commodities) |
| `/kill` | 🚨 Kill switch — close all positions, pause bot |
| `/resume` | Resume after kill switch |
| `/allowlist` | Show per-venue allowlists + blocklists |
| `/allow [venue] [symbol]` | Add symbol to venue allowlist |
| `/block [venue] [symbol]` | Block symbol on a venue |
| `/unblock [venue] [symbol]` | Unblock symbol |

### Risk Management

| Circuit Breaker | Default | Description |
|---|---|---|
| Max trades/session | 5 | Hard cap on trades per day |
| Max daily drawdown | 5% | Kills bot if exceeded |
| Consecutive loss cooldown | 2 → 30min | Pause after 2 consecutive losses |
| Max open positions | 3 | Across all venues |
| Max position size | 25% equity (HL) / 15% (Alpaca) | Per-position notional cap |
| Min EV per dollar | 0.15 | Won't enter trades below this |
| Min effective Kelly | 0.05 | Won't enter with tiny sizing |
| Min regime confidence | 0.40 | Requires confident regime detection |
| Kill switch | Manual `/kill` or auto on DD | Closes all positions on all venues |

### Venue Configuration

| Feature | HyperLiquid | Alpaca |
|---|---|---|
| Asset class | Crypto perpetual futures | US equities, ETFs, forex |
| Hours | 24/7 | 9:30–16:00 ET (equities), 24h weekdays (forex) |
| Auth | EIP-712 wallet signature | API key + secret |
| Max leverage | 50× | 4× (Reg T) |
| Fees | 2 bps taker | $0 commission |
| Paper trading | Testnet | Paper API (identical to live) |
| Position sizing | Coins (fractional) | Shares (integer) or lots |
| Symbol format | BTC, ETH, SOL | AAPL, EUR/USD, etc. |

### Deployment (Render)

1. **Create a Background Worker** service in Render
2. Set **Root Directory** to `mini-services/trading-bot`
3. Set **Runtime** to **Docker**
4. Add environment variables from `.env.example`
5. No health check needed (long-running process)

Or use the included `render.yaml` for Blueprint deployment:

```bash
# The render.yaml handles:
# - Background Worker type
# - Docker build from mini-services/trading-bot/Dockerfile
# - Starter plan ($7/mo)
# - All required env vars
```

### Local Development

```bash
cd mini-services/trading-bot
cp .env.example .env   # Fill in your keys
bun install
bun run dev            # Starts with --hot for auto-reload
```

### Signal Flow

```
1. Supabase Edge Functions write to Redis (trading:config:{symbol})
2. Bot polls Redis every 15s for all 14 symbols
3. Signal change detected → venue router selects executor
4. Risk gate checks: allowlist, blocklist, EV minimums, session limits
5. Market hours verified (equities/forex/commodities)
6. Executor places order on the venue (IOC for HL, market for Alpaca)
7. Position tracked in-memory, SL/TP checked every 5s
8. On exit: P&L calculated, risk stats updated, journal written, Telegram notified
9. Account snapshots taken every 60s per venue
```

---

## Supabase + Redis Setup

### 1. Supabase Project Setup

1. Create a new Supabase project at [supabase.com](https://supabase.com)
2. Enable the required extensions in the SQL Editor:

```sql
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net SCHEMA extensions;
```

3. Run the migrations in order:

```bash
# Apply schema and seed symbols
psql $DATABASE_URL -f supabase/migrations/001_sim_tables.sql

# Add SPY & QQQ
psql $DATABASE_URL -f supabase/migrations/002_add_spy_qqq.sql

# Add Copper
psql $DATABASE_URL -f supabase/migrations/003_add_copper.sql

# Stagger cron batches (replaces hardcoded placeholders)
psql $DATABASE_URL -f supabase/migrations/004_stagger_cron_batches.sql

# Fix cron auth: Vault-based secrets + SECURITY DEFINER invoker
psql $DATABASE_URL -f supabase/migrations/005_fix_cron_auth.sql

# Stagger into 3-symbol sub-batches (reduces cold-start 503s)
psql $DATABASE_URL -f supabase/migrations/006_stagger_cron_subbatches.sql

# Enable weekend regime detection
psql $DATABASE_URL -f supabase/migrations/007_enable_weekend_regime.sql

# Add 503 retry logic
psql $DATABASE_URL -f supabase/migrations/008_add_cron_retry_on_503.sql

# Warmup and batch optimization
psql $DATABASE_URL -f supabase/migrations/009_warmup_and_batch.sql

# Verified warmup batch
psql $DATABASE_URL -f supabase/migrations/010_verified_warmup_batch.sql

# Fix XRP/LINK testnet data source
psql $DATABASE_URL -f supabase/migrations/011_fix_xrp_link_testnet.sql

# Add BTCUSD/ETHUSD Alpaca crypto spot
psql $DATABASE_URL -f supabase/migrations/012_add_btcusd_ethusd.sql

# Alpaca mainnet pricing data source
psql $DATABASE_URL -f supabase/migrations/013_mainnet_pricing_alpaca_datasource.sql

# Simplify to 15 symbols
psql $DATABASE_URL -f supabase/migrations/014_simplify_to_15_symbols.sql

# Unified sweep fn + 24 cron jobs cutover
psql $DATABASE_URL -f supabase/migrations/015_sweep_function.sql

# Fix sim_retry_stale_symbols() to call sweep
psql $DATABASE_URL -f supabase/migrations/016_fix_retry_stale_symbols.sql

# F-Lite-1: suppress light during heavy hours
psql $DATABASE_URL -f supabase/migrations/017_f_lite_1_enforcement.sql

# P1: stocks/ETFs heavy sweep → 21:00 UTC off-hours window
psql $DATABASE_URL -f supabase/migrations/018_off_hours_heavy_sweep.sql

# P1: asset-class-aware retry-stale skip
psql $DATABASE_URL -f supabase/migrations/019_asset_aware_stale_retry.sql

# P2: sim_write_failures dead-letter table + countShifts24h composite index
psql $DATABASE_URL -f supabase/migrations/020_write_hardening.sql
```

> **Important:** Migration 005 uses Supabase Vault for the service role key. After running it, you must manually insert your key:
> ```sql
> SELECT vault.create_secret(
>   'supabase_service_role',
>   'eyJhbGciOiJIUzI1NiIs...YOUR_ACTUAL_KEY',
>   'Supabase service_role key'
> );
> ```
> Find your key at: Supabase Dashboard → Settings → API → service_role key (secret)

4. Deploy Edge Functions:

```bash
# Primary — inlines _shared/utils.ts into each function, deploys via Management API
SUPABASE_ACCESS_TOKEN=sbp_xxx bun run scripts/deploy-edge-functions.ts

# Or via Supabase CLI (one at a time — does NOT auto-inline _shared/utils.ts)
supabase functions deploy sweep         # primary (v13+) — mode-switched heavy+light
supabase functions deploy light-regime  # legacy (deprecated, kept for watch window)
supabase functions deploy heavy-sweep   # legacy (deprecated, kept for watch window)
```

5. Set Edge Function secrets:

```bash
supabase secrets set \
  UPSTASH_REDIS_REST_URL=https://your-redis.upstash.io \
  UPSTASH_REDIS_REST_TOKEN=your-token \
  SB_URL=https://your-project.supabase.co \
  SB_SERVICE_ROLE_KEY=your-service-role-key \
  NOBLE_TRADER_URL=https://noble-trader-fastapi-backend.onrender.com
```

### 2. Upstash Redis Setup

1. Create a Redis instance at [upstash.com](https://upstash.com)
2. Copy the **REST URL** and **REST Token** from the dashboard
3. Add them to your `.env.local` and Supabase Edge Function secrets

#### Redis Key Schema

| Key Pattern | Type | Description |
|---|---|---|
| `trading:config:{symbol}` | Hash | **Golden State** — regime, brick params, EV, Kelly, signal, prices, shift state |
| `trading:heartbeat:{symbol}` | String (TTL) | Last heartbeat timestamp (epoch ms), healthy if TTL not expired |
| `trading:errors:{symbol}` | List | Last 10 error entries from edge functions |
| `trading:regime_cache` | String (JSON) | Aggregated regime cache for all symbols (1 GET vs N HGETALL) |
| `trading:allowlist:{venueId}` | Set | Symbols allowed per venue (`hyperliquid`, `alpaca`). Empty = all allowed |
| `trading:venue_map:{symbol}` | Hash | Venue routing per symbol: `venueId`, `assetClass`, `alpacaSymbol` |

---

## Redis Config Reference (`trading:config:{symbol}`)

The `trading:config:{symbol}` hash is the **single source of truth** for each symbol's intelligence state. It's written by the unified `sweep` edge function in both `mode=heavy` (full optimization — crypto 6×/day, stocks/ETFs 1×/day at 21:00 UTC) and `mode=light` (fast regime update, every 15min), and read by the trading bot every 15 seconds.

### Data Flow

```
sweep mode=heavy (crypto 6×/day at 02/06/10/14/18/22 UTC; stocks/ETFs 1×/day at 21:00 UTC)
  → fetches 45d candles → runs /optimize + /regime/detect + /size/kelly in parallel
  → selects best Sharpe combo → computes EV → writes FULL config
  → logs row to sim_sweep_results table (via hardened writeWithDeadLetter — 3× backoff+jitter, DLQ fallback)
  → stocks/ETFs deferred (HTTP 202) outside 20:00–23:00 UTC off-hours window

sweep mode=light (every 15min, suppressed during heavy hours — F-Lite-1)
  → fetches 200 latest candles → runs /regime/detect + /size/kelly
  → updates regime + signal + EV every cycle
  → full config write only on regime shift; targeted HSET otherwise
  → logs regime shifts to sim_regime_log table
```

### Timestamp Fields

| Field | Written By | Meaning |
|-------|-----------|---------|
| `last_update` | Both (every cycle) | Last time ANY field was modified |
| `sweep_timestamp` | Both (every cycle) | Last time brick params were validated (optimized or refreshed). A gap vs `last_update` indicates brick params may be stale |

### Field Reference

All values are stored as strings in the Redis hash. Numeric fields are parsed with NaN/negative guards.

#### Regime Detection Fields

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `regime` | `med_low_vol_strong_bull` | `/regime/detect` API | Composite of `{vol_state}_{trend_state}` from 2-state volatility HMM × 5-state trend HMM. Values: `low_vol_bull`, `high_vol_bear`, `med_low_vol_strong_bull`, etc. **Guard:** If detection returns `"unknown"`, the previous valid regime is preserved instead of overwriting. |
| `regime_conf` | `0.9999` | `/regime/detect` API | Posterior probability of the most likely HMM state (0–1). Higher = more confident in current regime classification. |

#### Renko Brick Parameters (set by `sweep mode=heavy` optimization)

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `brick_size` | `163.19` | `/optimize` API | Optimal Renko brick size in price units. Generated by `generateBrickSizes()` using ATR-based calibration: `base = price × baseBrickPct / 100` where `baseBrickPct` varies by volatility regime (0.75% high-vol crypto, 0.60% medium stocks, 0.40% low-vol indices, 0.25% forex). The `/optimize` grid search tests 3 multipliers (1.0×, 1.5×, 2.0×) and selects the best Sharpe. |
| `sl_bricks` | `2` | `/optimize` API | Stop-loss distance in brick units. Selected from grid `[2, 3, 4, 5]` by highest Sharpe ratio with R:R ≥ 2.0. |
| `tp_bricks` | `5` | `/optimize` API | Take-profit distance in brick units. Selected from grid `[4, 5, 6, 8, 10, 12]` by highest Sharpe ratio with R:R ≥ 2.0. |
| `sweep_sharpe` | `0.6775` | `/optimize` API | Sharpe ratio of the best backtest combination. Sharpe > 10 is rejected as overfit. |
| `sweep_trades` | `15` | `/optimize` API | Number of trades in the best backtest combo. Minimum 5 required for statistical significance. |
| `sweep_window` | `45d` | Edge function | Lookback window for the sweep. BETA: 45d to fit 0.5 CPU Render tier (was 90d). |

#### Markov Probability Fields

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `markov_p_up` | `0.65` | `computeMarkovProbs()` | Heuristic probability of upward move, derived from regime label. Bullish → 0.65 (low-vol) or 0.55 (high-vol). Bearish → inverse. Neutral → 0.50. Used by the trading bot for quick regime-based probability lookup. |
| `markov_p_dn` | `0.35` | `computeMarkovProbs()` | Inverse of `markov_p_up` (1 − markov_p_up). |
| `markov_current_state` | `FLAT` | `computeMarkovTransition()` | Current Markov chain state: `UP`, `DOWN`, or `FLAT`. Computed from real price transitions: UP = price increased > brick_size, DOWN = price decreased > brick_size, FLAT = within brick_size. |

#### Kelly Fraction Fields

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `kelly_f` | `0.5` | `/size/kelly` API | Base Kelly fraction from the Kelly criterion. Capped at 0.5 (half-Kelly max). Uses `recommended_f` from API, falling back to `regime_gated_f` → `fractional_f` → `full_kelly_f × 0.5`. Zeroed for `unknown`/`market_closed` regimes; 50% reduction in high-vol regimes. |
| `effective_kelly` | `0.5` | `computeEV()` | `kelly_f × ev_scale`. This is the actual position sizing the trading bot uses. Proportional to EV conviction — low EV shrinks the bet, high EV keeps it at full Kelly. |

#### Trade Signal Fields (computed every `sweep mode=light` cycle)

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `signal` | `buy` | `computeTradeSignal()` | Direction to trade: `buy` (bullish regime), `sell` (bearish regime), `neutral` (unknown/market_closed). Based on whether regime label contains "bull" or "bear". |
| `entry_price` | `66596.0` | `computeTradeSignal()` | Current market price, rounded to exchange tick size. |
| `stop_loss` | `66263.0` | `computeTradeSignal()` | `entry_price ± (sl_bricks × brick_size)`, rounded to tick size. Floor at 1% of entry (prevents zero-SL). Minimum SL distance guard widens SL if too tight (< MIN_SL_PCT of price, e.g. 0.5% for BTC, 0.3% for stocks). |
| `take_profit` | `67412.0` | `computeTradeSignal()` | `entry_price ± (tp_bricks × brick_size)`, rounded to tick size. |
| `price_decimals` | `1` | Heavy-sweep | Tick-size decimal places per symbol (BTC=1, ETH=2, SOL=4, stocks=2, forex=5). Used for rounding SL/TP to exchange-acceptable prices. |

#### EV Engine v2 Fields (Market Pulse)

The EV Engine computes a blended probability of winning from three independent signal sources, then derives expected value, aggression level, and EV-scaled position sizing.

**Core formula:**
```
P_win = 0.40 × P_regime + 0.30 × P_imbalance + 0.30 × P_markov
EV    = (P_win × profit) - (P_loss × stake)     where profit = tp_bricks × brick_size, stake = sl_bricks × brick_size
EV/RR = EV / stake                                (normalized edge per dollar risked)
```

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `p_win` | `0.624` | `computeEV()` | **Blended probability of winning.** `0.40 × p_regime + 0.30 × p_imbalance + 0.30 × p_markov`. Range 0–1. This is the single-number conviction indicator. |
| `p_regime` | `0.9999` | `computePRegime()` | **HMM regime component (40% weight).** For longs: `P(BULL) + 0.5 × P(NEUTRAL)` from HMM trend_probs posteriors. For shorts: `P(BEAR) + 0.5 × P(NEUTRAL)`. Range 0–1. High values indicate the HMM is confident the current regime aligns with the trade direction. |
| `p_imbalance` | `0.50` | `computePImbalance()` | **L2 order book imbalance component (30% weight).** Aligned with direction: `confidence × (0.6 + 0.4 × persistence_bonus)`. Opposed: `1 − confidence`. Flat/no data: `0.50` (neutral). Currently defaults to 0.50 for most symbols (L2 data not yet wired into heavy-sweep). |
| `p_markov` | `0.2455` | `computePMarkov()` | **Markov transition component (30% weight).** `P(UP \| current_state)` for longs, `P(DOWN \| current_state)` for shorts. Computed from real price transition matrix built by `computeMarkovTransition()`. |
| `ev` | `386.02` | `computeEV()` | **Raw expected value** in price units. `(p_win × profit) - (p_loss × stake)`. Positive = favorable trade. |
| `ev_per_dollar` | `1.18` | `computeEV()` | **Normalized EV per $1 risked.** `ev / stake`. Key metric for aggression dispatching: > 0.40 = passive, 0.15–0.40 = mid, < 0.15 = aggressive. |
| `aggression` | `passive` | `computeEV()` | **Execution strategy.** `passive` (EV/$ > 0.40 → use deep limit orders to capture the spread), `mid` (0.15–0.40 → balanced midpoint limit), `aggressive` (< 0.15 → market orders for immediate fill). |
| `ev_scale` | `1.0` | `computeEV()` | **Kelly scaling factor.** `min(1.0, ev_per_dollar / 0.50)`. Scales position size proportionally to EV conviction. High EV → full Kelly; low EV → reduced Kelly. |
| `effective_kelly` | `0.5` | `computeEV()` | **Actual position sizing fraction.** `kelly_f × ev_scale`, capped at 0.5. This is what the trading bot reads to determine position size. |

#### Regime Shift Fields (v2.3 — Shift-Aware EV)

Regime shifts occur when the HMM detects a change in the market's underlying state (e.g., `low_vol_bull` → `high_vol_bear`). These fields expose shift events to the trading bot and dashboard, enabling shift-aware position sizing and risk management.

**Why this matters:** Previously, the bot could only detect regime shifts by comparing its stored position regime with the current regime — missing shifts entirely when flat. Now the bot explicitly sees shift events from Redis, including shift velocity (how many shifts in 24h), which is a second-order volatility meta-signal.

**Timestamp approach (idempotent):** The bot stores `lastShiftSeen[symbol]` and compares it with `shift_at`. If `shift_at > lastShiftSeen`, it's a new shift. No clearing or ack required — multiple consumers can independently track shifts without race conditions.

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `regime_shift` | `"true"` | Both functions | `"true"` if a regime shift was detected this cycle (current regime ≠ previous valid regime); `"false"` otherwise. Written by both light-regime and heavy-sweep on every cycle. Heavy-sweep compares new regime with previous Redis config to detect shifts. |
| `prev_regime` | `"low_vol_bull"` | Both functions | The regime the symbol was in **before** this cycle. Always populated: on shift, it's the regime transitioned FROM; on no-shift, it's the same as current regime (carried forward from cache/Redis). Useful for understanding shift direction: `low_vol_bull → high_vol_bear` is a volatility+direction reversal; `low_vol_bull → low_vol_strong_bull` is a trend strengthening. |
| `shift_at` | `1781559902390` | Both functions | **Unix epoch ms** when the most recent shift was detected. The bot compares this with its `lastShiftSeen[symbol]` to determine if this is a new (unseen) shift: `shift_at > lastShiftSeen[symbol]` → new shift. Persists across bot restarts. Always populated: on shift, it's `Date.now()`; on no-shift, it carries forward from the previous cycle's value. `0` means no shift has ever been recorded. |
| `shifts_24h` | `3` | Both functions | **Count of regime shifts in the last 24 hours**, queried from `sim_regime_log` table. Key volatility meta-signal: 0 = stable regime (trust params), 1–2 = normal, 3+ = turbulent (market doesn't know what it wants). Always populated: heavy-sweep queries fresh count every cycle; light-regime queries fresh count on shift, carries forward from cache on no-shift. |

**Bot behavior based on shift fields:**

| Condition | Action | Rationale |
|-----------|--------|-----------|
| `shift_at` within last 30 min | Kelly × 0.5 (50% reduction) | Fresh shift → params may be stale, reduce exposure |
| `shifts_24h ≥ 3` (turbulent) | Kelly × 0.3 (70% reduction) | Oscillating regime → go defensive |
| `shifts_24h ≥ 5` | Skip entry entirely | Too unstable, don't enter new positions |
| `shift_at` within last 5 min | Hard block (risk gate) | Even with reduced Kelly, don't enter in the first 5 min after a shift |
| Adverse shift + open position | Close position | Bull→bear while long, or bear→bull while short |
| Turbulent + open position | Close position | ≥3 shifts/24h = exit, even if shift direction is favorable |

#### Audit / Metadata Fields

| Field | Example | Source | Calculation |
|-------|---------|--------|-------------|
| `last_update` | `1781559902390` | Both functions | Epoch ms of last write. Updated every cycle by both heavy-sweep and light-regime (via `redisUpdateSignal()` or `redisSetConfig()`). |
| `sweep_timestamp` | `1781559902390` | Both functions | Epoch ms of last optimization or regime refresh. Updated by: (1) heavy-sweep on optimize success or fallback, (2) light-regime on regime shift, (3) every light-regime cycle via `redisUpdateSignal()`. If this is stale (>4h), brick params may be outdated. |

### Example Config (BTC)

```
trading:config:BTC
├── regime               : "med_low_vol_strong_bull"    # 2-state vol × 5-state trend HMM
├── regime_conf          : "1"                          # Posterior probability (0-1)
├── brick_size           : "163.19"                     # ATR-calibrated, optimized by Sharpe
├── tp_bricks            : "5"                          # Take-profit in brick units
├── sl_bricks            : "2"                          # Stop-loss in brick units
├── markov_p_up          : "0.65"                       # Regime-based heuristic P(up)
├── markov_p_dn          : "0.35"                       # 1 - markov_p_up
├── kelly_f              : "0.5"                        # Base Kelly (capped at 0.5)
├── last_update          : "1781559902390"              # Epoch ms, updated every cycle
├── sweep_timestamp      : "1781559902390"              # Epoch ms, updated on optimize/refresh
├── sweep_sharpe         : "0.6775"                     # Sharpe of best backtest combo
├── sweep_trades         : "15"                         # Trades in best combo
├── sweep_window         : "45d"                        # Lookback (BETA: 45d for 0.5 CPU)
├── signal               : "buy"                        # buy | sell | neutral
├── entry_price          : "66596.0000"                 # Current price (tick-rounded)
├── stop_loss            : "66263.0000"                 # entry - sl_bricks × brick_size
├── take_profit          : "67412.0000"                 # entry + tp_bricks × brick_size
├── price_decimals       : "1"                          # Tick precision (BTC=1, forex=5)
│
├── // EV Engine v2
├── ev                   : "386.02"                     # (P_win × profit) - (P_loss × stake)
├── ev_per_dollar        : "1.18"                       # EV / stake
├── p_win                : "0.624"                      # 0.40×P_regime + 0.30×P_imbalance + 0.30×P_markov
├── p_regime             : "0.9999"                     # P(BULL) + 0.5×P(NEUTRAL) from HMM posteriors
├── p_imbalance          : "0.50"                       # L2 imbalance (0.50 = no data)
├── p_markov             : "0.2455"                     # P(UP|current_state) from transition matrix
├── aggression           : "passive"                    # passive (>0.40) | mid (0.15-0.40) | aggressive (<0.15)
├── ev_scale             : "1.0"                        # min(1.0, ev_per_dollar / 0.50)
├── effective_kelly      : "0.5"                        # kelly_f × ev_scale (actual position size)
├── markov_current_state : "FLAT"                       # Current state: UP | DOWN | FLAT
│
├── // Regime Shift (v2.3 — Shift-Aware EV)
├── regime_shift         : "false"                      # "true" if shift detected this cycle
├── prev_regime          : "med_low_vol_strong_bull"    # Previous regime (always populated)
├── shift_at             : "0"                          # Epoch ms of last shift (0 = never)
└── shifts_24h           : "0"                          # Shifts in last 24h (3+ = turbulent)
```

### Regime Degradation Guard

When `/regime/detect` returns `"unknown"` (API timeout, incomplete data), both edge functions **preserve the previous valid regime** instead of overwriting with `"unknown"`:

- **sweep mode=light**: Falls back to the previous regime from cache. Uses `effectiveRegime` for all downstream computations (EV, signal, Markov). Logs as source `light-regime-degraded` for audit.
- **sweep mode=heavy**: Reads the previous regime from Redis config. Falls back to `"unknown"` only if no previous config exists (cold start).
- Price-dependent fields (entry, SL, TP) are still updated every cycle even during degradation, so they don't go stale.

### 3. Verify the Pipeline

After setup, verify the full pipeline works:

```bash
# Test heavy sweep manually (creates Redis config + EV state)
curl -X POST https://your-project.supabase.co/functions/v1/sweep \
  -H "Authorization: Bearer YOUR_SERVICE_ROLE" \
  -H "Content-Type: application/json" \
  -d '{"mode":"heavy","symbol":"BTC"}'

# Test light regime manually
curl -X POST https://your-project.supabase.co/functions/v1/sweep \
  -H "Authorization: Bearer YOUR_SERVICE_ROLE" \
  -H "Content-Type: application/json" \
  -d '{"mode":"light","symbol":"BTC"}'

# Check Redis config was written (includes EV fields)
curl https://your-redis.upstash.io/hgetall/trading:config:BTC \
  -H "Authorization: Bearer YOUR_TOKEN"

# Check cron jobs are scheduled
curl https://your-project.supabase.co/rest/v1/rpc/cron_job_status \
  -H "apikey: YOUR_SERVICE_ROLE" \
  -H "Authorization: Bearer YOUR_SERVICE_ROLE"
```

### 4. Cron Schedule Reference

> **Updated June 17, 2026 (post-sweep-unification + Fix A/B/C).** All 24 active jobs
> call the unified `sweep` edge function (v12) at `/functions/v1/sweep` via
> `pg_cron` + `pg_net` (`net.http_post`). The old `heavy-sweep` and `light-regime`
> functions are still deployed but no longer referenced by any active code path.

#### Heavy Sweep — Every 4 hours at staggered minutes (14 jobs)

Heavy sweeps run a full Renko grid optimization (3 brick sizes × 4 SL × 6 TP = ~72
combos) and write to `sim_sweep_results` + `trading:config:{symbol}` in Redis. Each
symbol is offset by 5 minutes to avoid API rate limits and to stay within the
**global concurrency cap of 4** concurrent sweep invocations.

**Heavy hours (UTC):** 02, 06, 10, 14, 18, 22 (every 4 hours).
**F-Lite-1 rule:** Light regime is suppressed during these hours to avoid collision.

| Job ID | Job name | Schedule (cron) | Symbol | DOW |
|---:|---|---|---|---|
| 780 | `sweep-heavy-BTC` | `0 2,6,10,14,18,22 * * *` | BTC | daily |
| 781 | `sweep-heavy-ETH` | `5 2,6,10,14,18,22 * * *` | ETH | daily |
| 782 | `sweep-heavy-SOL` | `10 2,6,10,14,18,22 * * *` | SOL | daily |
| 783 | `sweep-heavy-AAPL` | `0 2,6,10,14,18,22 * * 1-5` | AAPL | weekdays |
| 784 | `sweep-heavy-AMZN` | `5 2,6,10,14,18,22 * * 1-5` | AMZN | weekdays |
| 785 | `sweep-heavy-GOOGL` | `10 2,6,10,14,18,22 * * 1-5` | GOOGL | weekdays |
| 786 | `sweep-heavy-META` | `15 2,6,10,14,18,22 * * 1-5` | META | weekdays |
| 787 | `sweep-heavy-MSFT` | `20 2,6,10,14,18,22 * * 1-5` | MSFT | weekdays |
| 788 | `sweep-heavy-NVDA` | `25 2,6,10,14,18,22 * * 1-5` | NVDA | weekdays |
| 789 | `sweep-heavy-TSLA` | `30 2,6,10,14,18,22 * * 1-5` | TSLA | weekdays |
| 790 | `sweep-heavy-SPY` | `35 2,6,10,14,18,22 * * 1-5` | SPY | weekdays |
| 791 | `sweep-heavy-QQQ` | `40 2,6,10,14,18,22 * * 1-5` | QQQ | weekdays |
| 792 | `sweep-heavy-BTCUSD` | `20 2,6,10,14,18,22 * * *` | BTCUSD | daily |
| 793 | `sweep-heavy-ETHUSD` | `25 2,6,10,14,18,22 * * *` | ETHUSD | daily |

**Schedule notation:** `M H * * DOW` where M = minute, H = hour, DOW = day-of-week
(`*` = daily, `1-5` = Mon-Fri). Heavy jobs fire 6 times/day (crypto) or 6 times/weekday
(stocks) — i.e. every 4 hours during heavy hours.

#### Light Regime — Every 15 minutes during non-heavy hours (9 jobs)

Light regime runs an HMM-only fast regime detection (no parameter optimization)
and updates Redis config + `sim_regime_log` table. Light jobs fire at quarter-hour
marks but **only during non-heavy hours** (F-Lite-1 — see below).

**Light hours (UTC):** 00, 01, 03, 04, 05, 07, 08, 09, 11, 12, 13, 15, 16, 17, 19, 20, 21, 23
(every hour except the 6 heavy hours).

| Job ID | Job name | Schedule (cron) | Symbols in batch | DOW |
|---:|---|---|---|---|
| 794 | `sweep-light-crypto` | `0,15,30,45 <light-hours> * * *` | BTC, ETH, SOL | daily |
| 795 | `sweep-light-stocks-1-wk` | `5,20,35,50 <light-hours> * * 1-5` | AAPL, AMZN, GOOGL | weekdays |
| 796 | `sweep-light-stocks-2-wk` | `6,21,36,51 <light-hours> * * 1-5` | META, MSFT, NVDA | weekdays |
| 797 | `sweep-light-stocks-3-wk` | `7,22,37,52 <light-hours> * * 1-5` | TSLA, SPY, QQQ | weekdays |
| 798 | `sweep-light-stocks-1-we` | `5,35 <light-hours> * * 0,6` | AAPL, AMZN, GOOGL | weekend |
| 799 | `sweep-light-stocks-2-we` | `6,36 <light-hours> * * 0,6` | META, MSFT, NVDA | weekend |
| 800 | `sweep-light-stocks-3-we` | `7,37 <light-hours> * * 0,6` | TSLA, SPY, QQQ | weekend |
| 801 | `sweep-light-BTCUSD` | `0,15,30,45 <light-hours> * * *` | BTCUSD | daily |
| 802 | `sweep-light-ETHUSD` | `0,15,30,45 <light-hours> * * *` | ETHUSD | daily |

Where `<light-hours>` = `0,1,3,4,5,7,8,9,11,12,13,15,16,17,19,20,21,23`.

**Weekend cadence:** Stocks run at half the weekday frequency (`:05/:35` instead of
`:05/:20/:35/:50`) because markets are closed — the sweep is just keeping regime
detection warm for Monday open.

#### Retry Stale Symbols — Every 5 minutes (1 job)

| Job ID | Job name | Schedule | Purpose |
|---:|---|---|---|
| 777 | `retry-stale-symbols` | `*/5 * * * *` | Picks up any symbol whose `sim_regime_log` is >20 min stale. Calls `sweep` with `{"mode":"light","symbol":"<SYM>"}`. **Fix A:** skips light invocations during heavy hours and defers to the heavy sweep cron. |

#### F-Lite-1: Light Suppressed During Heavy Hours

To eliminate the recurring collision between `retry-stale-symbols` (every 5 min) and
the heavy sweep cron (at minute :00–:40 of every 4th hour), light regime is suppressed
during the 6 heavy hours [02, 06, 10, 14, 18, 22 UTC]. This is enforced in **three
layers** (defense in depth):

| Layer | Where | Mechanism |
|---|---|---|
| **Fix A** | SQL — `sim_retry_stale_symbols()` | Checks `EXTRACT(HOUR FROM now() AT TIME ZONE 'UTC') IN (2,6,10,14,18,22)` before each `net.http_post` call. If heavy hour, skips with `RAISE NOTICE` and continues to next symbol. (Migration 017.) |
| **Fix B** | Edge function — `sweep/index.ts` `Deno.serve` handler | After mode validation, if `mode === "light" && isHeavyHour()` (where `HEAVY_HOURS_UTC = [2,6,10,14,18,22]`), returns `202 {"status":"suppressed","reason":"f-lite-1","hour":<H>}` immediately — before lock acquisition, before any work. Defense in depth: catches any caller that bypasses the cron schedule (manual curl, future SQL changes, etc). |
| **Cron schedule** | `pg_cron.job` rows 794–802 | The light cron jobs are scheduled only during non-heavy hours (see schedule notation above). This is the primary defense; Fix A and Fix B are backups. |

#### Two-Layer Concurrency Control (in the edge function)

| Layer | Mechanism | Purpose | TTL |
|---|---|---|---|
| **1. Per-symbol lock** | `SET lock:sweep:{sym} NX EX {TTL}` | Kills same-symbol double-fire (e.g. heavy + light on SOL at 02:10 UTC) | Heavy: 360s · Light: 90s |
| **2. Global concurrency cap** | Sorted set `sweep:active` (member=UUID, score=timestamp) + `ZREMRANGEBYSCORE` stale eviction + `ZCARD` check | Kills 504s from runtime exhaustion — max 4 concurrent invocations | Stale eviction: 300s · Key EXPIRE: 330s |

Deferred jobs (symbol-locked or cap-reached) return `202 {"status":"deferred"}`
and are picked up by `retry-stale-symbols` on its next 5-min cycle.

#### Skip Visibility (Fix C)

When a sweep is skipped due to a per-symbol lock collision, it is now logged to the
`sim_errors` table with a descriptive message:

```sql
SELECT created_at, symbol, fn_name, substring(error_msg, 1, 100)
FROM sim_errors
WHERE fn_name IN ('sweep-heavy', 'sweep-light')
ORDER BY created_at DESC LIMIT 20;
```

Example skip entry:
```
2026-06-17 21:15:24  BTCUSD  sweep-light  skipped: symbol-locked by concurrent invocation (heavy sweep holding lock)
```

Previously these skips were only `console.warn` (ephemeral, lost on function cold
start). Now they appear in the dashboard's `sim_errors` view for post-mortem analysis.

#### Authentication

All cron jobs use the `set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true)` + `net.http_post` pattern. The service role key is read from **Supabase Vault** at runtime — secrets never appear in cron job definitions or committed SQL files.

```sql
-- Verify Vault secret exists:
SELECT name, decrypted_secret FROM vault.decrypted_secrets
WHERE name = 'supabase_service_role';

-- Verify a cron job's command:
SELECT jobid, jobname, command
FROM cron.job
WHERE jobname = 'sweep-heavy-SOL';

-- Test a single sweep invocation manually:
SELECT set_config('app.settings.service_role_key', vault_read('supabase_service_role'), true);
SELECT net.http_post(
  url     := 'https://<project-ref>.supabase.co/functions/v1/sweep',
  headers := jsonb_build_object(
    'Content-Type',  'application/json',
    'Authorization', 'Bearer ' || vault_read('supabase_service_role')
  ),
  body    := jsonb_build_object('mode', 'heavy', 'symbol', 'SOL')
);
```

#### Verification Commands

```sql
-- Last run status of every active cron job:
SELECT j.jobid, j.jobname, j.active, r.status,
       (now() - r.end_time) AS since_last_run
FROM cron.job j
LEFT JOIN LATERAL (
  SELECT * FROM cron.job_run_details WHERE jobid = j.jobid
  ORDER BY start_time DESC LIMIT 1
) r ON true
WHERE j.active = true
ORDER BY j.jobid;

-- Failed cron runs in the last 2 hours:
SELECT j.jobname, r.status, r.start_time, substring(r.return_message, 1, 100)
FROM cron.job_run_details r
JOIN cron.job j ON j.jobid = r.jobid
WHERE r.start_time > now() - interval '2 hours'
  AND r.status != 'succeeded'
ORDER BY r.start_time DESC;

-- sim_sweep_results freshness per symbol (heavy sweep DB writes):
SELECT symbol, max(sweep_timestamp) AS last_heavy,
       (now() - max(sweep_timestamp)) AS age
FROM sim_sweep_results
GROUP BY symbol
ORDER BY age;

-- sim_errors in the last 2 hours (Fix C skip visibility):
SELECT created_at, symbol, fn_name, substring(error_msg, 1, 80) AS err
FROM sim_errors
WHERE created_at > now() - interval '2 hours'
ORDER BY created_at DESC;
```

---

## API Endpoints

### Intelligence (Governor)
| Endpoint | Method | Description |
|---|---|---|
| `/api/governor/configs` | GET | Fetch all symbol configs + EV state + heartbeats from Redis |
| `/api/governor/intelligence-commentary` | GET | AI-generated market briefing (5-min cache) |
| `/api/governor/cron-status` | GET | pg_cron scheduled job status |
| `/api/governor/sweep-results` | GET | Heavy sweep optimization history |
| `/api/governor/regime-log` | GET | Regime transition detection log |
| `/api/governor/reports` | GET | List reports by asset class |
| `/api/governor/generate-report` | POST | Generate new analysis report |
| `/api/governor/repair` | POST | Fix NaN values in Redis configs |

### Scheduler
| Endpoint | Method | Description |
|---|---|---|
| `/api/scheduler/status` | GET | Scheduler health + invocation history |
| `/api/scheduler/invoke` | POST | Trigger edge function manually |
| `/api/scheduler/stale-check` | GET | Detect stale configs (missed heartbeats) |

### Trading & Market Data
| Endpoint | Method | Description |
|---|---|---|
| `/api/noble-trader/[...path]` | * | Proxy to Noble Trader FastAPI backend |
| `/api/hyperliquid/candles` | GET | Fetch candle data from HyperLiquid |
| `/api/hyperliquid/symbols` | GET | Available HyperLiquid symbols |
| `/api/hyperliquid/l2book` | POST | L2 order book depth snapshot |
| `/api/hyperliquid/trade-journal` | GET | Wallet fills, positions, P&L from HyperLiquid |
| `/api/yahoo-finance/candles` | POST | Fetch OHLCV from Yahoo Finance |

### Reports
| Endpoint | Method | Description |
|---|---|---|
| `/api/reports` | GET | List all reports |
| `/api/reports/latest` | GET | Latest reports per asset class |
| `/api/reports/status` | GET | Report generation status |
| `/api/reports/run` | POST | Trigger report generation |
| `/api/reports/[assetClass]/[id]` | GET | Fetch individual report |
| `/api/reports/[assetClass]/[id]/download` | GET | Download report as file |

### Auth
| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/login` | POST | Authenticate with Noble Trader backend |

---

## Supabase Database Schema

### `sim_symbols` — Symbol Registry

| Column | Type | Description |
|---|---|---|
| `symbol` | TEXT (PK) | Trading symbol (e.g. BTC, SPY, EURUSD) |
| `asset_class` | TEXT | `crypto`, `forex`, `stocks`, `commodities` |
| `data_source` | TEXT | `yahoo` or `hyperliquid` |
| `yahoo_ticker` | TEXT | Yahoo Finance ticker (e.g. `EURUSD=X`, `GC=F`) |
| `hyperliquid_coin` | TEXT | HyperLiquid coin name (crypto only) |
| `active` | BOOLEAN | Whether the symbol is being tracked |
| `heartbeat_ttl_seconds` | INTEGER | Heartbeat expiry threshold (default 900 = 15 min) |
| `sweep_cron` | TEXT | Cron schedule for heavy sweep |
| `regime_cron` | TEXT | Cron schedule for light regime |
| `created_at` | TIMESTAMPTZ | Row creation time |
| `updated_at` | TIMESTAMPTZ | Last update time |

### `sim_sweep_results` — Heavy Sweep History

| Column | Type | Description |
|---|---|---|
| `id` | BIGSERIAL (PK) | Auto-increment ID |
| `symbol` | TEXT (FK) | Symbol reference |
| `brick_size` | DOUBLE | Optimized brick size |
| `sl_bricks` | INTEGER | Stop-loss in bricks |
| `tp_bricks` | INTEGER | Take-profit in bricks |
| `sharpe` | DOUBLE | Sharpe ratio |
| `total_return` | DOUBLE | Total return |
| `win_rate` | DOUBLE | Win rate (0–1) |
| `n_trades` | INTEGER | Number of trades |
| `profit_factor` | DOUBLE | Profit factor |
| `regime` | TEXT | Regime at sweep time |
| `regime_conf` | DOUBLE | Regime confidence |
| `kelly_f` | DOUBLE | Kelly fraction |
| `sweep_timestamp` | TIMESTAMPTZ | When the sweep ran |

### `sim_regime_log` — Regime Transition Log

| Column | Type | Description |
|---|---|---|
| `id` | BIGSERIAL (PK) | Auto-increment ID |
| `symbol` | TEXT (FK) | Symbol reference |
| `source` | TEXT | `sweep-light` or `sweep-heavy` (or legacy `light-regime` / `heavy-sweep` from pre-unification rows) |
| `regime` | TEXT | Current regime label |
| `regime_conf` | DOUBLE | Regime confidence |
| `markov_p_up` | DOUBLE | Markov probability of up move |
| `markov_p_dn` | DOUBLE | Markov probability of down move |
| `kelly_f` | DOUBLE | Kelly fraction |
| `prev_regime` | TEXT | Previous regime (null if first) |
| `regime_shift` | BOOLEAN | Whether regime changed |
| `detected_at` | TIMESTAMPTZ | Detection timestamp |

---

## Supabase Edge Functions

### Unified Sweep (`sweep/index.ts`)

> **Updated June 17, 2026.** The `heavy-sweep` and `light-regime` functions have been
> merged into a single unified `sweep` function (Option F). Both behaviors are now
> modes of the same function, switched via request body. The old functions remain
> deployed (for rollback safety) but no active code path references them.

**Mode = Heavy** (full parameter grid search with EV computation):
1. Fetches recent price data from HyperLiquid or Yahoo Finance (45d / 1080 hourly bars)
2. Computes ATR-based brick sizes (3 sizes: 1.0×, 1.5×, 2.0×) — BETA: reduced from 5 to fit 0.5 CPU Render tier
3. Runs `/optimize` in a single API call (3 bricks × 4 SL × 6 TP = 72 combos per symbol)
4. Runs **regime detection + Kelly sizing in parallel** with optimize (Promise.allSettled)
5. Selects optimal params by Sharpe ratio (with R:R ≥ 2.0 + Sharpe ≤ 10 overfit guards)
6. **Computes blended EV**: P_regime from HMM posteriors + P_imbalance from L2 depth + P_markov from transition matrix
7. **Determines aggression level**: passive/mid/aggressive based on EV per dollar risked
8. Computes trade signal (buy/sell/neutral) with tick-size-rounded SL/TP
9. Writes result + EV state to Redis `trading:config:{symbol}`
10. Logs to `sim_sweep_results` + `sim_regime_log` tables
11. **Graceful fallback**: if optimize times out but regime/kelly succeed, writes cached brick params + regime
12. **130s deadline guard** — skips remaining symbols if approaching Supabase's 150s limit
13. **Per-symbol lock** (TTL 360s) — kills same-symbol double-fire across heavy + light

**Mode = Light** (fast regime detection with EV update, runs every 15 min per symbol):
1. Fetches recent prices (hourly, fallback to daily if < 100 bars)
2. Runs HMM regime detection only (no parameter sweep)
3. Computes Kelly fraction via Noble Trader
4. **Updates EV Engine outputs**: P_regime, P_imbalance, P_markov, blended P_win, aggression
5. Updates regime_label, confidence, Markov probabilities, trade signal in Redis
6. **Refreshes `sweep_timestamp`** on every cycle (prevents stale timestamps between heavy sweeps)
7. Detects regime shifts, logs to `sim_regime_log`
8. Writes `market_closed` config when insufficient data (preserves heartbeat)
9. **130s deadline guard** — skips remaining symbols if approaching Supabase's 150s limit
10. **Per-symbol lock** (TTL 90s) — kills same-symbol double-fire across heavy + light
11. **F-Lite-1 enforcement** (Fix B) — if invoked during a heavy hour, returns `202 suppressed` immediately. Cron schedule + Fix A (SQL) should prevent this, but Fix B is defense in depth.

> **⚠️ BETA CPU Constraints (0.5 CPU Render tier)**
> - Sweep window: **45d** (1080 hourly bars) — reduced from 90d to fit CPU budget
> - Brick sizes: **3** (1.0×, 1.5×, 2.0×) — reduced from 5 to reduce combos
> - Combos/symbol: **~72** (3 bricks × 4 SL × 6 TP) — was ~120
> - Optimize call: **~40-60s** on 0.5 CPU — was ~90-120s at 90d/5 bricks
>
> **Upgrade Path (≥1 CPU on Render):**
> 1. `SWEEP_CANDLE_HOURS`: 1080 → 2160 (45d → 90d)
> 2. `SWEEP_YAHOO_RANGE`: "45d" → "90d"
> 3. `generateBrickSizes`: add 0.75× and 3.0× back (3 → 5 bricks)
> 4. `sweep_window` labels: "45d" → "90d"
> 5. Optimize timeout: 90s → 120s (more data = longer compute)

### Shared Utilities (`_shared/utils.ts`)
Common code used by the unified `sweep` function (inlined into `index.deployed.ts` for deployment):
- `nobleTraderPost()` — HTTP client with configurable timeout (90s optimize, 20s regime/kelly), 2 retries, no 429 retry (fail fast)
- `supabaseRest()` / `supabaseRestWithRetry()` — PostgREST client with transient-error retry
- `redisSetConfig()`, `redisGetConfig()`, `redisSetHeartbeat()`, `redisPushError()` — Upstash Redis
- `detectRegime()`, `computeKelly()`, `runRenkoOptimize()` — Noble Trader API wrappers (parallel-ready)
- `parseRegimeLabel()`, `computeMarkovProbs()`, `parseKellyF()` — result parsers
- `computePRegime()` — Extract P_regime from HMM trend_probs (P(BULL) + 0.5 × P(NEUTRAL))
- `computePImbalance()` — L2 imbalance → P_imbalance bridge (confidence × persistence bonus)
- `computePmarkov()` — Real Markov transition probability from transition matrix
- `computeBlendedEV()` — Weighted P_win = 0.40×P_regime + 0.30×P_imbalance + 0.30×P_markov
- `computeAggression()` — EV → passive/mid/aggressive execution strategy
- `computeTradeSignal()`, `roundPrice()` — signal generation with exchange tick-size compliance
- `fetchSweepCandles()`, `fetchLatestCandles()` — data fetching (HyperLiquid + Yahoo)
- `isMarketOpen()` — market hours check per asset class
- `sleep()` — promise-based delay

---

## Methodology

### HMM Regime Detection
- **2-State Volatility HMM** — Classifies into low-vol/high-vol using Gaussian emissions on log-returns
- **5-State Trend HMM** — Identifies strong_bull, bull, neutral, bear, strong_bear
- **Composite Labels** — Combined labels like `low_vol_bull`, `high_vol_bear`
- **Confidence Scores** — Posterior probability of most likely state (0–1)

### EV Engine v2 (Blended P_win)
- **P_regime** (40% weight) — `P(BULL) + 0.5 × P(NEUTRAL)` extracted from HMM trend_probs
- **P_imbalance** (30% weight) — L2 book imbalance confidence × persistence bonus; 0.50 if flat
- **P_markov** (30% weight) — Real Markov transition probability from UP/DOWN/FLAT state matrix
- **Blended P_win** — `0.40 × P_regime + 0.30 × P_imbalance + 0.30 × P_markov`
- **EV per dollar** — `(P_win × profit - P_loss × stake) / stake`
- **Aggression** — Passive (EV > 0.40), Mid (0.15–0.40), Aggressive (< 0.15)
- **EV-scaled Kelly** — `final_f = base_kelly × min(1.0, EV_per_dollar / 0.50)`
- **Bayesian Update** — Beta-Binomial posterior with session loss decay (0.10 penalty after 3+ consecutive losses)

### Kelly Fraction Sizing
- **Full Kelly**: `f = (p × b - q) / b` — maximizes long-run geometric growth
- **Half-Kelly (f=0.5)** — Production default, 50% variance reduction for 25% growth sacrifice
- **Regime Gate** — Kelly zeroed for `unknown`/`market_closed` regimes; 50% reduction in high-vol regimes

### Renko Brick Construction
- **2-Brick Reversal** — Requires 2× brick size movement for trend reversal (noise filter)
- **ATR-Calibrated** — Default sizes calibrated to asset's Average True Range
- **Fixed (Production)** — Optimized via parameter sweep; dynamic ATR sizing available

---

## License

Private — All rights reserved.
