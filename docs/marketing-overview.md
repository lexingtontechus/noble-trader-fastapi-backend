# Noble Trader — Dynamic Regime Risk Management Platform

> **Where quantitative finance meets production-grade AI.**  
> A FastAPI-powered trading intelligence platform that detects market regimes, computes real-time risk metrics, and drives RL/ML-powered trading policies.

---

## 🔥 The Problem

Most trading systems are **blind** — they execute the same strategy regardless of what the market is doing. A position size that works in a calm bull market can be catastrophic during a volatility spike. Regime shifts go unnoticed until it's too late.

Traditional risk management is **reactive**, not predictive.

---

## 💡 The Solution

**Noble Trader** is the first open-source platform that combines:

- 🧠 **Hidden Markov Model (HMM) regime detection** — dual 4-state models for volatility and trend
- 📐 **Position sizing** — blended Kelly Criterion + Dynamic Masaniello formula
- 🔮 **TimesFM foundation model forecasting** — self-hosted Google AI time-series predictions
- 🛡️ **Production-grade risk controls** — VaR/CVaR, kill switches, circuit breakers
- 🤖 **AI agent readiness** — Model Context Protocol (MCP) server for autonomous agent integration

---

## 🚀 Key Capabilities

### 1. Regime Detection Engine

| Dimension | States | Application |
|-----------|--------|-------------|
| **Volatility** | Low / Med-Low / Med-High / High | Position sizing, stop-loss width |
| **Trend** | Strong Bear / Bear / Bull / Strong Bull | Directional bias, asset selection |

The two HMMs produce a **16-cell regime risk multiplier table** ranging from **0.10×** (ideal conditions) to **1.75×** (extreme caution). States are auto-labeled via sorted component means — zero manual mapping required.

**Hardware flexibility:** CPU via `hmmlearn` or GPU via `pomegranate` (10–50× speedup) — same interface, zero code changes.

### 2. Position Sizing — Two Complementary Paths

| Path | Approach | Best For |
|------|----------|----------|
| **Path A: Kelly Criterion** | Portfolio-fraction, returns-based, stateless | Portfolio-level allocation |
| **Path B: Dynamic Masaniello** | Per-trade, stateful, 5-factor model | High-frequency / individual entries |

**The Masaniello formula:**
```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
```

Five factors controlling every trade: base risk appetite, batch urgency (time decay), model quality (regime confidence), drawdown protection (trailing loss gate), and volatility adjustment — all clamped by a Kelly overlay.

### 3. EV Engine v4 — Blended Probability of Winning with Log-Odds Pooling

```
P_win = inv_logit(0.40 * logit(P_regime) + 0.25 * logit(P_external) + 0.35 * logit(P_timesfm))
```

| Source | Weight | Purpose |
|--------|--------|---------|
| HMM Regime Posteriors | 40% | Macro regime context |
| External Signal (e.g. L2 Imbalance) | 25% | Micro-structure edge via live order book |
| TimesFM Foundation Model | 35% | AI-powered forecast validation |

**v4 upgrades:**
- **Log-odds pooling** instead of linear averaging — prevents overconfidence when signals agree
- **Markov chain** moved from P_win blend → position-sizing multiplier (was double-counting directionality)
- **Live L2 order book pipeline** — real-time multi-level imbalance from Alpaca (equities) and Hyperliquid (crypto)
- **Self-calibrating:** The platform tracks its own prediction accuracy, computes Brier scores, reliability diagrams, and self-labels every prediction as CALIBRATED / OVERCONFIDENT / UNDERCONFIDENT

### 4. TimesFM Self-Hosted Forecasting

Google's **TimesFM 2.5-200m** foundation model runs on a dedicated service (port 8002), isolated from the main API. This architecture:

- ✅ Keeps the 925 MB model from bloating the main process
- ✅ Allows independent scaling of inference vs. API workloads  
- ✅ Eliminates recurring cloud API costs
- ✅ Keeps sensitive market data entirely on your infrastructure

### 5. Renko HFT Pipeline — 6-Layer Brick Trading

```
Tick → Brick Construction → Swing Classification → Pattern Detection 
→ Signal Filter → Risk Manager → Alpaca Executor
```

22+ endpoints for tick processing, backtesting, parameter optimization, and live trading toggle. Pattern detection includes double-tops, head-and-shoulders, and more — all on Renko bricks instead of traditional candlesticks.

### 6. Risk Management Dashboard

| Metric | Description |
|--------|-------------|
| VaR (95% / 99%) | Historical value-at-risk |
| CVaR (95% / 99%) | Expected shortfall |
| Max Drawdown | Peak-to-trough decline |
| Dynamic Stop-Loss / Take-Profit | Regime-aware exit levels |
| Risk Budget Tracking | Real-time vs. regime max loss limits |

### 7. Operational Hardening

| Feature | Purpose |
|---------|---------|
| **3-Tier Kill Switch** | Global / User / Symbol — Redis-backed, instant |
| **Circuit Breakers** | Error rate, latency, concurrent request thresholds |
| **Trading Mode Manager** | Paper / Live with 2-step confirmation |
| **Order Reconciliation** | Compare intended vs. executed orders |
| **Rate Throttling** | 200 req/min with per-endpoint granularity |
| **Audit Logging** | Full traceability with CSV export |
| **Discord & Telegram** | Rich-embed notifications for signals, executions, alerts |

### 8. Backtesting Engine

- Walk-forward HMM backtest with **30+ performance metrics**
- Parameter grid sweep — up to **50 combinations** per run
- Persistence to Supabase with history, comparison, and CSV/JSON export
- Historical VaR backtesting (Kupiec POF, Christoffersen independence tests)

---

## 🏗️ Architecture at a Glance

```
┌─────────────┐     ┌─────────────────────────────────────┐
│  Client /   │     │         Gateway Proxy :8000         │
│  AI Agent   │───▶ │  (reverse proxy, rate limit, auth)  │
│  (MCP)      │     └──┬──────────────────────┬───────────┘
└─────────────┘        │                      │
                 ┌─────▼──────┐         ┌──────▼──────┐
                 │  Main API  │         │  TimesFM    │
                 │  :8001     │         │  :8002      │
                 │            │         │             │
                 │ HMM Engine │         │ Google      │
                 │ Renko Pipe │         │ TimesFM 2.5 │
                 │ EV Engine  │         │ Foundation  │
                 │ Auth/MCP   │         │ Model       │
                 │ Risk Mgr   │         └─────────────┘
                 └────┬───────┘
                      │
	          ┌───────────┼──────────────────┐
          │           │                  │
     ┌────▼───┐ ┌────▼────┐  ┌──────▼─────────┐
     │ Redis  │ │Supabase │  │   Alpaca        │
     │(State) │ │(Persist)│  │   Hyperliquid   │
     └────────┘ └─────────┘  │   (Exchanges)   │
                             └─────────────────┘
```

### Three-Service Gateway Pattern

| Service | Port | Responsibility |
|---------|------|---------------|
| **proxy.py** | 8000 | Public ingress, routing, header injection |
| **main.py** | 8001 | All trading logic: HMM, Renko, EV, risk, auth, MCP |
| **server.py** | 8002 | Self-hosted TimesFM inference (isolated) |

---

## 🎯 Who Is This For?

| Audience | What Noble Trader Gives You |
|----------|---------------------------|
| **Quantitative Trading Firms** | Production-ready regime detection + risk management deployed in minutes |
| **Hedge Funds & Prop Desks** | Battle-tested sizing engine with academic-grade statistical rigor |
| **AI/ML Engineers** | MCP server for agentic trading — AI agents that understand market regimes |
| **Individual Algorithmic Traders** | Renko HFT pipeline, backtesting, and paper trading — all in one stack |
| **Researchers** | Open architecture, TDA modules, calibration loop for experimentation |

---

## 🧪 Statistical Rigor

- **Deflated Sharpe Ratio** — multiple testing correction for strategy evaluation
- **Bootstrap Confidence Intervals** — non-parametric performance bounds
- **Significance Testing** — statistical tests for strategy outperformance
- **Survivorship Bias Elimination** — data quality checks for clean signals
- **Corporate Actions Handling** — dividend, split, and M&A adjustments
- **Data Lineage Tracking** — every data point's provenance is known

---

## 🔌 Integrations

| Integration | Purpose |
|-------------|---------|
| **Alpaca** | Live order execution, WebSocket P&L streaming |
| **Supabase** | PostgreSQL backtest persistence, calibration, audit, API keys |
| **Redis (Upstash)** | Price buffers, kill switch state, session cache |
| **TimesFM (Google)** | Self-hosted foundation model time-series forecasting |
| **Clerk** | OAuth/JWT authentication with JWKS auto-fetching |
| **Yahoo Finance** | Cold-start warmup for new symbols |
| **Binance / IBKR** | Optional crypto / traditional broker feed adapters |
| **Discord / Telegram** | Real-time alerts and execution notifications |
| **FastAPI-MCP** | Expose 30+ endpoints as AI agent tools |

---

## 📦 Quick Start

```bash
# Clone
git clone https://github.com/lexingtontechus/noble-trader-fastapi-backend.git
cd noble-trader-fastapi-backend

# Install
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Launch (all 3 services)
./start.sh
```

**Or deploy in 5 minutes on Lightning.ai (Full GPU) or Render** — `render.yaml` included, one-click setup.

---

## 📊 By the Numbers

| Metric | Value |
|--------|-------|
| HMM states per model | 4 (configurable 2–4) |
| Risk multiplier grid | 16 cells (0.10× – 1.75×) |
| Observation vector | 24 features |
| P_win sources | 3 blended (log-odds) |
| Renko pipeline layers | 6 |
| Backtest metrics | 30+ |
| API endpoints | 80+ |
| OpenAPI spec | 200+ paths |
| MCP exposed tools | 30+ |
| Python version | 3.11+ |
| Version | **7.5.0** |

---

## 🔗 Links

- **GitHub:** [github.com/lexingtontechus/noble-trader-fastapi-backend](https://github.com/lexingtontechus/noble-trader-fastapi-backend)
- **Frontend:** [noble-trader-agent-frontend.vercel.app](https://noble-trader-agent-frontend.vercel.app)
- **API Docs:** `/docs` (Swagger UI) and `/openapi.yaml`
- **WebSocket Test Client:** `/ws-client.html`
- **Changelog:** [docs/CHANGELOG.md](docs/CHANGELOG.md)

---

## 📄 License

**Development Status:** 4 — Beta  
**Intended Audience:** Financial and Insurance Industry

---

*Built by **Lexington Tech LLC** — bridging the gap between AI agents and live trading infrastructure.*

*Noble Trader is not financial advice. Trading involves substantial risk of loss. Use at your own risk.*
