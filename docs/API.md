# Noble Trader Dynamic Regime Risk Management Platform — API Reference

**Version:** 5.1.0  
**Base URL:** `https://noble-trader-fastapi-backend.onrender.com`  
**Stack:** FastAPI · hmmlearn · scipy · numpy · pandas · pydantic v2  
**Interactive Docs:** `/docs` (Swagger UI) · `/redoc` (ReDoc)  
**MCP Server:** `/mcp` (Model Context Protocol — exposes all endpoints as AI agent tools)

---

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Regime Detection](#regime-detection)
- [Position Sizing](#position-sizing)
- [Risk Analysis](#risk-analysis)
- [Strategy](#strategy)
- [Full Pipeline](#full-pipeline)
- [Backtest](#backtest)
- [Multi-Asset](#multi-asset)
- [Observation Builder](#observation-builder)
- [TDA Features](#tda-features)
- [Portfolio](#portfolio)
- [Simulation](#simulation)
- [Streaming](#streaming)
- [GPU](#gpu)
- [Feed Adapters](#feed-adapters)
- [Renko HFT Pipeline](#renko-hft-pipeline)
- [Health](#health)
- [Recommended Workflows](#recommended-workflows)

---

## Overview

The Noble Trader Platform is a quantitative trading risk management web service that provides:

| Component | Library | Purpose |
|-----------|---------|---------|
| **Regime Detection** | `hmmlearn` GaussianHMM | Classify market into volatility × trend regimes |
| **Position Sizing** | `scipy` + Kelly formula | Fractional Kelly with vol-scaling & regime gating |
| **Risk Analysis** | `numpy` / `scipy.stats` | VaR, CVaR, drawdown, Sortino, Calmar |
| **Correlation** | `hmmlearn` + DCC | Portfolio-level correlation regime detection |
| **Optimisation** | `scipy.optimize` | Drawdown-controlled max-Sharpe portfolio |
| **Dynamic Masaniello** | Adaptive batch sizing | Progress-dependent position sizing with regime quality overlay |
| **Renko HFT** | 6-layer pipeline | Brick construction → swing classification → pattern detection → signal filter → risk manager → execution |
| **GPU HMM** | `pomegranate` (optional) | 10-50× HMM speedup on CUDA/MPS |
| **API Framework** | `FastAPI` + `pydantic v2` | Typed, validated, auto-documented REST API |

### Regime Labels

The HMM produces a **2D regime** combining:
- **Volatility**: `low` / `med_low` / `med_high` / `high`
- **Trend**: `strong_bear` / `bear` / `bull` / `strong_bull`

Combined label example: `low_vol_bull`, `high_vol_bear`, etc.

### Risk Multiplier

Each regime maps to a **risk_multiplier** [0.25 – 1.50] that scales the Kelly fraction:
- `low_vol_bull` → 1.5× (max risk-on)
- `high_vol_bear` → 0.25× (max defensive)

---

## Authentication

The platform supports three authentication methods, tried in priority order:

1. **Clerk JWT** (primary) — verified via Clerk's JWKS endpoint
2. **Local JWT** — signed with `JWT_SECRET_KEY`, issued via `/auth/token`
3. **API Key** — `X-API-Key` header for service-to-service calls

When `AUTH_ENABLED=false` (dev mode), all requests are treated as `admin`.

### Roles

| Role | Access Level |
|------|-------------|
| `admin` | Full access — all endpoints, configuration, feed management |
| `trader` | Read + write — sizing, risk, streaming, simulation |
| `viewer` | Read-only — regime detection, portfolio view, health checks |

### Environment Variables

| Variable | Description |
|----------|-------------|
| `JWT_SECRET_KEY` | Signing secret for local JWTs (required in production) |
| `JWT_ALGORITHM` | Default `HS256` |
| `JWT_EXPIRE_MINS` | Token lifetime in minutes (default 60) |
| `API_KEYS` | Comma-separated valid API keys |
| `AUTH_ENABLED` | Set `false` to disable auth (dev mode) |
| `AUTH_USERS` | `"user1:pass1:role1,user2:pass2:role2"` |
| `CLERK_SECRET_KEY` | Clerk JWT verification key |

### Endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/auth/token` | Issue a JWT access token | None |
| POST | `/auth/refresh` | Refresh a non-expired token | JWT |
| GET | `/auth/me` | Return current user info from token | JWT |
| POST | `/auth/clerk/verify` | Verify a Clerk JWT token | None |
| GET | `/auth/clerk/config` | Get Clerk authentication configuration | None |
| GET | `/auth/clerk/me` | Return Clerk user info from Clerk JWT | Clerk JWT |

#### POST /auth/token

Standard OAuth2 password flow. Supply `username` and `password` as form fields.

**Request** (form-data):

| Field | Type | Description |
|-------|------|-------------|
| `username` | string | Login username |
| `password` | string | Login password |

**Response:**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 3600,
  "sub": "admin",
  "role": "admin"
}
```

#### POST /auth/refresh

Issues a fresh token with a new expiry, keeping the same subject and role. Requires a valid (non-expired) Bearer token.

**Response:** Same as `/auth/token`.

#### GET /auth/me

Returns the identity and role extracted from the Bearer JWT.

**Response:**

```json
{
  "sub": "admin",
  "role": "admin",
  "auth_enabled": true
}
```

#### POST /auth/clerk/verify

Verifies a Clerk JWT token and returns decoded user information.

**Request:**

```json
{
  "token": "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..."
}
```

**Response:**

```json
{
  "valid": true,
  "user": {
    "sub": "user_2xyz...",
    "role": "viewer",
    "email": "trader@example.com",
    "first_name": "Jane",
    "last_name": "Trader",
    "username": "janetrader",
    "clerk_auth_enabled": true
  },
  "error": null
}
```

#### GET /auth/clerk/config

Returns Clerk authentication configuration status (publishable key truncated for security).

**Response:**

```json
{
  "clerk_auth_enabled": true,
  "publishable_key": "pk_test_X...",
  "jwt_algorithm": "RS256",
  "jwks_endpoint": "https://large-shark-21.clerk.accounts.dev/.well-known/jwks.json"
}
```

#### GET /auth/clerk/me

Returns Clerk user identity extracted from a valid Clerk JWT in the Authorization header.

**Response:** Same `ClerkUserInfo` as in `/auth/clerk/verify`.

---

## Regime Detection

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/regime/detect` | Detect market regime from price series | None |
| POST | `/regime/detect-v2` | Enhanced regime detection with configurable 2-4 states | None |

### POST /regime/detect

Fits dual Gaussian HMMs (volatility + trend) on the supplied price series and returns posterior regime probabilities plus a risk multiplier.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (OHLCV close preferred). Min 81 bars. |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol for labelling |

**Example Request:**

```json
{
  "prices": [450.10, 451.20, 449.80, ...],
  "symbol": "SPY"
}
```

**Response:**

```json
{
  "symbol": "SPY",
  "vol_probs": {"low": 0.65, "med_low": 0.20, "med_high": 0.10, "high": 0.05},
  "trend_probs": {"strong_bear": 0.02, "bear": 0.08, "bull": 0.60, "strong_bull": 0.30},
  "vol_state": "low",
  "trend_state": "bull",
  "regime_label": "low_vol_bull",
  "confidence": 0.78,
  "risk_multiplier": 1.50,
  "n_bars_fitted": 200
}
```

### POST /regime/detect-v2

Enhanced HMM regime detection with configurable state count, auto-labeled states, transition entropy, and regime stability.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `n_states` | `int` | `4` | Number of HMM states per dimension (2-4) |
| `n_iter` | `int` | `100` | Maximum EM iterations (10-500) |

**State Configurations:**

| n_states | Volatility Labels | Trend Labels | Combined Regimes |
|----------|-------------------|-------------|-----------------|
| 2 | `low`, `high` | `bear`, `bull` | 4 |
| 3 | `low`, `medium`, `high` | `bear`, `neutral`, `bull` | 9 |
| 4 | `low`, `med_low`, `med_high`, `high` | `strong_bear`, `bear`, `bull`, `strong_bull` | 16 |

**Response:**

```json
{
  "symbol": "SPY",
  "n_states": 3,
  "vol_probs": {"low": 0.55, "medium": 0.30, "high": 0.15},
  "trend_probs": {"bear": 0.10, "neutral": 0.25, "bull": 0.65},
  "vol_state": "low",
  "trend_state": "bull",
  "regime_label": "low_vol_bull",
  "confidence": 0.82,
  "risk_multiplier": 1.35,
  "n_bars_fitted": 200,
  "transition_entropy": 1.45,
  "regime_stability": 0.85
}
```

---

## Position Sizing

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/size/kelly` | Regime-aware fractional Kelly position sizing (Path A) | None |
| POST | `/size/masaniello` | Dynamic Masaniello position sizing (Path B) | JWT/Key |
| POST | `/size/masaniello/batch` | Batch Dynamic Masaniello sizing | JWT/Key |
| POST | `/size/masaniello/portfolio` | Portfolio-optimised Masaniello sizing across multiple assets | JWT/Key |

### POST /size/kelly

Computes fractional Kelly position size, optionally scaled by current realised volatility vs target volatility and the dynamic regime risk multiplier from HMM.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `returns` | `list[float]` | `null` | Use explicit returns instead of computing from prices |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly multiplier (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target volatility for scaling (0–1.0) |
| `use_regime` | `bool` | `true` | Enable regime-gated sizing |

**Response:**

```json
{
  "symbol": "SPY",
  "full_kelly_f": 0.18,
  "fractional_f": 0.09,
  "vol_scaled_f": 0.075,
  "regime_gated_f": 0.1125,
  "recommended_f": 0.1125,
  "kelly_edge": 0.035,
  "sharpe_ratio": 1.42,
  "regime_label": "low_vol_bull",
  "regime_multiplier": 1.50,
  "fraction_type": "half_kelly",
  "notes": ["Regime gate: 1.50x multiplier applied (low_vol_bull)"]
}
```

### POST /size/masaniello

Computes position size using the **Dynamic Masaniello formula**:

```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
```

Where:
- **β** = base risk fraction (default 0.5%)
- **M_i** = Masaniello progress factor — urgency = wins_needed / trades_remaining
- **Q_i** = Model quality composite — prob × regime_quality × confidence
- **DD_i** = Drawdown scaling factor — never below 0.25
- **V_i** = Volatility adjustment — ATR-based ratio

Supports optional Kelly overlay cap and regime auto-detection from prices.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `equity` | `float` | *required* | Current account equity in $ |
| `stop_distance_price` | `float` | *required* | Stop distance in price units |
| `point_value` | `float` | `1.0` | $ value per 1 price unit (1.0 for stocks/FX) |
| `p_win` | `float` | *required* | Estimated win probability (0–1) |
| `reward_risk` | `float` | *required* | Expected reward/risk ratio (R multiple) |
| `direction` | `string` | `"long"` | `"long"` or `"short"` |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `atr_baseline` | `float` | *required* | Historical ATR reference |
| `atr_current` | `float` | *required* | Current ATR value |
| `prices` | `list[float]` | `null` | Price series for auto regime detection (min 81) |
| `wins_so_far` | `int` | `0` | Wins in current batch |
| `losses_so_far` | `int` | `0` | Losses in current batch |
| `trade_index` | `int` | `1` | Trade number within batch (≥1) |
| `current_drawdown` | `float` | `0.0` | Current drawdown fraction (0–1) |
| `base_risk` | `float` | `0.005` | Base risk fraction β (0–5%) |
| `min_risk` | `float` | `0.0025` | Minimum risk fraction |
| `max_risk` | `float` | `0.010` | Maximum risk fraction |
| `min_prob` | `float` | `0.50` | Minimum win probability to trade |
| `min_rr` | `float` | `1.50` | Minimum reward/risk ratio |
| `use_regime` | `bool` | `true` | Auto-detect regime from prices |
| `use_kelly_overlay` | `bool` | `false` | Apply Kelly cap on top of Masaniello |
| `kelly_fraction` | `float` | `0.25` | Kelly fraction for overlay (0.01–1.0) |
| `batch_size` | `int` | `5` | Trades per Masaniello batch (2–50) |
| `target_wins` | `int` | `3` | Target wins per batch (1–25) |

**Example Request:**

```json
{
  "equity": 100000,
  "stop_distance_price": 2.50,
  "point_value": 1.0,
  "p_win": 0.62,
  "reward_risk": 2.0,
  "direction": "long",
  "symbol": "SPY",
  "atr_baseline": 5.20,
  "atr_current": 4.80,
  "prices": [450.10, 451.20, 449.80, 452.30, ...],
  "current_drawdown": 0.02,
  "base_risk": 0.005,
  "use_regime": true
}
```

**Example Response:**

```json
{
  "allowed": true,
  "risk_fraction": 0.0063,
  "risk_amount": 630.00,
  "units": 252.0,
  "contracts": 252,
  "masaniello_factor": 0.60,
  "quality_factor": 0.93,
  "drawdown_factor": 0.95,
  "volatility_factor": 1.08,
  "expected_edge": 0.24,
  "kelly_cap": null,
  "reason": "Masaniello sizing: f=0.63% (M=0.60, Q=0.93, DD=0.95, V=1.08)",
  "regime_label": "low_vol_bull",
  "regime_quality": 1.50,
  "batch_num": 1,
  "trade_index": 1,
  "wins_so_far": 0,
  "losses_so_far": 0,
  "drawdown": 0.02,
  "symbol": "SPY",
  "direction": "long"
}
```

### POST /size/masaniello/batch

Sizes an entire batch of trades sequentially, with batch state (wins, losses, drawdown) flowing between trades automatically. Optionally provide outcomes and PnLs to update batch state as the batch is sized.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `equity` | `float` | *required* | Current account equity |
| `point_value` | `float` | `1.0` | $ per 1 price unit |
| `direction` | `string` | `"long"` | Direction for all trades |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `atr_baseline` | `float` | *required* | Historical ATR reference |
| `stop_distances` | `list[float]` | *required* | Stop distance per trade (1–50 items) |
| `p_wins` | `list[float]` | *required* | Win probability per trade (1–50 items) |
| `reward_risks` | `list[float]` | *required* | R multiple per trade (1–50 items) |
| `atr_currents` | `list[float]` | *required* | Current ATR per trade (1–50 items) |
| `outcomes` | `list[bool]` | `null` | Win/loss outcomes (True=win, False=loss) |
| `pnls` | `list[float]` | `null` | PnL per trade |
| `base_risk` | `float` | `0.005` | Base risk fraction β |
| `min_rr` | `float` | `1.50` | Minimum reward/risk ratio |
| `batch_size` | `int` | `5` | Trades per batch (2–50) |
| `target_wins` | `int` | `3` | Target wins per batch (1–25) |
| `use_kelly_overlay` | `bool` | `false` | Apply Kelly cap |
| `kelly_fraction` | `float` | `0.25` | Kelly fraction for overlay |
| `prices` | `list[float]` | `null` | For auto regime detection (min 81) |

**Example Request:**

```json
{
  "equity": 100000,
  "symbol": "SPY",
  "atr_baseline": 5.20,
  "direction": "long",
  "stop_distances": [2.50, 2.30, 2.60],
  "p_wins": [0.62, 0.58, 0.65],
  "reward_risks": [2.0, 2.5, 1.8],
  "atr_currents": [4.80, 5.10, 4.70]
}
```

**Example Response:**

```json
{
  "results": [
    {
      "allowed": true,
      "risk_fraction": 0.0063,
      "risk_amount": 630.0,
      "units": 252.0,
      "contracts": 252,
      "masaniello_factor": 0.60,
      "quality_factor": 0.93,
      "drawdown_factor": 1.00,
      "volatility_factor": 1.08,
      "expected_edge": 0.24,
      "kelly_cap": null,
      "reason": "Masaniello sizing: f=0.63%",
      "regime_label": "low_vol_bull",
      "symbol": "SPY",
      "direction": "long"
    }
  ],
  "batch_summary": {
    "batch_num": 1,
    "wins": 0,
    "losses": 0,
    "pnl": 0.0,
    "drawdown": 0.0,
    "trade_index": 3
  }
}
```

### POST /size/masaniello/portfolio

Two-stage pipeline for multi-asset position sizing:

**Stage 1** — `DrawdownOptimiser` → regime-adjusted portfolio weights (what to hold)  
**Stage 2** — `DynamicMasanielloSizer` → per-asset contract sizes (how much to hold)

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbols` | `list[str]` | *required* | Asset tickers (2–50) |
| `returns_matrix` | `list[list[float]]` | *required* | (n_bars × n_assets) matrix of log-returns |
| `equity` | `float` | *required* | Total account equity |
| `stop_distances` | `list[float]` | *required* | Stop distance per asset |
| `point_values` | `list[float]` | `null` | $ per 1 price unit per asset (null = all 1.0) |
| `p_wins` | `list[float]` | *required* | Win probability per asset |
| `reward_risks` | `list[float]` | *required* | R multiple per asset |
| `atr_baselines` | `list[float]` | *required* | Historical ATR per asset |
| `atr_currents` | `list[float]` | *required* | Current ATR per asset |
| `directions` | `list[str]` | `null` | Direction per asset (default all `"long"`) |
| `max_dd_limit` | `float` | `0.20` | Max acceptable expected drawdown (0.01–1.0) |
| `max_weight` | `float` | `0.40` | Maximum single-asset weight (0.05–1.0) |
| `base_risk` | `float` | `0.005` | Masaniello base risk β |
| `min_rr` | `float` | `1.50` | Min reward/risk for Masaniello |
| `batch_size` | `int` | `5` | Trades per Masaniello batch |
| `target_wins` | `int` | `3` | Target wins per batch |
| `use_kelly_overlay` | `bool` | `false` | Apply Kelly cap on Masaniello |
| `kelly_fraction` | `float` | `0.25` | Kelly fraction for overlay |

**Response:** See [`PortfolioMasanielloResponse`](#post-optimisemasaniello) below.

---

## Risk Analysis

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/risk/analyse` | Full risk analysis: VaR, CVaR, drawdown, stop/TP suggestions | None |
| POST | `/risk/analyze` | Enhanced risk analysis with stress tests and regime context | None |

### POST /risk/analyse

Computes comprehensive risk metrics including parametric + historical VaR/CVaR, max drawdown, Sortino & Calmar ratios, and regime-adjusted stop-loss/take-profit.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `returns` | `list[float]` | `null` | Explicit returns (optional) |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target vol (0–1.0) |
| `use_regime` | `bool` | `true` | Enable regime gating |
| `base_risk_limit` | `float` | `0.02` | Max daily loss fraction (0–0.5) |

**Response:**

```json
{
  "symbol": "SPY",
  "recommended_f": 0.1125,
  "var_95": 0.0128,
  "var_99": 0.0214,
  "cvar_95": 0.0189,
  "cvar_99": 0.0295,
  "max_drawdown": 0.0842,
  "annual_vol": 0.142,
  "annual_return": 0.185,
  "calmar_ratio": 2.20,
  "sortino_ratio": 2.85,
  "regime_label": "low_vol_bull",
  "regime_max_loss": 0.008,
  "suggested_stop": 439.25,
  "suggested_tp": 468.50,
  "risk_budget_used": 0.45,
  "notes": ["Regime gate: 1.50x multiplier applied"]
}
```

### POST /risk/analyze

Enhanced risk analysis (US spelling) with five built-in stress test scenarios.

**Additional Request Fields** (beyond `/risk/analyse`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `n_hmm_states` | `int` | `4` | HMM states (2–4) |
| `stress_scenarios` | `bool` | `true` | Include stress tests |

**Stress Test Scenarios:**

| Scenario | Description |
|----------|-------------|
| `2008_crash` | Sustained -2%/day with elevated vol |
| `flash_crash` | -10% single-day drop |
| `volatility_spike` | 3× normal vol for 20 bars |
| `rate_shock` | +2% rates reducing equity returns |
| `liquidity_crisis` | Wider spreads and asymmetric slippage |

**Response:** Same as `/risk/analyse` plus:

```json
{
  "stress_tests": [
    {
      "scenario": "2008_crash",
      "description": "2008-style crash: sustained -2%/day with elevated vol",
      "simulated_return": -0.482,
      "simulated_var_95": 0.0456,
      "simulated_max_dd": -0.512
    },
    {
      "scenario": "flash_crash",
      "description": "Flash crash: -10% single-day drop",
      "simulated_return": -0.115,
      "simulated_var_95": 0.1005,
      "simulated_max_dd": -0.115
    }
  ]
}
```

---

## Strategy

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/strategy/signal` | Generate strategy signal with regime context and position sizing | None |

### POST /strategy/signal

Full strategy signal pipeline:
1. Fit HMM regime model → detect current regime
2. Generate trading signal based on regime (`long` / `short` / `flat`)
3. Compute regime-gated Kelly position size
4. Compute risk metrics and guidance (stop/TP, VaR/CVaR)
5. Return combined signal with all context

**Signal Logic:**
- **Long**: low/med_low vol + bull/strong_bull trend
- **Short**: high/med_high vol + bear/strong_bear trend (reduced strength × 0.5)
- **Flat**: all other regimes

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target vol (0–1.0) |
| `use_regime` | `bool` | `true` | Enable regime gating |
| `base_risk_limit` | `float` | `0.02` | Max daily loss (0–0.5) |
| `n_hmm_states` | `int` | `4` | HMM states (2–4) |

**Response:**

```json
{
  "symbol": "SPY",
  "signal": "long",
  "signal_strength": 0.975,
  "confidence": 0.65,
  "regime_label": "low_vol_bull",
  "vol_state": "low",
  "trend_state": "bull",
  "risk_multiplier": 1.50,
  "recommended_f": 0.1125,
  "full_kelly_f": 0.18,
  "fractional_f": 0.09,
  "vol_scaled_f": 0.075,
  "regime_gated_f": 0.1125,
  "sharpe_ratio": 1.42,
  "suggested_stop": 439.25,
  "suggested_tp": 468.50,
  "var_95": 0.0128,
  "cvar_95": 0.0189,
  "notes": ["Regime gate: 1.50x multiplier applied"]
}
```

---

## Full Pipeline

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/analyse/full` | One-shot: regime + Kelly sizing + risk metrics | None |

### POST /analyse/full

Runs the complete pipeline in a single request:
1. Fit HMM regime model → get vol/trend posteriors
2. Compute regime-gated Kelly position size
3. Compute VaR, CVaR, drawdown, and trade guidance

Returns a nested JSON with all three result objects.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target vol (0–1.0) |
| `base_risk_limit` | `float` | `0.02` | Max daily loss (0–0.5) |

**Response:**

```json
{
  "symbol": "SPY",
  "regime": { "...RegimeResponse..." },
  "sizing": { "...SizeResponse..." },
  "risk": { "...RiskResponse..." }
}
```

---

## Backtest

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/backtest/run` | Run walk-forward backtest with regime-aware strategy (30+ metrics) | None |

### POST /backtest/run

Walk-forward backtest pipeline:
1. Rolling HMM fit on trailing window
2. Strategy signal generation based on regime
3. Regime-gated Kelly position sizing
4. Risk check gates (VaR, max drawdown)
5. Trade execution with commission/slippage
6. Comprehensive performance metrics (30+)

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `window` | `int` | `200` | Trailing window for HMM fit (≥81) |
| `refit_every` | `int` | `50` | Refit HMM every N bars (≥10) |
| `n_hmm_states` | `int` | `4` | HMM states (2–4) |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target vol (0–1.0) |
| `base_risk_limit` | `float` | `0.02` | Max daily loss (0–0.5) |
| `initial_equity` | `float` | `100000` | Starting equity |
| `commission_bps` | `float` | `5.0` | Commission in basis points (0–50) |
| `slippage_bps` | `float` | `2.0` | Slippage in basis points (0–50) |
| `max_position_pct` | `float` | `0.25` | Max position as % of equity (0–1.0) |
| `risk_check` | `bool` | `true` | Enable risk check gates |
| `regime_gate` | `bool` | `true` | Enable regime gate filter |

**Response:**

```json
{
  "symbol": "SPY",
  "n_bars": 1250,
  "n_trades": 42,
  "n_wins": 28,
  "n_losses": 14,
  "win_rate": 0.667,
  "total_return": 0.245,
  "annual_return": 0.152,
  "buy_hold_return": 0.108,
  "excess_return": 0.137,
  "monthly_return_avg": 0.0127,
  "max_drawdown": 0.068,
  "max_drawdown_duration": 18,
  "avg_drawdown": 0.022,
  "var_95": 0.0142,
  "cvar_95": 0.0218,
  "annual_vol": 0.118,
  "downside_vol": 0.082,
  "sharpe_ratio": 1.29,
  "sortino_ratio": 1.85,
  "calmar_ratio": 2.24,
  "information_ratio": 0.92,
  "avg_win_pct": 0.028,
  "avg_loss_pct": -0.015,
  "avg_win_loss_ratio": 1.87,
  "max_consecutive_wins": 7,
  "max_consecutive_losses": 3,
  "avg_hold_bars": 12.5,
  "profit_factor": 3.73,
  "regime_distribution": {"low_vol_bull": 0.45, "high_vol_bear": 0.12, "...": "..."},
  "trades_by_regime": {"low_vol_bull": {"n": 22, "win_rate": 0.82}, "...": "..."},
  "equity_curve": [100000, 100250, 100100, ...],
  "drawdown_curve": [0.0, -0.002, -0.005, ...],
  "trade_log": [{"entry_bar": 201, "exit_bar": 215, "pnl": 1250.0, "...": "..."}]
}
```

---

## Multi-Asset

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/correlation/detect` | Detect portfolio-level correlation regime | JWT/Key |
| POST | `/optimise` | Drawdown-controlled regime-aware portfolio optimisation | JWT/Key |
| POST | `/optimise/full` | One-shot: correlation regime + portfolio optimisation | JWT/Key |
| POST | `/optimise/masaniello` | Two-stage pipeline: optimise + per-asset Masaniello sizing | JWT/Key |

### POST /correlation/detect

Fits a 4-state Gaussian HMM on rolling correlation features across all assets. Returns the current correlation regime:

| Regime | Description | Mean \|ρ\| |
|--------|-------------|-----------|
| `low_corr` | Healthy diversification | < 0.30 |
| `mid_corr` | Moderate comovement | 0.30–0.55 |
| `high_corr` | Elevated stress | 0.55–0.75 |
| `crisis` | Diversification collapsed | > 0.75 |

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbols` | `list[str]` | *required* | Asset tickers (2–50) |
| `returns_matrix` | `list[list[float]]` | *required* | (n_bars × n_assets) matrix of log-returns |
| `window` | `int` | `60` | Rolling window (20–252) |
| `ewma_span` | `int` | `20` | EWMA span for correlation (5–60) |
| `n_hmm_states` | `int` | `4` | HMM states (2–6) |

**Response:**

```json
{
  "symbols": ["SPY", "QQQ", "TLT"],
  "n_assets": 3,
  "corr_regime": "low_corr",
  "corr_confidence": 0.82,
  "corr_risk_multiplier": 1.20,
  "blended_risk_multiplier": 1.35,
  "mean_abs_correlation": 0.28,
  "correlation_matrix": [[1.0, 0.85, -0.22], [0.85, 1.0, -0.18], [-0.22, -0.18, 1.0]],
  "corr_probs": {"low_corr": 0.72, "mid_corr": 0.18, "high_corr": 0.07, "crisis": 0.03},
  "n_bars_fitted": 252,
  "asset_regimes": [
    {"symbol": "SPY", "regime_label": "low_vol_bull", "vol_state": "low", "trend_state": "bull", "confidence": 0.78, "risk_multiplier": 1.50},
    {"symbol": "QQQ", "regime_label": "low_vol_bull", "vol_state": "low", "trend_state": "bull", "confidence": 0.82, "risk_multiplier": 1.50},
    {"symbol": "TLT", "regime_label": "med_high_vol_bear", "vol_state": "med_high", "trend_state": "bear", "confidence": 0.65, "risk_multiplier": 0.50}
  ]
}
```

### POST /optimise

Solves a regime-adjusted maximum Sharpe portfolio subject to a drawdown constraint. Three regime layers applied:
1. **Regime-adjusted μ** — per-asset expected returns scaled by risk multiplier
2. **Regime weight bounds** — tighter upper bounds in high-risk regimes
3. **Correlation exposure** — gross portfolio exposure reduced in crisis regimes

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbols` | `list[str]` | *required* | Asset tickers (2–50) |
| `returns_matrix` | `list[list[float]]` | *required* | (n_bars × n_assets) matrix of log-returns |
| `max_dd_limit` | `float` | `0.20` | Max acceptable expected drawdown (0.01–1.0) |
| `max_weight` | `float` | `0.40` | Maximum single-asset weight (0.05–1.0) |
| `risk_free_rate` | `float` | `0.04` | Risk-free rate for Sharpe calculation (0–0.20) |
| `corr_regime` | `string` | `null` | Override correlation regime |
| `kelly_fractions` | `list[float]` | `null` | Per-asset Kelly fractions |
| `use_asset_regimes` | `bool` | `true` | Auto-detect per-asset regimes |

**Response:**

```json
{
  "symbols": ["SPY", "QQQ", "TLT"],
  "weights": [0.35, 0.30, 0.35],
  "regime_adj_weights": [0.40, 0.35, 0.25],
  "expected_return": 0.142,
  "expected_vol": 0.098,
  "sharpe_ratio": 1.04,
  "expected_max_drawdown": 0.158,
  "dd_constraint_met": true,
  "regime_exposure": 0.72,
  "per_asset_bounds": [[0.0, 0.40], [0.0, 0.40], [0.0, 0.35]],
  "converged": true,
  "solver_message": "Optimization terminated successfully"
}
```

### POST /optimise/full

Runs the complete multi-asset pipeline in one request:
1. Fit per-asset HMMs → per-asset RegimeSnapshots
2. Detect correlation regime → corr_regime label + blended multiplier
3. Solve drawdown-controlled max-Sharpe with both regime layers applied

**Additional Request Fields** (beyond `/optimise`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `corr_window` | `int` | `60` | Correlation rolling window |
| `corr_ewma` | `int` | `20` | Correlation EWMA span |

**Response:**

```json
{
  "correlation": { "...CorrRegimeResponse..." },
  "optimisation": { "...OptimiseResponse..." }
}
```

### POST /optimise/masaniello

Full two-stage pipeline connecting portfolio optimisation to per-trade position sizing.

**Architecture:**

```
┌─────────────────────────────────────────────────────────────────┐
│                    POST /optimise/masaniello                     │
│                                                                  │
│  ┌──────────────────────────────────┐                           │
│  │  STAGE 1: DrawdownOptimiser      │                           │
│  │  ─────────────────────────────── │                           │
│  │  • Per-asset HMM regime detect   │                           │
│  │  • Correlation regime detect     │                           │
│  │  • Max-Sharpe with DD constraint │                           │
│  │  • Regime-adjusted weights       │                           │
│  │                                  │                           │
│  │  Output: weights[], regime_adj_  │                           │
│  │          weights[], capital_      │                           │
│  │          allocation per asset     │                           │
│  └──────────────┬───────────────────┘                           │
│                 │                                                │
│                 ▼                                                │
│  ┌──────────────────────────────────┐                           │
│  │  STAGE 2: DynamicMasanielloSizer │                           │
│  │  ─────────────────────────────── │                           │
│  │  f_i = β × (0.5 + M_i)          │                           │
│  │        × Q_i × DD_i × V_i       │                           │
│  │                                  │                           │
│  │  Per asset:                      │                           │
│  │  • M_i = wins_needed/trades_left │                           │
│  │  • Q_i = prob × regime × conf   │                           │
│  │  • DD_i = drawdown scaling       │                           │
│  │  • V_i = ATR-based vol adj       │                           │
│  │                                  │                           │
│  │  Output: risk_amount, contracts, │                           │
│  │          allowed, factor details │                           │
│  └──────────────────────────────────┘                           │
│                                                                  │
│  "The optimiser says WHAT to hold;                               │
│   Masaniello says HOW MUCH to hold."                             │
└─────────────────────────────────────────────────────────────────┘
```

**Request:** Same as `PortfolioMasanielloRequest` — see [`/size/masaniello/portfolio`](#post-sizemasanielloportfolio) for full parameter documentation.

**Example Request:**

```json
{
  "symbols": ["SPY", "QQQ", "TLT"],
  "returns_matrix": [[0.001, 0.002, -0.001], [0.0005, 0.0015, -0.0008], ...],
  "equity": 500000,
  "stop_distances": [2.50, 3.80, 1.20],
  "p_wins": [0.60, 0.58, 0.55],
  "reward_risks": [2.0, 2.2, 1.8],
  "atr_baselines": [5.20, 8.50, 1.80],
  "atr_currents": [4.80, 7.90, 2.10],
  "max_dd_limit": 0.15,
  "max_weight": 0.40,
  "base_risk": 0.005
}
```

**Example Response:**

```json
{
  "symbols": ["SPY", "QQQ", "TLT"],
  "weights": [0.35, 0.30, 0.35],
  "regime_adj_weights": [0.40, 0.35, 0.25],
  "expected_return": 0.142,
  "expected_vol": 0.098,
  "sharpe_ratio": 1.04,
  "expected_max_drawdown": 0.158,
  "regime_exposure": 0.72,
  "asset_sizes": [
    {
      "symbol": "SPY",
      "portfolio_weight": 0.40,
      "capital_allocation": 200000,
      "allowed": true,
      "risk_fraction": 0.0058,
      "risk_amount": 1160.0,
      "contracts": 464,
      "masaniello_factor": 0.60,
      "quality_factor": 0.90,
      "drawdown_factor": 1.00,
      "volatility_factor": 1.08,
      "expected_edge": 0.20,
      "reason": "Masaniello sizing: f=0.58%",
      "regime_label": "low_vol_bull"
    },
    {
      "symbol": "QQQ",
      "portfolio_weight": 0.35,
      "capital_allocation": 175000,
      "allowed": true,
      "risk_fraction": 0.0052,
      "risk_amount": 910.0,
      "contracts": 239,
      "masaniello_factor": 0.60,
      "quality_factor": 0.87,
      "drawdown_factor": 1.00,
      "volatility_factor": 1.06,
      "expected_edge": 0.16,
      "reason": "Masaniello sizing: f=0.52%",
      "regime_label": "low_vol_bull"
    },
    {
      "symbol": "TLT",
      "portfolio_weight": 0.25,
      "capital_allocation": 125000,
      "allowed": false,
      "risk_fraction": 0.0,
      "risk_amount": 0.0,
      "contracts": 0,
      "masaniello_factor": 0.60,
      "quality_factor": 0.33,
      "drawdown_factor": 1.00,
      "volatility_factor": 1.17,
      "expected_edge": -0.01,
      "reason": "Trade rejected: expected edge negative",
      "regime_label": "med_high_vol_bear"
    }
  ],
  "total_risk_amount": 2070.0,
  "total_contracts": 703,
  "corr_regime": "low_corr",
  "corr_confidence": 0.82
}
```

---

## Observation Builder

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/observation/build` | Build a 24-feature observation vector from price series | None |
| POST | `/observation/build-v2` | Build enhanced 24+ feature observation vector with optional TDA | None |

### POST /observation/build

Constructs the full 24-feature observation vector used by the RL/ML policy layer. Features 14–19 are **Markov features**: live HMM posterior probabilities — never defaulted to uniform priors.

**24 Feature Labels:**

| Index | Label | Description |
|-------|-------|-------------|
| 0 | `log_return_1bar` | 1-bar log return |
| 1 | `log_return_3bar` | 3-bar log return |
| 2 | `log_return_10bar` | 10-bar log return |
| 3 | `normalised_atr` | ATR normalised by price |
| 4 | `rolling_vol_20` | 20-bar rolling volatility |
| 5 | `ema_distance` | Price distance from EMA |
| 6 | `hhll_score` | Higher-high/lower-low score |
| 7 | `vol_regime_percentile` | Volatility regime percentile |
| 8 | `atr_vs_baseline` | ATR vs historical baseline |
| 9 | `vol_trend_slope` | Volatility trend slope |
| 10 | `regime_prob_0` | HMM state 0 posterior |
| 11 | `regime_prob_1` | HMM state 1 posterior |
| 12 | `regime_prob_2` | HMM state 2 posterior |
| 13 | `regime_prob_3` | HMM state 3 posterior |
| 14 | `vol_prob_low` | Low volatility posterior |
| 15 | `vol_prob_medium` | Medium volatility posterior |
| 16 | `vol_prob_high` | High volatility posterior |
| 17 | `trend_prob_down` | Down trend posterior |
| 18 | `trend_prob_neutral` | Neutral trend posterior |
| 19 | `trend_prob_up` | Up trend posterior |
| 20 | `regime_quality` | Regime quality (risk multiplier) |
| 21 | `state_confidence` | HMM confidence scalar |
| 22 | `masaniello_pressure` | Masaniello urgency factor |
| 23 | `drawdown_factor` | Drawdown scaling factor |

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `high` | `list[float]` | *required* | Ordered high-price series (min 81 bars) |
| `low` | `list[float]` | *required* | Ordered low-price series (min 81 bars) |
| `window` | `int` | `200` | Rolling window length (≥81) |
| `refit_every` | `int` | `50` | HMM refit interval (≥1) |
| `n_hmm_states` | `int` | `4` | HMM states (2–8) |
| `recommended_f` | `float` | `0.0` | Kelly fraction for Masaniello feature (idx 22) |

**Response:**

```json
{
  "symbol": "SPY",
  "observation_vector": [0.0023, 0.0045, 0.0120, 0.0118, 0.00142, -0.0035, 0.65, 0.72, 0.92, 0.0015, 0.05, 0.15, 0.60, 0.20, 0.65, 0.20, 0.15, 0.08, 0.12, 0.80, 1.50, 0.78, 0.60, 1.00],
  "feature_labels": ["log_return_1bar", "..."],
  "vol_probs": [0.65, 0.20, 0.10, 0.05],
  "trend_probs": [0.02, 0.08, 0.60, 0.30],
  "regime_label": "low_vol_bull",
  "confidence": 0.78,
  "bar_index": 199,
  "is_markov_uniform": false,
  "n_bars_fitted": 200
}
```

### POST /observation/build-v2

Enhanced observation builder that optionally appends 10 TDA features, extending the vector to 34 dimensions.

**Additional TDA Feature Labels (indices 24–33):**

| Index | Label | Description |
|-------|-------|-------------|
| 24 | `tda_n_components` | Connected components |
| 25 | `tda_n_loops` | Topological loops |
| 26 | `tda_max_persistence_0d` | Max 0-dim persistence |
| 27 | `tda_mean_persistence_0d` | Mean 0-dim persistence |
| 28 | `tda_max_persistence_1d` | Max 1-dim persistence |
| 29 | `tda_mean_persistence_1d` | Mean 1-dim persistence |
| 30 | `tda_entropy_0d` | 0-dim persistence entropy |
| 31 | `tda_entropy_1d` | 1-dim persistence entropy |
| 32 | `tda_anomaly_score` | Topology deviation score |
| 33 | `tda_regime_change_probability` | Regime transition probability |

**Additional Request Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `include_tda` | `bool` | `false` | Append 10 TDA features (extends to 34) |
| `embedding_dim` | `int` | `3` | Takens embedding dimension (2–10) |
| `embedding_delay` | `int` | `1` | Time delay for embedding (1–5) |

**Response:** Same as `/observation/build` plus:

```json
{
  "tda_included": true,
  "tda_anomaly_score": 0.42,
  "tda_regime_change_prob": 0.15
}
```

---

## TDA Features

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/tda/features` | Extract TDA features from price series using persistent homology | None |

### POST /tda/features

Topological Data Analysis feature extraction pipeline:
1. Compute log-returns from price series
2. Create Takens time-delay embedding (point cloud)
3. Compute distance matrix
4. Compute persistent homology (0-dim: connected components, 1-dim: loops)
5. Compute Betti numbers at each filtration step
6. Compute persistence entropy (topological complexity)
7. Compute anomaly score (deviation from baseline topology)
8. Estimate regime change probability

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Ordered price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `embedding_dim` | `int` | `3` | Takens embedding dimension (2–10) |
| `embedding_delay` | `int` | `1` | Time delay for embedding (1–5) |
| `max_filtration` | `float` | `2.0` | Maximum filtration value in std units (0–10) |
| `n_filtration_steps` | `int` | `20` | Filtration resolution (5–100) |
| `anomaly_threshold` | `float` | `1.5` | Std deviations for anomaly detection (0–5) |
| `baseline_prices` | `list[float]` | `null` | Baseline series for anomaly comparison |

**Response:**

```json
{
  "symbol": "SPY",
  "n_bars": 200,
  "n_components": 1,
  "n_loops": 2,
  "max_persistence_0d": 0.85,
  "mean_persistence_0d": 0.32,
  "max_persistence_1d": 0.42,
  "mean_persistence_1d": 0.18,
  "persistence_entropy_0d": 1.25,
  "persistence_entropy_1d": 0.78,
  "total_persistence_entropy": 2.03,
  "anomaly_score": 0.42,
  "regime_change_probability": 0.15,
  "feature_vector": [1.0, 2.0, 0.85, 0.32, 0.42, 0.18, 1.25, 0.78, 0.42, 0.15],
  "feature_labels": ["n_components", "n_loops", "..."],
  "betti_0": [1, 1, 2, 1, ...],
  "betti_1": [0, 0, 1, 1, ...]
}
```

---

## Portfolio

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/portfolio` | Aggregated regime + risk summary across all active sessions | JWT/Key |
| GET | `/portfolio/symbols` | List symbols with active sessions | JWT/Key |

### GET /portfolio

Returns a portfolio-level view aggregating all (or specified) active streaming sessions.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symbols` | `string` | `null` | Comma-separated symbols. Omit for all active sessions. |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target vol (0–1.0) |
| `base_risk_limit` | `float` | `0.02` | Max daily loss (0–0.5) |

**Risk Flags:**

| Flag | Meaning |
|------|---------|
| `high_risk_count` | Number of symbols with risk_multiplier < 0.5 |
| `concentration_flag` | Any symbol with recommended_f > 40% |
| `regime_divergence_flag` | Portfolio spans many different regime buckets |
| `active_alerts` | Symbols that recently changed regime |

**Response:**

```json
{
  "n_symbols": 3,
  "symbols": [
    {
      "symbol": "SPY",
      "regime_label": "low_vol_bull",
      "vol_state": "low",
      "trend_state": "bull",
      "confidence": 0.78,
      "risk_multiplier": 1.50,
      "recommended_f": 0.1125,
      "var_95": 0.0128,
      "cvar_95": 0.0189,
      "sharpe_ratio": 1.42,
      "last_price": 452.30,
      "n_bars": 250,
      "ready": true
    }
  ],
  "regime_counts": {"low_vol_bull": 2, "med_vol_neutral": 1},
  "dominant_regime": "low_vol_bull",
  "regime_consensus": 0.67,
  "avg_risk_multiplier": 1.25,
  "avg_recommended_f": 0.095,
  "avg_sharpe": 1.28,
  "portfolio_var95": 0.022,
  "portfolio_cvar95": 0.031,
  "high_risk_count": 0,
  "concentration_flag": false,
  "regime_divergence_flag": false,
  "active_alerts": ["SPY: regime changed to high_vol_bear"]
}
```

### GET /portfolio/symbols

Returns all symbols currently registered in the session registry.

**Response:**

```json
{
  "symbols": ["SPY", "QQQ", "TLT"]
}
```

---

## Simulation

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/simulate/{symbol}` | Simulate regime transition paths forward in time | JWT/Key |

### POST /simulate/{symbol}

Fits a 4-state HMM on the supplied price series, then simulates `n_paths` forward paths of `horizon` bars using the fitted Markov transition matrix.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Historical price series (min 81 bars) |
| `symbol` | `string` | `"UNKNOWN"` | Ticker symbol |
| `horizon` | `int` | `20` | Forward steps to simulate (1–252) |
| `n_paths` | `int` | `500` | Monte Carlo paths (50–5000) |
| `seed` | `int` | `42` | Random seed for reproducibility |
| `current_price` | `float` | `null` | Override starting price (>0) |

**Response:**

```json
{
  "symbol": "SPY",
  "horizon": 20,
  "n_paths": 500,
  "current_regime": "low_vol_bull",
  "current_price": 452.30,
  "price_p5": [449.8, 448.2, 446.5, ...],
  "price_p25": [451.0, 450.5, 449.8, ...],
  "price_median": [452.5, 452.8, 453.2, ...],
  "price_p75": [454.0, 455.2, 456.5, ...],
  "price_p95": [455.8, 457.5, 459.8, ...],
  "expected_risk_mult": [1.50, 1.48, 1.45, ...],
  "return_mean": 0.018,
  "return_std": 0.042,
  "return_var95": -0.052,
  "return_cvar95": -0.071,
  "terminal_regime_mode": "low_vol_bull",
  "pct_paths_positive": 0.68,
  "max_drawdown_mean": 0.035,
  "step_dominant_regime": ["low_vol_bull", "low_vol_bull", "med_low_vol_bull", ...]
}
```

---

## Streaming

The streaming subsystem provides real-time regime detection on live price data. Sessions must be seeded with historical prices before streaming ticks.

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/stream/seed` | Seed a symbol session with historical prices | None |
| POST | `/stream/tick` | Ingest one price tick and receive regime snapshot | None |
| POST | `/stream/ticks` | Ingest a batch of price ticks | None |
| GET | `/stream/sessions` | List all active streaming sessions | None |
| GET | `/stream/session/{symbol}` | Get session status | None |
| DELETE | `/stream/session/{symbol}` | Remove a streaming session | None |
| GET | `/sse/{symbol}` | Server-Sent Events stream for a symbol | None |
| GET | `/sse/alerts` | Global SSE stream for all regime-change alerts | None |

### POST /stream/seed

Creates (or resets) a streaming session for a symbol and pre-loads historical prices so the HMM can be fitted immediately. **Must be called before streaming ticks.**

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | `string` | *required* | Ticker symbol |
| `prices` | `list[float]` | *required* | Historical prices (min 81 bars) |
| `window` | `int` | `500` | Window length (81–5000) |
| `kelly_fraction` | `float` | `0.5` | Fractional Kelly (0.1–1.0) |
| `target_vol` | `float` | `0.15` | Annualised target vol (0–1.0) |
| `base_risk_limit` | `float` | `0.02` | Max daily loss (0–0.5) |
| `refit_every` | `int` | `50` | Refit interval (5–500) |

**Response:**

```json
{
  "symbol": "SPY",
  "ready": true,
  "n_bars": 500,
  "refit_count": 1,
  "tick_count": 0,
  "last_regime": "low_vol_bull"
}
```

### POST /stream/tick

Push a single price tick. Returns a full `TickResponse` once the session has enough bars; returns `{"status": "buffering", "n_bars": N}` while warming up.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | `string` | *required* | Ticker symbol |
| `price` | `float` | *required* | Latest price (>0) |
| `ts` | `float` | `null` | Unix timestamp (auto-filled if omitted) |

**Response:**

```json
{
  "symbol": "SPY",
  "ts": 1710000000.0,
  "price": 452.30,
  "n_bars": 501,
  "regime_label": "low_vol_bull",
  "vol_state": "low",
  "trend_state": "bull",
  "vol_probs": {"low": 0.65, "med_low": 0.20, "med_high": 0.10, "high": 0.05},
  "trend_probs": {"strong_bear": 0.02, "bear": 0.08, "bull": 0.60, "strong_bull": 0.30},
  "confidence": 0.78,
  "risk_multiplier": 1.50,
  "recommended_f": 0.1125,
  "sharpe_ratio": 1.42,
  "var_95": 0.0128,
  "cvar_95": 0.0189,
  "suggested_stop": 439.25,
  "suggested_tp": 468.50,
  "regime_changed": false,
  "alert": null,
  "refit_count": 1
}
```

### POST /stream/ticks

Replay or batch-ingest multiple ticks. Returns a snapshot for each tick (or a buffering stub).

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `symbol` | `string` | Ticker symbol |
| `prices` | `list[float]` | Price series |
| `timestamps` | `list[float]` | Optional timestamps |

### GET /stream/sessions

Lists all active streaming sessions with statistics.

### GET /stream/session/{symbol}

Returns session status for a specific symbol.

**Response:**

```json
{
  "symbol": "SPY",
  "ready": true,
  "n_bars": 523,
  "refit_count": 2,
  "tick_count": 23,
  "last_regime": "low_vol_bull"
}
```

### DELETE /stream/session/{symbol}

Removes a streaming session.

**Response:**

```json
{
  "status": "removed",
  "symbol": "SPY"
}
```

### GET /sse/{symbol}

Server-Sent Events stream that pushes a JSON regime snapshot on every tick received for `symbol`. Subscribe from any browser with `EventSource`.

```javascript
const es = new EventSource('/sse/SPY');
es.onmessage = e => console.log(JSON.parse(e.data));
```

### GET /sse/alerts

Broadcasts regime-change alerts for **all symbols** to connected listeners. Only fires on regime state transitions (not every tick). Severity levels: `info` | `warning` | `critical`.

---

## GPU

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/gpu/capabilities` | Report GPU availability and active HMM backend | JWT |
| POST | `/gpu/benchmark` | Benchmark HMM fit + predict on synthetic data | JWT |

### GET /gpu/capabilities

Returns which HMM backend will be used and what hardware is available.

**Response:**

```json
{
  "hmm_backend": "GpuRegimeHMM (pomegranate)",
  "active_device": "cuda",
  "pomegranate_installed": true,
  "cuda_available": true,
  "mps_available": false
}
```

### POST /gpu/benchmark

Runs `n_iterations` fit+predict cycles on synthetic price data. Returns mean latency for fit and predict separately.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `n_bars` | `int` | `500` | Synthetic data length (81–50000) |
| `n_iterations` | `int` | `3` | Number of fit+predict cycles (1–10) |
| `force_cpu` | `bool` | `false` | Force hmmlearn baseline for comparison |

**Response:**

```json
{
  "backend": "GpuRegimeHMM (pomegranate)",
  "device": "cuda",
  "n_bars": 500,
  "n_iterations": 3,
  "mean_fit_ms": 12.45,
  "mean_predict_ms": 1.23,
  "regime_label": "low_vol_bull",
  "pomegranate_available": true,
  "cuda_available": true,
  "mps_available": false
}
```

---

## Feed Adapters

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| POST | `/feeds/start` | Start live feed adapters | Trader/Admin |
| POST | `/feeds/stop` | Stop all running feed adapters | Trader/Admin |
| GET | `/feeds/status` | Get feed adapter status | Trader/Admin |

### POST /feeds/start

Start one or more live feed adapters. Symbols must be seeded via `POST /stream/seed` before starting feeds — bars for unseeded symbols are silently dropped. Adapters reconnect automatically on disconnect (exponential backoff).

**Request** (array of configs):

```json
[
  {
    "source": "alpaca",
    "symbols": ["SPY", "QQQ"],
    "bar_size": "1Min",
    "feed": "sip"
  },
  {
    "source": "binance",
    "symbols": ["BTCUSDT"],
    "bar_size": "1m"
  }
]
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | `string` | *required* | `"alpaca"`, `"binance"`, or `"ib"` |
| `symbols` | `list[str]` | *required* | Symbols to stream |
| `bar_size` | `string` | `"1Min"` | Bar interval |
| `feed` | `string` | `"sip"` | Alpaca feed type: `"sip"` or `"iex"` |

### POST /feeds/stop

Gracefully stops all feed adapter tasks.

### GET /feeds/status

Returns running state and per-symbol bar counts for all active feed adapters.

**Response:**

```json
{
  "running": true,
  "adapters": [
    {"source": "alpaca", "symbols": ["SPY", "QQQ"], "status": "connected"}
  ],
  "total_bars": {"SPY": 1250, "QQQ": 980}
}
```

---

## Renko HFT Pipeline

The Renko HFT pipeline processes price ticks through a 6-layer architecture for high-frequency trading signals.

### 6-Layer Pipeline Architecture

```
Tick ──→ [Layer 1: Brick Engine] ──→ [Layer 2: Swing Classifier]
                                              │
                     [Layer 6: Executor] ←── [Layer 5: Risk Manager]
                           │                        │
                           ▼                        │
                     Trade Journal      [Layer 4: Signal Filter]
                                                  │
                                        [Layer 3: Pattern Detector]
```

| Layer | Module | Purpose |
|-------|--------|---------|
| 1 | `brick_engine.py` | Renko brick construction with 2-brick reversal rule |
| 2 | `swing_classifier.py` | Labels bricks HH/HL/LH/LL, detects swing points |
| 3 | `pattern_detector.py` | Detects bull_run, bear_run, reversal, double_top, double_bottom, consolidation_break |
| 4 | `signal_filter.py` | 7-layer risk governance: session window, max trades, daily loss, consecutive losses, cooldown, velocity, HMM regime gate |
| 5 | `risk_manager.py` | Brick-denominated SL/TP/trailing stop/time-stop/partial-exit |
| 6 | `executor.py` | Alpaca order executor with bracket orders (dry-run simulation mode when no API keys) |

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/renko/tick` | Process single tick through 6-layer pipeline |
| POST | `/renko/tick/batch` | Batch tick processing (backtest / warm-up) |
| GET | `/renko/state` | Current pipeline state |
| GET | `/renko/stats` | Comprehensive pipeline statistics |
| GET | `/renko/bricks` | Recent Renko bricks |
| GET | `/renko/classified` | Bricks with swing labels (HH/HL/LH/LL) |
| GET | `/renko/signals` | Pattern signals |
| GET | `/renko/trades` | Trade journal |
| GET | `/renko/swing-points` | Detected swing points |
| POST | `/renko/regime` | Update HMM regime for gate filter |
| POST | `/renko/equity` | Update account equity |
| POST | `/renko/config` | Update pipeline configuration (resets pipeline) |
| POST | `/renko/reset` | Reset pipeline for a symbol |
| GET | `/renko/backtest/stats` | Backtest/trading statistics |

All Renko endpoints accept an optional `symbol` query parameter (default `"SPY"`). The backend maintains one `RenkoPipeline` instance per symbol in memory.

### POST /renko/tick

Process a single tick through the 6-layer pipeline.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `price` | `float` | *required* | Latest trade price |
| `timestamp` | `float` | `null` | Unix timestamp |
| `symbol` | `string` | `null` | Override symbol |

### POST /renko/tick/batch

Process a batch of ticks for backtest or warm-up.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prices` | `list[float]` | *required* | Price series |
| `timestamps` | `list[float]` | `null` | Timestamps |
| `regimes` | `list[str]` | `null` | Pre-computed regime labels |
| `symbol` | `string` | `null` | Override symbol |

### GET /renko/bricks

Recent Renko bricks.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symbol` | `string` | `"SPY"` | Trading symbol |
| `last_n` | `int` | `100` | Number of bricks to return |

### GET /renko/classified

Bricks with swing labels (HH/HL/LH/LL).

**Query Parameters:** Same as `/renko/bricks`.

### GET /renko/signals

Pattern signals (bull_run, bear_run, reversal, double_top, double_bottom, consolidation_break).

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `symbol` | `string` | `"SPY"` | Trading symbol |
| `last_n` | `int` | `50` | Number of signals to return |

### GET /renko/trades

Trade journal entries.

### GET /renko/swing-points

Detected swing points.

### POST /renko/regime

Update the current HMM regime for the gate filter (Layer 4).

**Request:**

| Field | Type | Description |
|-------|------|-------------|
| `regime` | `string` | Current regime name (e.g. `"low_vol_bull"`) |

### POST /renko/equity

Update the current account equity (used by risk manager).

**Request:**

| Field | Type | Description |
|-------|------|-------------|
| `equity` | `float` | Current account equity |

### POST /renko/config

Update pipeline configuration. **Resets the pipeline** for the symbol.

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `brick_size` | `float` | `0.50` | Fixed brick size in dollars |
| `brick_size_mode` | `string` | `"fixed"` | `"fixed"`, `"atr"`, or `"dynamic"` |
| `reversal_bricks` | `int` | `2` | Bricks needed for direction reversal |
| `bull_trigger_n` | `int` | `3` | Consecutive HH/HL for bull trigger |
| `bear_trigger_n` | `int` | `3` | Consecutive LL/LH for bear trigger |
| `sl_bricks` | `int` | `3` | Stop-loss distance in brick units |
| `tp_bricks` | `int` | `5` | Take-profit distance in brick units |
| `trailing_stop` | `bool` | `true` | Enable trailing stop |
| `trail_after_bricks` | `int` | `3` | Activate trail after N bricks profit |
| `trail_distance_bricks` | `int` | `2` | Trail distance in bricks |
| `max_trades_per_session` | `int` | `15` | Maximum trades per session |
| `max_daily_loss_bricks` | `float` | `10.0` | Maximum daily loss in bricks |
| `max_consecutive_losses` | `int` | `3` | Pause after N consecutive losses |
| `cooldown_seconds` | `float` | `30.0` | Minimum seconds between trades |
| `regime_gate` | `bool` | `true` | Only trade with HMM regime alignment |
| `symbol` | `string` | `"SPY"` | Trading symbol |

### POST /renko/reset

Reset the pipeline for a symbol (clears all bricks, signals, trades).

### GET /renko/backtest/stats

Returns trading statistics from the journal: win rate, profit factor, Sharpe, max drawdown, Kelly estimates, etc.

---

## Health

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| GET | `/health` | Health check | None |

### GET /health

**Response:**

```json
{
  "status": "ok",
  "version": "5.1.0",
  "sessions": 3,
  "redis": true,
  "gpu_backend": "RegimeHMM (hmmlearn)",
  "device": "cpu"
}
```

---

## Recommended Workflows

### Quick Analysis (Single Asset)

```
POST /analyse/full  →  regime + sizing + risk in one call
```

Best for: One-shot analysis of a single symbol with minimal round trips.

### Signal-Driven Trading

```
POST /strategy/signal  →  signal (long/short/flat) + sizing + risk guidance
```

Best for: Getting an actionable trading decision with full context.

### Multi-Asset Portfolio Construction

```
POST /correlation/detect  →  portfolio-level correlation regime
POST /optimise/full       →  correlation + optimisation one-shot
POST /optimise/masaniello →  portfolio optimise + per-asset Masaniello sizing
```

Best for: Building a regime-aware portfolio with precise per-asset position sizing.

### Live Streaming Pipeline

```
POST /stream/seed           →  seed symbol with historical prices
POST /stream/tick           →  push live ticks, receive regime snapshots
GET  /sse/{symbol}          →  subscribe to real-time SSE stream
GET  /sse/alerts            →  monitor regime changes across all symbols
GET  /portfolio             →  aggregated portfolio view
```

Best for: Real-time monitoring with regime change alerts.

### Renko HFT Trading

```
POST /renko/tick            →  process ticks through 6-layer pipeline
GET  /renko/signals         →  check for pattern signals
GET  /renko/trades          →  review trade journal
POST /renko/regime          →  update HMM regime for gate filter
GET  /renko/stats           →  comprehensive pipeline statistics
```

Best for: High-frequency Renko-based trading with pattern detection and risk management.

### Walk-Forward Backtesting

```
POST /backtest/run  →  30+ metrics with full equity/drawdown curves
```

Best for: Validating strategy performance with realistic regime-aware execution.

### Dynamic Masaniello Sizing

```
POST /size/masaniello           →  single-trade sizing (Path B)
POST /size/masaniello/batch     →  batch sizing with state flow
POST /optimise/masaniello       →  full two-stage: optimise + Masaniello
```

Best for: Progress-dependent position sizing where batch outcomes influence future trade sizes.

### Observation Building for RL/ML

```
POST /observation/build-v2  →  24 or 34 feature vector (with TDA)
```

Best for: Feeding structured feature vectors into reinforcement learning or ML policy networks.

### GPU-Accelerated Regime Detection

```
GET  /gpu/capabilities  →  check GPU availability
POST /gpu/benchmark      →  measure speedup
```

Best for: Enabling 10-50× HMM speedup with pomegranate on CUDA/MPS hardware.
