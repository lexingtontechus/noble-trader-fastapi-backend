# Noble Trader — Dynamic Regime Risk Management Platform API

> **Version**: 5.0.0 | **Status**: Stable | **Base URL**: `https://noble-trader-fastapi-backend.onrender.com`

A production-grade FastAPI service for quantitative trading risk management. Detects market regimes with Hidden Markov Models, gates position sizes via the Kelly Criterion, computes real-time VaR/CVaR, and streams live regime snapshots and alerts over WebSocket and SSE.

---

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Core Concepts](#core-concepts)
- [Renko HFT Pipeline (v5.0)](#renko-hft-pipeline-v50)
- [Batch API](#batch-api)
- [Streaming API](#streaming-api)
- [Simulation](#simulation)
- [Portfolio](#portfolio)
- [GPU HMM (v3.0)](#gpu-hmm-v30)
- [Feed Adapters (v3.0)](#feed-adapters-v30)
- [Correlation & Optimisation (v3.0)](#correlation--optimisation-v30)
- [Observation Builder](#observation-builder)
- [Discord Notifications (v5.0)](#discord-notifications-v50)
- [Reference](#reference)

---

## Overview

### What it Does

The platform runs a multi-stage pipeline on every price update:

```
01 Regime HMM  →  02 Kelly Sizer  →  03 Risk Engine  →  04 Stream Out
Dual GaussianHMM   Fractional Kelly    VaR / CVaR         WS / SSE / REST
vol + trend states  vol-scaled + gated  stop + TP targets  + regime alerts
```

### API Surface

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/health` | Service health + status | Public |
| `POST` | `/auth/token` | Issue a local JWT token | Public |
| `GET` | `/auth/me` | Current authenticated user | Any |
| `GET` | `/auth/clerk/me` | Current Clerk-authenticated user | Bearer |
| `POST` | `/analyse/full` | Full pipeline: regime + sizing + risk | Public |
| `POST` | `/regime/detect` | HMM regime detection only | Public |
| `POST` | `/size/kelly` | Kelly position sizing only | Public |
| `POST` | `/risk/analyse` | Risk metrics only | Public |
| `POST` | `/stream/seed` | Seed a symbol session | Public |
| `POST` | `/stream/tick` | Push one price tick | Public |
| `GET` | `/stream/sessions` | List active sessions + stats | Public |
| `WS` | `/ws/{symbol}` | Full-duplex WebSocket stream | Public |
| `SSE` | `/sse/{symbol}` | Server-Sent Events per symbol | Public |
| `SSE` | `/sse/alerts` | Global regime-change alert broadcast | Public |
| `POST` | `/renko/tick` | Single tick through Renko pipeline | Any |
| `POST` | `/renko/tick/batch` | Batch tick processing | Any |
| `GET` | `/renko/state` | Current Renko pipeline state | Any |
| `GET` | `/renko/stats` | Pipeline statistics | Any |
| `GET` | `/renko/bricks` | Recent Renko bricks | Any |
| `GET` | `/renko/classified` | Bricks with swing labels | Any |
| `GET` | `/renko/signals` | Pattern signals | Any |
| `GET` | `/renko/trades` | Trade journal | Any |
| `GET` | `/renko/swing-points` | Detected swing points | Any |
| `POST` | `/renko/regime` | Update HMM regime | Write |
| `POST` | `/renko/equity` | Update account equity | Write |
| `POST` | `/renko/config` | Update pipeline config (resets pipeline) | Admin |
| `POST` | `/renko/reset` | Reset pipeline for a symbol | Admin |
| `GET` | `/renko/backtest/stats` | Backtest/trading statistics | Any |
| `POST` | `/simulate/{symbol}` | Monte Carlo regime simulation | Any |
| `GET` | `/portfolio` | Multi-symbol aggregated regime + risk | Any |
| `GET` | `/gpu/capabilities` | GPU HMM backend capabilities | Any |
| `POST` | `/gpu/benchmark` | HMM fit/predict latency benchmark | Any |
| `GET` | `/feeds/status` | Feed adapter status | Write |
| `POST` | `/feeds/start` | Start live feed adapters | Write |
| `POST` | `/feeds/stop` | Stop all feed adapters | Write |
| `POST` | `/correlation/detect` | DCC-based portfolio correlation regime | Any |
| `POST` | `/optimise` | Drawdown-controlled max-Sharpe optimisation | Any |
| `POST` | `/optimise/full` | Correlation + optimisation one-shot | Any |

---

## Authentication

All protected endpoints require authentication via one of three methods, tried in priority order:

| Method | Header | Description |
|--------|--------|-------------|
| **Clerk JWT** (primary) | `Authorization: Bearer <token>` | JWT issued by Clerk (verified via JWKS) |
| **Local JWT** | `Authorization: Bearer <token>` | JWT issued by `/auth/token` |
| **API Key** | `X-API-Key: <key>` | Pre-shared key for service-to-service calls |

### Auth Levels

Three role-based access levels control what actions a user can perform:

| Level | Dependency | Roles | Use Case |
|-------|-----------|-------|----------|
| **Any** | `Depends(get_authed_user)` | viewer, trader, admin | Read endpoints, tick ingestion |
| **Write** | `Depends(require_write)` | trader, admin | State mutations: regime/equity updates |
| **Admin** | `Depends(require_admin)` | admin only | Destructive ops: config changes, resets |

### Unified Auth Flow

The `get_authed_user` dependency resolves authentication in this order:

1. `AUTH_ENABLED=false` → Return synthetic admin (dev bypass)
2. `X-API-Key` header valid → Return `TokenData(sub="apikey:...", role="trader")`
3. Bearer token present → Try Clerk JWT verification (checks `iss` claim for `clerk` or `.accounts.dev`)
4. Clerk fails → Fall back to local JWT verification (`JWT_SECRET_KEY` + python-jose)
5. Nothing works → 401 Unauthorized

### Auth Endpoints

```
POST /auth/token          — Issue local JWT (OAuth2 password flow)
GET  /auth/me             — Current user from local JWT
POST /auth/refresh        — Refresh local JWT
GET  /auth/clerk/me       — Current user from Clerk JWT
POST /auth/clerk/verify   — Verify a Clerk JWT token
GET  /auth/clerk/config   — Clerk configuration status
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET_KEY` | _(empty)_ | Signing secret for local JWTs (required in production) |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINS` | `60` | Token lifetime in minutes |
| `API_KEYS` | _(empty)_ | Comma-separated valid API keys |
| `AUTH_ENABLED` | `true` | Set `false` to disable auth (dev mode) |
| `CLERK_PUBLISHABLE_KEY` | _(empty)_ | Clerk client-side key |
| `CLERK_SECRET_KEY` | _(empty)_ | Clerk server-side key (enables Clerk auth) |

> **Dev mode**: Set `AUTH_ENABLED=false` to bypass all auth. Every request gets a synthetic admin user. Never use in production.

---

## Quick Start

### Installation

```bash
# Clone the project
git clone https://github.com/lexingtontechus/noble-trader-agent-backend.git
cd noble-trader-agent-backend

# Install dependencies
pip install -r requirements.txt

# Start the server
uvicorn main:app --reload --port 8000

# Interactive Swagger docs
open http://localhost:8000/docs
```

### Batch Request — Full Pipeline

The fastest way to get started. Send a price series and receive regime, sizing, and risk in one JSON response.

```bash
curl -X POST http://localhost:8000/analyse/full \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "SPY",
    "prices": [450.1, 451.3, 449.8, ...],
    "kelly_fraction": 0.5,
    "target_vol": 0.15,
    "base_risk_limit": 0.02
  }'
```

```python
import requests

prices = [450.1, 451.3, 449.8, ...]  # min 81 bars

resp = requests.post("http://localhost:8000/analyse/full", json={
    "symbol":          "SPY",
    "prices":          prices,
    "kelly_fraction":  0.5,
    "target_vol":      0.15,
    "base_risk_limit": 0.02,
})

data = resp.json()
print(data["regime"]["regime_label"])    # e.g. "low_vol_bull"
print(data["sizing"]["recommended_f"])   # e.g. 0.3247
print(data["risk"]["var_95"])            # e.g. 0.0187
```

### Streaming — WebSocket

```python
import asyncio, json, websockets

async def stream():
    async with websockets.connect("ws://localhost:8000/ws/SPY") as ws:

        # 1. Seed with historical prices
        await ws.send(json.dumps({
            "type": "seed", "symbol": "SPY",
            "prices": prices  # list of 200+ floats
        }))
        await ws.recv()  # {"type": "seeded", ...}

        # 2. Stream live ticks
        for price in live_feed:
            await ws.send(json.dumps({
                "type": "tick", "symbol": "SPY", "price": price
            }))
            msg = json.loads(await ws.recv())
            print(msg["regime_label"], msg["recommended_f"])

asyncio.run(stream())
```

### Streaming — Browser SSE

```javascript
// Per-symbol regime stream
const feed = new EventSource('/sse/SPY');
feed.onmessage = e => {
  const tick = JSON.parse(e.data);
  console.log(tick.regime_label, tick.recommended_f);
};

// Global regime-change alert hub
const alerts = new EventSource('/sse/alerts');
alerts.onmessage = e => {
  const a = JSON.parse(e.data);
  // { symbol, previous, current, severity, message }
  if (a.severity === 'critical') triggerAlert(a);
};
```

> **Minimum bars requirement**: All batch endpoints require at least 81 price bars (50 log-returns for HMM fitting + 1 observation). For stable HMM convergence, 200+ bars is recommended.

---

## Architecture

### Project Structure

```
noble-trader-agent-backend/
├── main.py                              # FastAPI app factory (v5.0.0)
├── requirements.txt
├── render.yaml                          # Render deployment config
├── .env.example                         # Environment variable template
├── notifications/
│   ├── __init__.py                      # Exports DiscordNotifier
│   └── discord.py                       # Discord webhook notifier (rich embeds)
├── renko/
│   ├── __init__.py
│   ├── router.py                        # 14 REST endpoints under /renko/*
│   ├── brick_engine.py                  # Layer 1: Renko brick construction
│   ├── swing_classifier.py              # Layer 2: HH/HL/LH/LL classification
│   ├── pattern_detector.py              # Layer 3: Breakout/triple pattern signals
│   ├── signal_filter.py                 # Layer 4: Regime-gate + velocity filter
│   ├── executor.py                      # Layer 5: Alpaca bracket order executor
│   ├── risk_manager.py                  # Layer 6: SL=3bricks / TP=5bricks manager
│   ├── pipeline.py                      # Pipeline orchestrator
│   ├── config.py                        # RenkoConfig dataclass
│   ├── trade_journal.py                 # Trade logging + statistics
│   ├── integration.py                   # End-to-end integration helpers
│   └── tests.py                         # Unit + integration tests
├── regime_platform/
│   ├── auth/
│   │   ├── __init__.py                  # Re-exports all auth symbols
│   │   ├── jwt_auth.py                  # JWT auth + unified Clerk→JWT→API-key dep
│   │   └── clerk_auth.py               # Clerk JWT verification via JWKS
│   ├── core/
│   │   ├── regime_engine.py             # Dual GaussianHMM (vol + trend)
│   │   ├── position_sizer.py            # Fractional Kelly + vol-scaling + regime gating
│   │   ├── risk_manager.py              # VaR, CVaR, drawdown, stop/TP
│   │   └── gpu_engine.py                # pomegranate GPU HMM auto-select
│   ├── models/
│   │   ├── schemas.py                   # Pydantic v2 batch request/response models
│   │   └── stream_schemas.py            # Pydantic v2 streaming models
│   ├── routers/
│   │   ├── auth_router.py               # /auth/* endpoints
│   │   ├── pipeline.py                  # POST /analyse/full
│   │   ├── regime.py                    # POST /regime/detect
│   │   ├── sizing.py                    # POST /size/kelly
│   │   ├── risk.py                      # POST /risk/analyse
│   │   ├── simulate.py                  # POST /simulate/{symbol} (auth)
│   │   ├── portfolio.py                 # GET /portfolio (auth)
│   │   ├── feeds.py                     # /feeds/* (write auth)
│   │   ├── gpu.py                       # /gpu/* (auth)
│   │   ├── multi_asset.py               # /correlation/*, /optimise/* (auth)
│   │   ├── stream_rest.py               # /stream/* (REST tick ingestion)
│   │   ├── stream_ws.py                 # /ws/*, /sse/* (WebSocket + SSE)
│   │   ├── observation.py               # /observation/* (24-feature vectors)
│   │   ├── strategy.py                  # /strategy/*
│   │   ├── backtest.py                  # /backtest/*
│   │   └── tda.py                       # /tda/* (topological data analysis)
│   └── services/
│       ├── registry.py                  # Singleton session store
│       ├── stream_session.py            # Per-symbol stateful streaming engine
│       └── redis_persistence.py         # Upstash Redis session persistence
└── docs/
    ├── API.md                           # This file
    ├── openapi.yaml                     # Full OpenAPI 3.1 spec
    └── deployment.md                    # Deployment guide
```

### Pipeline Stages

| # | Stage | Module | Output |
|---|-------|--------|--------|
| 01 | RegimeHMM | `core/regime_engine.py` | RegimeSnapshot (vol_probs, trend_probs, risk_multiplier) |
| 02 | PositionSizer | `core/position_sizer.py` | KellyResult (full_kelly_f → fractional_f → vol_scaled_f → regime_gated_f) |
| 03 | RiskManager | `core/risk_manager.py` | RiskMetrics (VaR, CVaR, drawdown, stop, TP) |
| 04 | StreamSession | `services/stream_session.py` | StreamTick pushed to all WS / SSE subscribers |

### Streaming Architecture

Each symbol maintains an independent `StreamSession` managed by the `SessionRegistry` singleton. Sessions have three concurrent responsibilities:

| Responsibility | Mechanism | Latency |
|---------------|-----------|---------|
| **Tick path** | HMM predict → Kelly → VaR/CVaR → push to subscriber queues | ~7 ms/tick |
| **Background refit** | `asyncio.run_in_executor` — non-blocking EM fitting every N bars | ~200-500 ms, async |
| **Alert debounce** | 3-bar hysteresis before firing `RegimeAlert` | 3 bars delay |

> **Multiple concurrent subscribers**: Any number of WebSocket clients and SSE consumers can subscribe to the same symbol session. The session pushes to an `asyncio.Queue` per subscriber. Slow consumers drop ticks (queue bounded at 500 items) to prevent backpressure from blocking the tick path.

---

## Core Concepts

### Regime Detection

Two independent 3-state Gaussian HMMs are fitted on log-returns — one for **volatility regimes**, one for **trend regimes**. Each returns posterior state probabilities (not just Viterbi hard assignments), so confidence scores are smooth and differentiable.

| Dimension | States | Features |
|-----------|--------|----------|
| Volatility | `low` · `med_low` · `med_high` · `high` | 20-bar & 5-bar realised vol + \|return\| |
| Trend | `strong_bear` · `bear` · `bull` · `strong_bull` | Raw return + 10-bar & 30-bar cumulative return |

States are semantically labelled by sorting the HMM component means — no manual label assignment needed. The combined `regime_label` (e.g. `low_vol_bull`) is produced by joining the dominant vol and trend states.

### Kelly Criterion

Position sizes are computed using the continuous-returns Kelly formula:

```
f* = mu / sigma^2
```

where `mu` is the mean daily log-return and `sigma^2` is the variance. The raw Kelly fraction then passes through three scaling stages:

| Stage | Field | Formula |
|-------|-------|---------|
| 1. Fractional Kelly | `fractional_f` | `full_kelly_f × kelly_fraction` (default 0.5) |
| 2. Vol scaling | `vol_scaled_f` | `fractional_f × (target_vol / realised_annual_vol)` |
| 3. Regime gating | `regime_gated_f` | `vol_scaled_f × risk_multiplier` |

> **Warning**: Full Kelly maximises long-run geometric growth but implies roughly 50% drawdowns. The 50% fractional Kelly default reduces drawdown risk while preserving most of the compounding benefit.

### Risk Multiplier

Each regime maps to a `risk_multiplier` in the range [0.10, 1.75]:

```python
vol_scale   = {low: 1.75, med_low: 1.20, med_high: 0.70, high: 0.35}[vol_state]
trend_scale = {strong_bull: 1.00, bull: 0.88, bear: 0.60, strong_bear: 0.40}[trend_state]
risk_multiplier = clip(vol_scale * trend_scale, 0.10, 1.75)
```

### Regime Risk Multiplier Matrix

| | low vol | med_low vol | med_high vol | high vol |
|---|---|---|---|---|
| **strong_bull** | 1.75 | 1.20 | 0.70 | 0.35 |
| **bull** | 1.54 | 1.06 | 0.62 | 0.31 |
| **bear** | 1.05 | 0.72 | 0.42 | 0.21 |
| **strong_bear** | 0.70 | 0.48 | 0.28 | 0.14 |

### VaR and CVaR

Risk metrics use **historical (empirical) VaR/CVaR** rather than parametric estimates. This is more accurate for fat-tailed return distributions common in financial assets.

| Metric | Definition |
|--------|-----------|
| `var_95` | 5th percentile of the return distribution (95% of days lose less than this) |
| `cvar_95` | Mean loss in the worst 5% of days (Expected Shortfall) |
| `suggested_stop` | 2x CVaR95 — initial stop-loss suggestion |
| `suggested_tp` | 3x CVaR95 — take-profit at 3:1 reward/risk |

### Alert Severity Levels

| Severity | Trigger Condition | Action Suggested |
|----------|-------------------|-----------------|
| `critical` | New regime is `high_vol_bear` or `high_vol_neutral` | Reduce exposure immediately |
| `warning` | New regime contains `bear` or `high` (but not critical) | Review position sizing |
| `info` | All other transitions (e.g. neutral→bull) | Log and monitor |

> **3-bar debounce**: Alerts only fire when the new regime persists for 3 consecutive bars. This prevents noise-driven micro-transitions from flooding downstream consumers with false alarms.

---

## Renko HFT Pipeline (v5.0)

The Renko HFT pipeline is a 6-layer processing chain that converts raw price ticks into filtered, risk-managed trade signals:

```
Layer 1: Brick Engine       →  Renko bricks from price ticks
Layer 2: Swing Classifier   →  HH/HL/LH/LL labels on bricks
Layer 3: Pattern Detector   →  Breakout, triple-top/bottom signals
Layer 4: Signal Filter      →  Regime gate + velocity filter
Layer 5: Order Executor     →  Alpaca bracket order submission
Layer 6: Risk Manager       →  SL=3bricks / TP=5bricks, trailing stop
```

### Endpoints

#### Read Endpoints (Any authenticated user)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/renko/tick` | Process a single tick through the 6-layer pipeline |
| `POST` | `/renko/tick/batch` | Batch tick processing (backtest / warm-up) |
| `GET` | `/renko/state` | Current pipeline state |
| `GET` | `/renko/stats` | Comprehensive pipeline statistics |
| `GET` | `/renko/bricks` | Recent Renko bricks |
| `GET` | `/renko/classified` | Bricks with swing labels (HH/HL/LH/LL) |
| `GET` | `/renko/signals` | Pattern signals |
| `GET` | `/renko/trades` | Trade journal |
| `GET` | `/renko/swing-points` | Detected swing points |
| `GET` | `/renko/backtest/stats` | Backtest/trading statistics |

#### Write Endpoints (Trader or Admin)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/renko/regime` | Update the current HMM regime |
| `POST` | `/renko/equity` | Update the current account equity |

#### Admin Endpoints (Admin only)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/renko/config` | Update pipeline configuration (resets the pipeline) |
| `POST` | `/renko/reset` | Reset the pipeline for a symbol |

### Auth Level Summary

| Dependency | Endpoints | Roles |
|-----------|-----------|-------|
| `get_authed_user` | `/tick`, `/tick/batch`, `/state`, `/stats`, `/bricks`, `/classified`, `/signals`, `/trades`, `/swing-points`, `/backtest/stats` | viewer, trader, admin |
| `require_write` | `/regime`, `/equity` | trader, admin |
| `require_admin` | `/config`, `/reset` | admin only |

### Tick Input Schema

```json
{
  "price": 450.25,
  "timestamp": 1700000000.0,
  "symbol": "SPY"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `price` | float | Yes | — | Latest trade price |
| `timestamp` | float | No | `null` | Unix timestamp |
| `symbol` | string | No | `null` | Override symbol (defaults to SPY) |

### Batch Tick Input Schema

```json
{
  "prices": [450.1, 451.3, 449.8, ...],
  "timestamps": [1700000000, 1700000060, ...],
  "regimes": ["low_vol_bull", "low_vol_bull", ...],
  "symbol": "SPY"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `prices` | float[] | Yes | — | Price series |
| `timestamps` | float[] | No | `null` | Timestamps |
| `regimes` | string[] | No | `null` | Pre-computed regime labels |
| `symbol` | string | No | `null` | Override symbol |

### Config Input Schema

```json
{
  "brick_size": 0.50,
  "brick_size_mode": "fixed",
  "reversal_bricks": 2,
  "bull_trigger_n": 3,
  "bear_trigger_n": 3,
  "sl_bricks": 3,
  "tp_bricks": 5,
  "trailing_stop": true,
  "trail_after_bricks": 3,
  "trail_distance_bricks": 2,
  "max_trades_per_session": 15,
  "max_daily_loss_bricks": 10.0,
  "max_consecutive_losses": 3,
  "cooldown_seconds": 30.0,
  "regime_gate": true,
  "symbol": "SPY"
}
```

> **Warning**: Updating config via `POST /renko/config` **resets the entire pipeline** — all bricks, signals, and trade history are lost.

### Example: Process a Tick

```bash
curl -X POST https://noble-trader-fastapi-backend.onrender.com/renko/tick \
  -H "Authorization: Bearer <clerk-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"price": 450.25, "symbol": "SPY"}'
```

### Example: Batch Warm-up

```bash
curl -X POST "https://noble-trader-fastapi-backend.onrender.com/renko/tick/batch?symbol=SPY" \
  -H "Authorization: Bearer <clerk-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"prices": [445.0, 446.0, 447.5, ...], "symbol": "SPY"}'
```

---

## Batch API

### POST /analyse/full

One-shot full pipeline — regime + sizing + risk in a single request.

**Request Body** (FullAnalysisRequest):

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `symbol` | string | No | `UNKNOWN` | Ticker label |
| `prices` | float[] | Yes | — | Ordered close price series (min 81 values, all positive) |
| `kelly_fraction` | float | No | `0.5` | Fractional Kelly multiplier [0.1, 1.0] |
| `target_vol` | float | No | `0.15` | Target annualised volatility (0, 1.0] |
| `base_risk_limit` | float | No | `0.02` | Base risk limit (0, 0.5] |

**Response** (FullAnalysisResponse):

```json
{
  "symbol": "SPY",
  "regime": {
    "symbol": "SPY",
    "vol_probs": {"low": 0.72, "medium": 0.20, "high": 0.08},
    "trend_probs": {"bear": 0.05, "neutral": 0.15, "bull": 0.80},
    "vol_state": "low",
    "trend_state": "bull",
    "regime_label": "low_vol_bull",
    "confidence": 0.72,
    "risk_multiplier": 1.35,
    "n_bars_fitted": 200
  },
  "sizing": {
    "symbol": "SPY",
    "full_kelly_f": 0.68,
    "fractional_f": 0.34,
    "vol_scaled_f": 0.31,
    "regime_gated_f": 0.42,
    "recommended_f": 0.42,
    "kelly_edge": 0.025,
    "sharpe_ratio": 1.85,
    "regime_label": "low_vol_bull",
    "regime_multiplier": 1.35,
    "fraction_type": "50% Kelly",
    "notes": ["Regime gate: low_vol_bull -> 1.35x"]
  },
  "risk": {
    "symbol": "SPY",
    "recommended_f": 0.42,
    "var_95": 0.0187,
    "var_99": 0.0312,
    "cvar_95": 0.0245,
    "cvar_99": 0.0389,
    "max_drawdown": 0.087,
    "annual_vol": 0.14,
    "annual_return": 0.22,
    "calmar_ratio": 2.53,
    "sortino_ratio": 2.87,
    "regime_label": "low_vol_bull",
    "regime_max_loss": 0.025,
    "suggested_stop": 0.049,
    "suggested_tp": 0.0735,
    "risk_budget_used": 0.35,
    "notes": ["Stop at 2x CVaR95", "TP at 3x CVaR95 (3:1 R:R)"]
  }
}
```

### POST /regime/detect

HMM regime detection only. Same `PricePayload` body as `/analyse/full`.

### POST /size/kelly

Kelly position sizing only. Accepts `SizeRequest` with optional `returns` array and `use_regime` flag.

### POST /risk/analyse

Risk metrics only. Accepts `RiskRequest` (extends `SizeRequest` with `base_risk_limit`).

---

## Streaming API

### POST /stream/seed

Seed a symbol streaming session with historical prices.

**Request Body** (SeedRequest):

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `symbol` | string | Yes | — | Symbol identifier |
| `prices` | float[] | Yes | — | Historical prices (min 81, all positive) |
| `window` | int | No | `500` | Rolling window size [51, 5000] |
| `kelly_fraction` | float | No | `0.5` | Kelly multiplier [0.1, 1.0] |
| `target_vol` | float | No | `0.15` | Target volatility (0, 1.0] |
| `base_risk_limit` | float | No | `0.02` | Base risk limit (0, 0.5] |
| `refit_every` | int | No | `50` | Refit HMM every N bars [5, 500] |

### POST /stream/tick

Push one price tick and receive a regime snapshot.

**Request Body** (TickIngest):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | Yes | Symbol identifier |
| `price` | float | Yes | Price (must be positive) |
| `ts` | float | No | Unix timestamp |

**Response**: Returns `TickResponse` with regime snapshot, or `BufferingStub` if not enough bars yet.

### GET /stream/sessions

List all active streaming sessions with stats per symbol.

### WebSocket /ws/{symbol}

Full-duplex stream. Supports `?token=<jwt>` or `?api_key=<key>` for auth.

**Client → Server messages**:
- `{"type": "seed", "symbol": "SPY", "prices": [...]}`
- `{"type": "tick", "symbol": "SPY", "price": 450.25}`

**Server → Client messages**:
- `{"type": "seeded", "symbol": "SPY", "n_bars": 200, ...}`
- `{"type": "tick", "regime_label": "low_vol_bull", "recommended_f": 0.42, ...}`
- `{"type": "alert", "severity": "critical", ...}`

### SSE /sse/{symbol}

Server-Sent Events stream for a single symbol. Each event is a JSON `TickResponse`.

### SSE /sse/alerts

Global regime-change alert broadcast. Events are `RegimeAlert` objects:

```json
{
  "type": "alert",
  "symbol": "SPY",
  "ts": 1700000000,
  "previous": "low_vol_bull",
  "current": "high_vol_bear",
  "severity": "critical",
  "message": "Regime changed to high_vol_bear"
}
```

---

## Simulation

### POST /simulate/{symbol}

Simulate regime transition paths forward in time using Monte Carlo methods.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `prices` | float[] | Yes | — | Price series (min 81) |
| `horizon` | int | No | `20` | Forward bars to simulate [1, 252] |
| `n_paths` | int | No | `500` | Monte Carlo paths [50, 5000] |
| `seed` | int | No | `42` | Random seed |

**Auth**: Any authenticated user.

---

## Portfolio

### GET /portfolio

Multi-symbol aggregated regime + risk overview.

**Auth**: Any authenticated user.

---

## GPU HMM (v3.0)

### GET /gpu/capabilities

Get GPU HMM backend capabilities (pomegranate auto-selection).

**Auth**: Any authenticated user.

### POST /gpu/benchmark

Run HMM fit/predict latency benchmark.

**Auth**: Any authenticated user.

---

## Feed Adapters (v3.0)

Live OHLCV feed management for Alpaca, Binance, and Interactive Brokers.

### GET /feeds/status

Get feed adapter status, bar counts, and health.

**Auth**: Write (trader or admin).

### POST /feeds/start

Start live feed adapters.

**Auth**: Write (trader or admin).

### POST /feeds/stop

Stop all running feed adapters.

**Auth**: Write (trader or admin).

---

## Correlation & Optimisation (v3.0)

### POST /correlation/detect

DCC-based portfolio correlation regime detection.

**Auth**: Any authenticated user.

### POST /optimise

Drawdown-controlled max-Sharpe portfolio optimisation.

**Auth**: Any authenticated user.

### POST /optimise/full

Correlation + optimisation one-shot.

**Auth**: Any authenticated user.

---

## Observation Builder

### POST /observation/build

Build a 24-feature observation vector from the InferenceObservationBuilder.

**Feature Index Reference**:

| Index | Feature | Index | Feature |
|-------|---------|-------|---------|
| 0 | log_ret_1bar | 12 | regime_prob_2 |
| 1 | log_ret_3bar | 13 | regime_prob_3 |
| 2 | log_ret_10bar | 14 | vol_prob_low |
| 3 | norm_atr | 15 | vol_prob_medium |
| 4 | rolling_vol_20 | 16 | vol_prob_high |
| 5 | ema_distance | 17 | trend_prob_down |
| 6 | hhll_score | 18 | trend_prob_neutral |
| 7 | vol_regime_pct | 19 | trend_prob_up |
| 8 | atr_vs_baseline | 20 | regime_quality |
| 9 | vol_slope | 21 | state_confidence |
| 10 | regime_prob_0 | 22 | masaniello_pressure |
| 11 | regime_prob_1 | 23 | drawdown_factor |

---

## Discord Notifications (v5.0)

The Renko pipeline sends real-time Discord notifications via rich embeds to three webhook channels:

| Channel | Webhook Env Var | Trigger |
|---------|----------------|---------|
| `#trade-signals` | `DISCORD_WEBHOOK_SIGNALS` | Pattern signal detected (breakout, triple-top, etc.) |
| `#trade-executions` | `DISCORD_WEBHOOK_EXECUTIONS` | Order submitted, SL/TP hit, trade closed |
| `#system-status` | `DISCORD_WEBHOOK_STATUS` | Pipeline reset, batch complete, risk alerts |

### Notification Flow

1. **Signal detected** (Layer 4 filter passes) → `#trade-signals` embed with direction, pattern, confidence, regime
2. **Execution submitted** (Layer 5) → `#trade-executions` embed with side, qty, price, SL, TP
3. **Trade closed** (SL/TP hit) → `#trade-executions` embed with PnL in bricks and dollars
4. **Large loss** (≤ -5 bricks) → `#system-status` risk alert
5. **Pipeline reset** → `#system-status` warning notification
6. **Batch complete** (≥50 ticks) → `#system-status` info notification

All notifications are fire-and-forget (non-blocking) and gracefully degrade if Discord is unavailable.

---

## Reference

### Error Responses

| Code | Meaning | When |
|------|---------|------|
| `401` | Unauthorized | Missing or invalid auth credentials |
| `403` | Forbidden | Insufficient role (e.g. viewer trying write endpoint) |
| `404` | Not Found | Symbol session doesn't exist |
| `422` | Validation Error | Invalid request body (Pydantic validation) |
| `500` | Internal Error | Server-side failure |

### Environment Variables (Complete)

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | Listening port |
| `WORKERS` | `1` | Uvicorn worker count |
| `LOG_LEVEL` | `info` | Uvicorn log level: `debug` \| `info` \| `warning` \| `error` |
| `JWT_SECRET_KEY` | _(empty)_ | Local JWT signing secret |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINS` | `60` | Token lifetime in minutes |
| `API_KEYS` | _(empty)_ | Comma-separated valid API keys |
| `AUTH_ENABLED` | `true` | Set `false` to disable auth |
| `CLERK_PUBLISHABLE_KEY` | _(empty)_ | Clerk client-side key |
| `CLERK_SECRET_KEY` | _(empty)_ | Clerk server-side key |
| `UPSTASH_REDIS_REST_URL` | _(empty)_ | Upstash Redis REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | _(empty)_ | Upstash Redis REST token |
| `DISCORD_WEBHOOK_SIGNALS` | _(empty)_ | Discord webhook for trade signals |
| `DISCORD_WEBHOOK_EXECUTIONS` | _(empty)_ | Discord webhook for trade executions |
| `DISCORD_WEBHOOK_STATUS` | _(empty)_ | Discord webhook for system status |

### Key Libraries

| Library | Purpose |
|---------|---------|
| `fastapi` | API framework |
| `pydantic v2` | Request/response validation |
| `hmmlearn` | Gaussian HMM fitting |
| `scipy` | Kelly formula, optimisation, stats |
| `numpy` | Array operations, VaR/CVaR |
| `pandas` | Data manipulation |
| `python-jose` | JWT encoding/decoding |
| `httpx` | Async HTTP client (Discord webhooks) |
| `pomegranate` | GPU-accelerated HMM (optional) |
| `uvicorn` | ASGI server |
| `fastapi-mcp` | Model Context Protocol exposure (optional) |

### Performance Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| Tick latency (p50) | ~7 ms | HMM predict + Kelly + VaR/CVaR |
| HMM refit latency | 200-500 ms | Background, non-blocking |
| Max concurrent sessions | ~500 | Per worker, memory-bound |
| Price buffer memory | ~32 KB | Per session at window=500 |
| SSE fan-out overhead | ~0.1 ms/subscriber | Queue put_nowait |

---

*See also: [openapi.yaml](./openapi.yaml) for the full OpenAPI 3.1 spec and [deployment.md](./deployment.md) for operational reference.*
