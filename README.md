# Dynamic Regime Risk Management Platform

A production-grade FastAPI web service for quantitative trading risk management.
Detects market regimes with 4-state Hidden Markov Models, gates position sizes
via a unified Dynamic Masaniello + Kelly Criterion engine, computes real-time risk
metrics (VaR/CVaR), streams live regime snapshots and alerts over WebSocket and SSE,
and produces 24-feature observation vectors for downstream RL/ML policy layers. The
`p_markov` source in the EV Engine v4 4-source log-odds blend is now produced by a
**multivariate Gaussian HMM** (5 states, 9 features: log_return + 3 microstructure +
5 TA indicators) trained live via Baum-Welch — replacing the legacy discrete 3-state
price-only Markov chain — with all 5 TA values persisted per sweep tick in Supabase.

---

## Version

**v8.1.0** — **Kairos** research stack (RANK board, event-time transitions, walk-forward `rank_score`) · **TimesFM quantile & covariate capture** (p10/p50/q90 band + XReg dynamic covariates) · Renko live-grid hygiene · Settlement / calibration measurement honesty

### Highlights

> **Geometry / `vol_ratio` — read before touching TP-SL (easy to miss).**
> **`vol_ratio` is a VOLATILITY ratio, not volume:** `std(Δlog close 24h) /
> std(Δlog close 168h)` at entry (`tiered_atr.compute_vol_ratio`).
> **BTCUSD** has no balanced pooled +EV cell — live geometry is
> **8.07 / 1.77 only when `vol_ratio ≤ 0.71`** (low-vol band). Outside the
> band the dynamic pick is withheld (heavy bricks kept).
> **XAUUSD** is **4.94 / 1.00** on 45d pooled (no gate); the old 0.91/0.71 is gone.
> Crypto **"trend runner"** (far TP ~20/2.5, wr≈0.25) is a **separate product** —
> do not fold it into the balanced engine bar (w∈[0.40,0.60]). Consider the same
> split for **ETHUSD / other crypto** before live brackets.
> Full write-up: `worklog/kairos/20260930_VOL_REGIME_ATR_BTC_XAU.md`.
> **Heavy sweep is not this research:** it re-walks 10d on a fixed grid; the
> 45d free lattice + vol terciles + OOS live in `scripts/vol_atr_solver.py`.

#### Kairos (beyond `p_win`)
- **`kairos_rank_score` R-Layer** — opportunity ranking *within* a regime cell (freshness, setup type, recent TP, shrunk cell hit-rate). Walk-forward AUC 0.67–0.78; top/bottom tercile TP-rate Δ +0.29. **Ranks capital priority; `p_win` stays an eligibility/level score** (level IC≈0, overconfident).
- **Event-time transitions** — post-regime-flip freshness (`dir_flip` ~6h information clock; fresh−deep y_acc +0.12). STATE badges: FRESH / WARM / STALE / DEEP.
- **Shadow gate** — trailing-P60 rank quantile (ΔD +0.23…+0.30, research only; not live-enforced).
- **Product surface** — nobletradingapp signal ranking board + Discord RANK/STATE/TRANSITION fields.

#### TimesFM — quantiles & covariates
- **Quantile capture (P1.3)** — univariate `/forecast` already returns `quantile_forecast`; orchestrator now stamps **q10/q50/q90**, `q_spread=(q90−q10)/last`, `q_pos` onto `nt_signal_sim` (migration 186). Research stamps only.
- **Dynamic covariates (P1.1–P1.2)** — TimesFM 2.5 `forecast_with_covariates` (XReg) accepts trend_z / momentum / vol / cluster returns. `timesfm_gap` (p_timesfm − p_win) is the only TimesFM-family feature with stable within-cell IC (+0.037 both halves).
- **Decision-aligned mapping** — prefer quantile/interval hit under recorded TP–SL brackets over raw sigmoid(`k·Δp`) for a future `p_map` promote; level Brier still led by shrunk `cell_p`.

> **v8.0.0 changelog (2026-09-24…26):** Twin-bar calibration + reachability methodology, settlement contract, dynamic ATR selector (shadow), evidence-pool / hourly resolve. See `docs/Setup/CHANGELOG.md`. Anchors: `pyproject.toml`, `main.py`, both `openapi.yaml`, `tests/test_version_consistency.py`.

> **v7.9.0 changelog (2026-09-16):** Batches A–F of `worklog/20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md` (quant audit follow-up; see `docs/QUANT_AUDIT_SIGNAL_CALIBRATION_REACHABILITY_PNL.md`). (1) **Settlement contract** — `settlement_contract.py` owns close-walk/OHLC resolution, same-bar policy (`tp_first` default), expired MTM, optional costs; `eod_resolver` delegates when `settlement_contract_mode=live`. (2) **Measurement honesty** — `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` + `NOBLE_TRADER_CALIBRATION_RAW_REQUIRED` default true; Brier uses raw only (`error_source=raw_missing` otherwise); raw-bias excludes raw-absent episodes (no enforced stand-in). (3) **Dual-report scorecard** (migration 153) — `pnl_closed_only` / `pnl_all_resolved` / `win_pct_closed` / `expiry_pct`; reachability `n_stored`/`n_proxy`; proxy ATR quarantined from expectancy. (4) **`p_win_bracket`** (migration 154, shadow) — `p_resolve × Beta(2,2) P(win|resolve)` by `(symbol, vol_tier, distance/ATR)`; `bracket_gate_shadow` logged. (5) **Heavy-sweep gate order** — calibration enforce **before** qualification (parity with light). (6) **Underconfidence** no longer cuts Kelly. (7) **Markov N** = `tp_bricks` (fallback 10). (8) **TimesFM** mapping from `feature_flags.yaml` (`timesfm.sigmoid_k=10`, clamp `[0.05,0.95]`); ghost `p_historical` 0.30 weight removed (live blend renormalized 5:4:5).

> **v7.8.0 changelog (2026-08-13):** EOD recalibration is no longer a once-daily 00:30 UTC job. (1) **Intraday correction passes** run every 4h (04:30/08:30/12:30/16:30/20:30 UTC, `EOD_CORRECTION_INTRADAY_ENABLED`) recomputing per-symbol bias + `nt_eod_actions`; emergency heavy sweeps fire only for newly-flagged symbols subject to a 12h cooldown. (2) **Rolling bias window** — `fetch_calibration_bias` computes bias over the last 7 days (`EOD_BIAS_WINDOW_DAYS`) with a 20-record minimum instead of all-time, so a regime shift moves the bias in hours. (3) **Drift monitor** on the 5-min light sweep (`drift_monitor.py`, `EOD_DRIFT_MONITOR_ENABLED`) writes an immediate `bayesian_penalty` when a 48h rolling bias exceeds `EOD_DRIFT_THRESHOLD` (0.15) with ≥20 records and no fresh action — a fast path between scheduled passes. (4) Action freshness window 26 → 8h (`EOD_ACTION_FRESH_HOURS`). (5) **Backlog drain** — `scripts/drain_signal_backlog.py` (resilient retry guard) cleared `nt_signal_sim` pending to 0; `EOD_RESOLVE_MAX_TOTAL` raised 4,000 → 12,000 so the nightly run outpaces capture. (6) **Retention fix** — the resolved-delete protect list is chunked (keyset pagination + `in.(≤100)` deletes), ending the nightly HTTP 400. (7) **Purge** — contaminated `nt_calibration_records` (`entered_at < 2026-08-11T14:00Z`, stale-price artifact) deleted; table rebuilt from clean records. (8) **`predicted_p_win_raw`/`calibration_brier`** — pre-enforcement p_win captured when `NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW` is enabled (migration: `supabase/migrations/114_add_predicted_p_win_raw.sql`) so calibration can separate model miscalibration from enforcement compensation; Brier score written per record. Full day detail: `worklog/20260813_CHANGELOG.md`.

> **v7.7.0 changelog:** The discrete 3-state `MarkovChain` is replaced as the live `p_markov` source by a 5-state Gaussian HMM (`regime_platform/services/hmm/`). The HMM emits the same 3×3 transition matrix contract (`{UP, DOWN, FLAT}`, rows sum to 1.0) and the same `current_state` string that the EV engine v4 expects — drop-in replacement. The HMM is trained live on a 9-feature observation vector per symbol (log_return + 3 microstructure + 5 TA), bootstrapped from `/api/price/batch` on first sweep and enriched every tick via concurrent `asyncio.gather` calls to TVDA `/api/quote` (microstructure) + `/api/ta` (TA). The 5 TA values are persisted per sweep tick in both `nt_sweep_result` and `nt_regime_log` (migration 036) for post-hoc calibration analytics. No new env vars, no gates, no shadow mode, no cold-start handling. See `TALARIA-MULTIVARIATE-HMM-PROPOSAL.md` (v4) for the full design.

> **v7.6.0 changelog:** EVEngine now exposes a validated public weight API (`set_weights()` / `temp_weights()` / `restore_default_weights()`); the soft-TimesFM-gate block in the sweep orchestrator no longer reaches into private `_full_weights`. `BayesianUpdater` is formally deprecated (use the agent's `BayesianAlpha` for live persistence). `renko/trade_journal.py:TradeJournal.record()` is gated behind `assert_execution_enabled()` — the backend is a sweep orchestrator only; live trade journaling lives in the Hermes agent (Talaria).

---

## Project Structure

```
noble-trader-fastapi-backend/
│
├── main.py                           Main FastAPI app — HMM, Renko, EV, auth
├── server.py                         TimesFM forecasting service (port 8002)
├── proxy.py                          Gateway reverse proxy (port 8000 → 8001/8002)
├── start.sh                          Launcher — starts main + timesfm + proxy
├── setup.py                          Editable install shim (delegates to pyproject.toml)
├── pyproject.toml                    Modern packaging metadata + tool config
├── requirements.txt
│
├── regime_platform/                  Python package
│   ├── core/
│   │   ├── regime_engine.py          4-state dual GaussianHMM + _sanitize_model
│   │   ├── obs_builder.py            24-feature InferenceObservationBuilder
│   │   ├── position_sizer.py         Unified Kelly (Path A) + Masaniello (Path B)
│   │   ├── risk_manager.py           VaR, CVaR, drawdown, stop/TP
│   │   ├── simulator.py              Markov-chain Monte Carlo regime simulation
│   │   ├── gpu_engine.py             Pomegranate GPU HMM + create_regime_hmm()
│   │   ├── correlation_regime.py     DCC multi-asset correlation regime detector
│   │   ├── portfolio_optimiser.py    Drawdown-controlled max-Sharpe optimiser
│   │   └── backtester.py          Walk-forward HMM backtest engine (503 lines)
│   ├── adapters/
│   │   └── feed_adapters.py          Alpaca / Binance / IB live OHLCV adapters
│   ├── auth/
│   │   └── api_key.py                Single shared X-API-Key gatekeeping
│   ├── models/
│   │   ├── schemas.py                Pydantic v2 batch I/O models
│   │   └── stream_schemas.py         Pydantic v2 streaming models
│   ├── routers/
│   │   ├── regime.py                 POST /regime/detect
│   │   ├── sizing.py                 POST /size/kelly
│   │   ├── risk.py                   POST /risk/analyse
│   │   ├── pipeline.py               POST /analyse/full
│   │   ├── stream_rest.py            POST /stream/seed | /tick | /ticks
│   │   ├── stream_ws.py            WS /ws/{symbol} | GET /sse/{symbol|alerts}
│   │   ├── simulate.py               POST /simulate/{symbol}
│   │   ├── portfolio.py              GET /portfolio
│   │   ├── gpu.py                    GET /gpu/capabilities | POST /gpu/benchmark
│   │   ├── feeds.py                  POST /feeds/start | /stop | GET /feeds/status
│   │   ├── multi_asset.py            POST /correlation/detect | /optimise | /optimise/full
│   │   ├── backtest.py            POST /backtest/run | /history | /{id} | /compare | /optimize | /export
│   │   ├── strategy.py            POST /strategy/signal
│   │   ├── tda.py                    POST /tda/features
│   │   ├── observation.py            POST /observation/build | /build-v2
│   │   ├── calibration.py            POST /calibration/log | GET /bias/{symbol} | GET /status
│   │   └── venue.py                  GET /venue/list | POST /{id}/snapshot | GET /{id}/snapshots | GET /{id}/status
│   ├── services/
│   │   ├── stream_session.py         Per-symbol stateful streaming engine
│   │   ├── registry.py               Singleton session store
│   │   ├── portfolio_service.py      Multi-symbol regime + risk aggregation
│   │   └── backtest_store.py      Supabase persistence for backtest results
│   ├── services/statistics/
│   │   ├── ev_engine.py              EV Engine v4 — 4-source P_win log-odds blend + public set_weights()/temp_weights()/restore_default_weights() API + calibration_bias shrink
│   │   └── bayesian_updater.py       [DEPRECATED P5] BayesianUpdater — use agent's BayesianAlpha for live persistence; backend's copy retained for backtest-replay only
│   ├── renko/                        6-layer Renko HFT pipeline (22 endpoints)
│   ├── notifications/               Discord notifier
│   ├── middleware/
│   │   ├── rate_limit.py            SlowAPI rate limiter (compute/write/default)
│   │   └── kill_switch_gate.py       Kill switch middleware (blocks order execution)
│   ├── services/
│   │   ├── alpaca_stream.py          Alpaca WebSocket stream manager
│   │   └── pnl_service.py            P&L analytics (intraday, risk dashboard, alerts)
│
├── docs/
│   ├── index.html                    Full documentation site (26 pages)
│   ├── ws-client.html                Interactive WebSocket test client
│   ├── openapi.yaml                  OpenAPI 3.1 specification
│   ├── CHANGELOG.md                  Complete version history (v1.0 -> v3.1)
│   ├── deployment.md                 Production operations guide
│   └── TimeFM/                       TimesFM integration requirements & architecture docs (6 documents)
│
├── regime_platform/
│   └── sweep_orchestrator/           Sweep Orchestrator — active signal generation (Python, APScheduler, Supabase)
│       ├── orchestrator.py           Core signal gen with TimesFM, EV gates, qualification
│       ├── __main__.py               Entry point for orchestrator service
│       ├── market_hours.py           Market hours validation
│       ├── price_fetcher.py           Price data fetching
│       └── supabase_client.py        Supabase integration (active symbols, logs)
│
├── supabase/
│   ├── functions/                    Edge functions [DEPRECATED] — retained for reference
│   └── migrations/                   Database migrations (001–022)
│
├── tests/
│   ├── test_platform.py              Smoke test: HMM + Kelly + risk
│   ├── test_platform_v4.py           4-state HMM: 16-cell multiplier table
│   ├── test_position_sizer.py        95-assertion Masaniello+Kelly suite
│   ├── test_obs_builder.py           24-feature observation builder (7 tests)
│   ├── test_streaming.py             StreamSession: asyncio ticks + alerts
│   └── test_backtest.py             25-test backtest suite (unit + integration)
```

---

## Quick Start

```bash
# 1. Install (editable install recommended)
pip install -e ".[dev]"     # includes torch + timesfm
# or: pip install -r requirements.txt

# 2. Set environment
export TIMESFM_API_KEYS="your-key-here"
export API_KEY="your-shared-secret"

# 3. Start (three-layer stack)
./start.sh
# Launches:
#   main.py   on port 8001  (HMM/Renko/auth)
#   server.py on port 8002  (TimesFM forecasting)
#   proxy.py  on port 8000  (gateway — this is the public port)

# 4. Verify
curl http://localhost:8000/health                    # aggregated health
curl http://localhost:8000/forecast                  # → TimesFM (port 8002)
curl http://localhost:8000/regime/detect             # → main app (port 8001)
```

**Direct (single-service) mode:**
```bash
# Main app only (no proxy, no TimesFM):
uvicorn main:app --port 8000 --reload

# TimesFM only:
python server.py
```

### Architecture

```
start.sh
  ├── PORT=8001  python main.py      (HMM, Renko, auth, all routers)
  ├── PORT=8002  python server.py    (TimesFM forecasting API)
  └── PORT=8000  python proxy.py     (routes by path prefix)

Client → proxy:8000
  /forecast*  → server.py:8002
  /info*      → server.py:8002
  /health     → aggregated from both
  /*          → main.py:8001
```

### Signal pipeline (system topology)

This service is the **signal producer**. It qualifies signals (EV/Kelly/regime
gates) and delivers them directly to Discord — no intermediate broker.
Redis, the proxy, and the per-plan StreamSplitter fan-out were all retired
2026-08-25; Discord delivery (`discord_delivery.py`) is now the sole
delivery path, gated by `NOBLE_TRADER_DISCORD_SIGNALS_ENABLED`.

```
noble-trader-fastapi-backend (LightningAI sweep orchestrator)
   │  qualification gates (EV / Kelly / regime confidence / TimesFM)
   ▼
Discord webhook delivery (direct, per-asset-class or per-plan channel routing)
```

- **Version:** `v7.9.0` (uniform across `pyproject.toml`, `main.py`, `openapi.yaml`).
- **CORS:** live middleware (`main.py`) reads **`CORS_ALLOWED_ORIGINS`** (default
  `http://localhost:3000`). The legacy `CORS_ORIGINS` var was removed — do not use it.
- **License validation:** the proxy validates subscriber `X-License-Key` via the
  Supabase Edge Function `license-validate`. **Note:** that Edge Function is
  owned by the `nobletradingapp` repo and is **not yet present** in this backend
  repo's `supabase/functions/` — cross-repo wiring is pending.
- **Execution:** this backend is **sweep-only (P5 contract)** — it never executes
  trades. Execution lives in `noble-trader-agent` via the MT4/MT5 bridge.

### Project Setup

```bash
# Editable install (dev)
pip install -e .

# With GPU HMM extras
pip install ".[gpu]"

# TimesFM model cache (first run downloads ~925MB)
# On first /forecast call, the model is cached at:
#   .cache/huggingface/hub/models--google--timesfm-2.5-200m-pytorch/
```

---

## Library Stack

| Library | Version | Role |
|---------|---------|------|
| **FastAPI** | ≥0.111 | Async web framework, auto-docs |
| **hmmlearn** | ≥0.3.2 | 4-state Gaussian HMM (CPU) |
| **scipy** | ≥1.13 | VaR/CVaR, SLSQP optimiser |
| **numpy** | ≥2.0 | Vectorised return/vol calculations |
| **pydantic v2** | ≥2.7 | Request/response validation |
| **uvicorn** | ≥0.29 | ASGI server |
| **pomegranate** | ≥1.0 *(optional)* | GPU-accelerated HMM (PyTorch) |
| **httpx** | ≥0.28.1 | Async HTTP client (Supabase/proxy routing) |
| **timesfm** | ≥2.0.1 | Google TimesFM 2.5 foundation model |
| **torch** | ≥2.4 | PyTorch (TimesFM inference) |
| **huggingface-hub** | ≥1.0 | HF model download/caching |
| **safetensors** | ≥0.6 | Safe tensor serialization |
| **alpaca-py** | *(optional)* | Alpaca live feed adapter |
| **websockets** | *(optional)* | Binance live feed adapter |
| **ib_async** | *(optional)* | Interactive Brokers adapter |

---

## API Endpoints

### Batch (stateless, historical)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/analyse/full` | ⭐ Regime + sizing + risk in one request |
| `POST` | `/regime/detect` | HMM regime classification |
| `POST` | `/size/kelly` | Regime-gated Kelly position sizing |
| `POST` | `/risk/analyse` | VaR, CVaR, drawdown, stop/TP |
| `POST` | `/simulate/{symbol}` | Monte Carlo regime transition simulation |

### TimesFM Forecasting

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/forecast` | TimesFM foundation model batch forecast | X-API-Key |
| `GET` | `/info` | Model config + device + GPU memory | X-API-Key |
| `GET` | `/health` | Liveness probe (no auth) | None |

The `/forecast` endpoint accepts batched inputs:

```json
{
  "horizon": 12,
  "inputs": [[100, 101, 102], [200, 201, 202]]
}
```

Response returns `point_forecast`, `quantile_forecast`, `num_series`, and `elapsed_ms`. See `server.py` for full schema.

**Quantiles (v8.1.0):** `quantile_forecast` is a continuous-quantile-head band (`[series][horizon][q]`, typically 9–10 levels covering p10…p90). The sweep orchestrator extracts **q10/q50/q90** at horizon step 0 and persists `timesfm_q_lo/mid/hi`, `timesfm_q_spread`, `timesfm_q_pos` on `nt_signal_sim` (migration 186) for research IC / Brier — never as live gates.

**Covariates (research):** TimesFM 2.5 `forecast_with_covariates` (XReg residual path) accepts dynamic channels (trend z-score, momentum, volatility, cluster-mate returns). Probe: `scripts/timesfm_p1_covariates_probe.py`.

### Multi-Asset

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/correlation/detect` | DCC portfolio correlation regime |
| `POST` | `/optimise` | Drawdown-controlled max-Sharpe |
| `POST` | `/optimise/full` | Correlation + optimise one-shot |
| `GET` | `/portfolio` | Multi-symbol aggregated risk view |

### Backtesting

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/backtest/run` | Walk-forward backtest with 30+ metrics, Supabase persistence |
| `GET` | `/backtest/history` | Paginated list of saved backtest results |
| `GET` | `/backtest/{id}` | Full backtest result by ID |
| `POST` | `/backtest/compare` | Side-by-side comparison of saved results |
| `POST` | `/backtest/optimize` | Parameter grid sweep (max 50 combinations) |
| `POST` | `/backtest/export` | Export result as CSV or JSON download |
| `DELETE` | `/backtest/{id}` | Delete a saved backtest result |

### Live Streaming

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/stream/seed` | Seed a symbol session |
| `POST` | `/stream/tick` | Push one price tick |
| `POST` | `/stream/ticks` | Batch tick replay |
| `GET` | `/stream/sessions` | List active sessions |
| `WS` | `/ws/{symbol}` | Full-duplex WebSocket |
| `GET` | `/sse/{symbol}` | SSE per-symbol stream |
| `GET` | `/sse/alerts` | SSE global regime-change alerts |

### Infrastructure

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/gpu/capabilities` | GPU hardware + HMM backend |
| `POST` | `/gpu/benchmark` | Fit/predict latency benchmark |
| `POST` | `/feeds/start` | Start Alpaca/Binance/IB live feed |
| `GET` | `/feeds/status` | Adapter health + bar counts |
| `GET` | `/health` | Service health check |

### Calibration Loop (v3)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/calibration/log` | Log a calibration record (predicted P_win vs actual outcome) | Write |
| `GET` | `/calibration/bias/{symbol}` | Get calibration bias per symbol | Any |
| `GET` | `/calibration/status` | Get daily CALIBRATED / OVERCONFIDENT / UNDERCONFIDENT status | Any |

**EOD recalibration (v7.9.0):** the self-correction loop runs a full
resolve+correct+retention pass nightly at 00:30 UTC, plus **correction-only passes
every 4h** (04:30/08:30/12:30/16:30/20:30 UTC) and a **drift monitor** on each 5-min
light sweep. Bias is computed over a **rolling 7-day window** (min 20 records).
Penalty actions (`bayesian_penalty` / `kelly_reduction` / `emergency_heavy_sweep`)
are enforced live for 8h (freshness window). See `worklog/20260813_CHANGELOG.md`
and `worklog/20260817_RECALIBRATION_PERF_ASSESSMENT.md`.

**Calibration tuning:** `min_rr` (Gate 6b / EV allow) is **shared** between light
qualification (`EVEngine.min_rr`) and the heavy-sweep grid pre-filter — both read
`get_min_rr()` from `config/feature_flags.yaml` (**not** env; Studio `MIN_RR` is
stale/ignored after 2026-09-04). Live value **1.5**. **Status 2026-09-20: maintained
but a non-factor on light qualification** — typical brick geometries already have
RR ≈ 1.67–2.0+, so `rr ≥ 1.5` almost never binds; reject logs are dominated by
kelly / regime confidence / p_win. RR is not a strong/weak signal tier; do not raise
the light floor to 2.0 as a quality lever (geometry choke). Code path unchanged.
`TIMESFM_SIGMOID_K`, `TIMESFM_CLAMP_MIN/MAX` live in `feature_flags.yaml` /
related keys — recommended production: k=10.0, clamp=[0.05, 0.95].

### Venue Management (v3)

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/venue/list` | List all trading venues with status | Any |
| `POST` | `/venue/{venue_id}/snapshot` | Record an account snapshot | Write |
| `GET` | `/venue/{venue_id}/snapshots` | Get snapshot history (paginated) | Any |
| `GET` | `/venue/{venue_id}/status` | Venue health check | Any |

### P&L Analytics

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/pnl/intraday` | Intraday P&L with realized/unrealized breakdown | Any |
| `GET` | `/pnl/history` | Historical P&L time series | Any |
| `GET` | `/pnl/export` | Export P&L data as CSV | Any |
| `GET` | `/pnl/alerts` | List P&L alert thresholds | Any |
| `POST` | `/pnl/alerts` | Create P&L alert threshold | Any |
| `DELETE` | `/pnl/alerts/{threshold_id}` | Delete P&L alert threshold | Any |
| `POST` | `/pnl/alerts/check` | Evaluate P&L alert thresholds | Any |

### Risk Dashboard

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/risk/dashboard` | Full risk dashboard with 12 metrics | Any |

### Operational Hardening

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/operational/kill-switch/status` | Kill switch status | Any |
| `POST` | `/operational/kill-switch/activate` | Activate kill switch (halts all trading) | Admin |
| `POST` | `/operational/kill-switch/deactivate` | Deactivate kill switch | Admin |
| `POST` | `/operational/kill-switch/cancel-all` | Cancel all open orders | Admin |
| `POST` | `/operational/kill-switch/close-all` | Close all positions | Admin |
| `GET` | `/operational/audit-log` | Paginated audit log entries | Any |
| `GET` | `/operational/audit-log/summary` | Audit log summary stats | Any |
| `GET` | `/operational/audit-log/export` | Export audit log as CSV | Any |
| `GET` | `/operational/mode` | Current trading mode (paper/live) | Any |
| `POST` | `/operational/mode/request` | Request trading mode change | Write |
| `POST` | `/operational/mode/confirm` | Confirm trading mode change (2-step) | Write |
| `GET` | `/operational/reconcile/status` | Reconciliation status | Any |
| `POST` | `/operational/reconcile/run` | Run reconciliation | Write |
| `POST` | `/operational/reconcile/order` | Reconcile a single order | Any |
| `GET` | `/operational/reconcile/positions` | Reconcile all positions | Any |
| `GET` | `/operational/mode/health` | Trading mode health check | Any |
| `GET` | `/operational/executor/status` | Alpaca executor status | Any |
| `GET` | `/operational/throttle/status` | Rate throttle status (200 req/min) | Any |

### SSE P&L Streaming

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/sse/pnl` | SSE stream for real-time P&L updates | Any |
| `GET` | `/sse/pnl/status` | SSE P&L connection status | Any |

---

## Regime Detection - 4-State HMM

Two independent 4-state Gaussian HMMs classify every price bar.
States are auto-labelled by sorting component means - no manual mapping.

| Dimension | States | Features |
|-----------|--------|---------|
| Volatility | `low` · `med_low` · `med_high` · `high` | 20-bar RV, 5-bar RV, \|return\| |
| Trend | `strong_bear` · `bear` · `bull` · `strong_bull` | return, 10-bar cum, 30-bar cum |

### Risk Multiplier Table - 4×4 = 16 regimes · range [0.10, 1.75]

| Vol \ Trend   | Strong Bear | Bear  | Bull  | Strong Bull |
|--------------|-------------|-------|-------|-------------|
| **Low**      | 0.70×       | 1.05× | 1.54× | **1.75×**   |
| **Med-Low**  | 0.48×       | 0.72× | 1.06× | 1.20×       |
| **Med-High** | 0.28×       | 0.42× | 0.62× | 0.70×       |
| **High**     | **0.14×**   | 0.21× | 0.31× | 0.35×       |

---

## Position Sizing - Dynamic Masaniello + Kelly

`core/position_sizer.py` provides two independent sizing paths:

### Path A - Portfolio-fraction Kelly (stateless)
Input: historical returns + `RegimeSnapshot`. Output: `recommended_f` ∈ [0, 1].

```python
from regime_platform.core.position_sizer import PositionSizer, PositionRequest

result = PositionSizer().size(PositionRequest(
    returns=log_returns, kelly_fraction=0.5, target_vol=0.15, regime=snap
))
print(result.recommended_f)
```

### Path B - Dynamic Masaniello (stateful, per-trade)

Formula: **`f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i`**

| Factor | Formula | Clamp | Purpose |
|--------|---------|-------|---------|
| β | `base_risk` (default 0.50%) | - | Risk anchor |
| M_i | `(W − w) / (N − i + 1)` | [0.0, 1.5] | Batch urgency |
| Q_i | `prob_factor × regime_factor × conf_factor` | product | Quality gate |
| DD_i | `clip(1 − dd/max_dd, 0.25, 1.0)` | [0.25, 1.0] | Drawdown protection |
| V_i | `clip(ATR_base/ATR_current, 0.5, 1.5)` | [0.5, 1.5] | Volatility adjustment |

```python
from regime_platform.core.position_sizer import DynamicMasanielloSizer, SizingConfig

sizer = DynamicMasanielloSizer(SizingConfig(base_risk=0.005, batch_size=5, target_wins=3))
sizer.batch.peak_equity = 100_000

result = sizer.size_from_snapshot(
    snap=snap, equity=100_000,
    stop_distance_price=2.50, point_value=50.0,
    p_win=0.62, reward_risk=2.5,
    atr_baseline=12.0, atr_current=10.0,
)
print(result.summary())
# [✓ ALLOWED]  f=0.550%  $550  units=5.50  contracts=5  ...

sizer.batch.record(result, outcome=True, pnl=625.0)
sizer.batch.reset(equity=100_625)
```

---

## 24-Feature Observation Vector

`core/obs_builder.py` - `InferenceObservationBuilder`

**Critical rule:** always use the stateful class. Never use a standalone function.
If Markov features 14–19 show uniform values (~0.33), the uniform-prior bug is active.

| Indices | Features | Source |
|---------|---------|--------|
| 0–2 | log returns (1, 3, 10 bar) | Price series |
| 3–5 | normalised ATR, rolling vol, EMA distance | OHLCV |
| 6–9 | HHLL score, vol percentile, ATR ratio, vol slope | Derived |
| 10–13 | Raw HMM state posteriors (unsorted) | HMM |
| **14–19** | **Markov features: vol_prob_{low,med,high}, trend_prob_{down,neutral,up}** | **HMM** |
| 20–21 | Regime quality, state confidence | HMM |
| 22 | Masaniello pressure f×(1−f) | PositionSizer |
| 23 | Drawdown factor from peak | BatchState |

---

## Streaming Workflow

```bash
# 1. Seed
POST /stream/seed  {"symbol": "SPY", "prices": [...200+ bars]}

# 2a. REST ticks
POST /stream/tick  {"symbol": "SPY", "price": 512.34}

# 2b. WebSocket
ws://localhost:8000/ws/SPY
-> {"type":"seed","symbol":"SPY","prices":[...]}
-> {"type":"tick","symbol":"SPY","price":512.34}

# 2c. Subscribe (read-only)
GET /sse/SPY          # per-symbol tick stream
GET /sse/alerts       # global regime-change alerts

# 3. Portfolio view
GET /portfolio
```

---

## Testing

Run all suites in order - each layer builds on the previous:

```bash
# Unit (no server required)
python test_platform.py           # 3 checks  - smoke: HMM, Kelly, risk
python test_platform_v4.py        # 5 checks  - 4-state HMM, 16-cell table
python test_position_sizer.py     # 95 checks - Masaniello factors, gates, Kelly
python test_obs_builder.py        # 7 checks  - 24-feature builder, Markov guard

# Integration (no server required)
python test_streaming.py          # asyncio: 160 ticks, alerts, subscriber queue
export API_KEY=test-secret
python test_v30.py                # GPU, feed adapters, correlation, optimiser

# Live API (server must be running)
uvicorn main:app --port 8000 --reload
chmod +x curl_test.sh && ./curl_test.sh     # 6 curl assertions
python stream_spy_ticks.py                  # seed + 50 live ticks with formatted output
open sse_alert_monitor.html                 # browser SSE monitor for /sse/alerts
```

---

## Correlation Regime (Multi-Asset)

```bash
POST /optimise/full
{
  "symbols": ["SPY", "QQQ", "GLD", "TLT"],
  "returns_matrix": [[r_spy, r_qqq, r_gld, r_tlt], ...],
  "max_dd_limit": 0.15,
  "max_weight": 0.40
}
# Returns: correlation regime + drawdown-controlled optimal weights
```

| Correlation Regime | Mean \|ρ\| | Multiplier | Exposure |
|-------------------|-----------|-----------|---------|
| `low_corr` | < 0.20 | 1.00× | 100% |
| `mid_corr` | 0.20–0.50 | 0.85× | 90% |
| `high_corr` | 0.50–0.75 | 0.60× | 70% |
| `crisis` | > 0.75 | 0.35× | 50% |

---

## Authentication

**2026-08-25 — simplified to a single shared API key.** There are no
live users and the only deployment target is a single Render fallback
instance, so JWT, Clerk, and per-user/role auth were retired in favor of
one gatekeeping secret: `regime_platform/auth/api_key.py`'s
`require_api_key` dependency, checked against the `API_KEY` setting.

```bash
# Configure
export API_KEY="your-shared-secret"

# Usage — every protected endpoint takes the same header
curl -H "X-API-Key: your-shared-secret" http://localhost:8000/portfolio

# WebSocket
ws://localhost:8000/ws/SPY?api_key=your-shared-secret
```

No roles, no per-key plans, no login/token endpoints (`/auth/*` no
longer exists) — a request either has the correct key or it doesn't.
If `API_KEY` is unset, the gate fails closed: nothing authenticates,
matching the prior system's "no key configured" posture rather than
opening up.

```python
# Every protected endpoint uses the same dependency
from regime_platform.auth import require_api_key

@router.post("/simulate/{symbol}")
async def simulate_symbol(
    symbol: str,
    req: SimulateRequest,
    _: None = Depends(require_api_key),
):
    ...
```

---

## Signal Delivery (2026-08-25+)

Redis — and the poll-model (`trading:config:{symbol}` hash) and push-model
(`signal.raw.noble_trader` pub/sub + Streams) architectures that preceded
it — has been fully retired. `StreamSession` price buffers are in-memory
only (no restart-survival layer); qualified signals are delivered
directly to Discord (`regime_platform/sweep_orchestrator/discord_delivery.py`)
with no intermediate broker, gated by `NOBLE_TRADER_DISCORD_SIGNALS_ENABLED`.
Operational state (trading mode, kill-switch halts, reconciliation
last-run) persists to Supabase (`nt_system_config`) instead of Redis.

### Signal Generation Architecture

**Active path**: `regime_platform/sweep_orchestrator/`
- Runs as standalone Python service (not Supabase edge functions)
- Schedules per-symbol sweeps via APScheduler cron jobs (`*/5 * * * *`)
- Fetches TimesFM predictions (cached 15min) → 3-source EV blend
- Enforces 7 qualification gates + cooldown before publishing

**Qualification gates** (thresholds via `config/feature_flags.yaml` + related
settings; some names still appear as env leftovers that are **ignored**):

| Gate | Check | Config key | Live default | Binding today? |
|------|-------|------------|--------------|----------------|
| 1 | Signal is buy/sell (not neutral) | — | — | yes (neutral drops) |
| 2 | HMM regime confidence ≥ threshold | `regime_conf` / `MIN_REGIME_CONF` | 0.60 | **yes — primary** |
| 3 | Kelly fraction ≥ threshold | `min_kelly` / `MIN_KELLY` | 0.02 | **yes — primary** |
| 4 | TimesFM available | `timesfm_gate_mode=soft` → not a veto | soft | informational in soft |
| 5 | TimesFM confidence | `timesfm_gate_mode` | soft shrink | soft-band kelly, not hard veto |
| 6 | EV allowed (`ev > 0` **and** `rr ≥ min_rr`) | `min_ev` / `min_rr` | 0.0 / **1.5** | **RR clause inert** (see below); EV>0 still applies |
| 7 | Blended p_win | hard floor 0.50; soft band → `MIN_P_WIN` 0.55 | 0.50 / 0.55 | **yes — primary** |
| + | Cooldown | `signal_cooldown_minutes` | 60 | yes (post-gate suppress) |

**`min_rr` note (2026-09-20):** maintained at `feature_flags.yaml` → `min_rr: 1.5`
via `get_min_rr()`. Still present in EVEngine Step 6 and the heavy grid filter, but
**non-factor for light signal quality/qualification volume** at this level — RR comes
from brick geometry (typical ≥1.67), not per-tick strength. Binding light rejects:
kelly, regime_conf, p_win. Do not tier strong/weak by RR in 1.5–2.0; do not treat
2.0 as a quality gate on light publishes.

**Signal delivery**: only qualified signals reach Discord — sent directly
from both the light-sweep (`_process_symbol`) and heavy-sweep
(`_push_heavy_signal`) paths via `discord_delivery.py`. There is no
intermediate broker, aggregate channel, or per-plan fan-out.

### Core Fields (sweep_orchestrator)

| Field | Type | Example | Source | Description |
|---|---|---|---|---|
| `regime` | string | `low_vol_bull` | Light regime | Composite regime label: `{vol}_{trend}` |
| `regime_conf` | float | `0.85` | Light regime | HMM posterior probability of dominant state |
| `signal` | string | `buy` | Trade signal | Computed direction: `buy`, `sell`, `neutral` |
| `signal_conf` | float | `0.72` | Trade signal | Signal confidence (0–1) |
| `entry_price` | float | `64441.00` | Trade signal | Computed entry price for bracket order |
| `stop_loss` | float | `63941.00` | Trade signal | Stop-loss price |
| `take_profit` | float | `65441.00` | Trade signal | Take-profit price |
| `ev` | float | `0.35` | EV Engine | Expected value |
| `ev_per_dollar` | float | `0.12` | EV Engine | EV normalized per dollar risked |
| `p_win` | float | `0.62` | EV Engine | Blended P_win (4-source) |
| `p_regime` | float | `0.55` | EV Engine | HMM regime component |
| `p_imbalance` | float | `0.48` | EV Engine | L2 imbalance component |
| `p_markov` | float | `0.50` | EV Engine | Markov transition component |
| `aggression` | string | `mid` | Aggression dispatcher | `passive`, `mid`, or `aggressive` |
| `ev_scale` | float | `0.80` | EV Engine | EV-scaled Kelly multiplier |
| `effective_kelly` | float | `0.12` | Kelly sizing | Final Kelly fraction |
| `brick_size` | float | `0.50` | Orchestrator (LightningAI) | Optimal Renko brick size |
| `tp_bricks` | int | `5` | Orchestrator (LightningAI) | Take-profit in bricks |
| `sl_bricks` | int | `3` | Orchestrator (LightningAI) | Stop-loss in bricks |
| `kelly_f` | float | `0.15` | Kelly sizing | Base Kelly fraction |
| `markov_p_up` | float | `0.52` | Multivariate HMM | Probability of UP transition (5-state Gaussian HMM aggregated to 3 states) |
| `markov_p_dn` | float | `0.48` | Multivariate HMM | Probability of DOWN transition |
| `markov_current_state` | string | `FLAT` | Multivariate HMM | Current Markov state (`UP`/`DOWN`/`FLAT`) |
| `p_markov_single_step` | float | `0.55` | Multivariate HMM | T[current][target] for one step (trending-regime detector for agent's synthesizer) |
| `markov_transition_matrix` | dict | `{"UP": {...}, ...}` | Multivariate HMM | Full 3×3 transition matrix (rows sum to 1.0) — agent can re-derive other step counts |
| `last_update` | timestamp | `1782015223299` | — | Unix ms of last update |
| `sweep_timestamp` | timestamp | `1782015223299` | Orchestrator (LightningAI) | Unix ms of last sweep |

### Regime Shift Fields

| Field | Type | Example | Source | Description |
|---|---|---|---|---|
| `regime_shift` | string | `true` | Light regime | `"true"` if regime changed this cycle |
| `prev_regime` | string | `low_vol_bear` | Light regime | Previous regime before shift |
| `shift_at` | timestamp | `1782015223299` | Light regime | Unix ms when shift was detected |
| `shifts_24h` | int | `3` | Light regime | Number of regime shifts in last 24h |

### TimesFM Fields (current)

| Field | Type | Example | Source | Description |
|---|---|---|---|---|
| `p_timesfm` | float | `0.48` | TimesFM API | Foundation model directional forecast (0–1). >0.5 = bullish, <0.5 = bearish |
| `timesfm_horizon` | string | `12h` | TimesFM API | Forecast window label |

**Example:** `p_timesfm=0.48` for BTC means TimesFM forecasts a slight price decline over the next 12 hours.

**Live P_win blend (EV Engine v6, Batch F):** logit pool of
`p_regime` + `p_markov_hold_n` + `p_timesfm` (weights renormalized over
available sources). `p_historical` is **not** in the live weight table.
`P_imbalance` was removed from EV years ago (microstructure deprecation).

```
p_win = inv_logit( Σ w_i · logit(p_i) )   # available sources only
# nominal: p_regime 0.25 / p_markov_hold_n 0.20 / p_timesfm 0.25
```

Mapping: `p_timesfm = clamp(σ(k · Δp/p), clamp_min, clamp_max)` with
`k=10`, clamp `[0.05, 0.95]` from `config/feature_flags.yaml` (`timesfm` block).

See `docs/SIGNAL_PIPELINE.md` for the full path.

### Tail Risk Fields (v3)

| Field | Type | Example | Source | Description |
|---|---|---|---|---|
| `tail_risk_score` | float | `0.60` | Light regime | Tail risk level (0 = none, 0.35 = mild, 0.60 = moderate, 0.85 = critical) |
| `tail_risk_action` | string | `reduce_50` | Light regime | Recommended action: `none`, `reduce_25` (×0.75), `reduce_50` (×0.5), or `skip` (×0.0) |

**Logic:**
| p_timesfm | Regime | Confidence | Score | Action | Multiplier |
|---|---|---|---|---|---|
| <0.2 or >0.8 | high_vol | ≥0.7 | 0.85 | `skip` | 0.0 |
| <0.2 or >0.8 | any | ≥0.7 | 0.60 | `reduce_50` | 0.5 |
| <0.2 or >0.8 | high_vol | <0.7 | 0.35 | `reduce_25` | 0.75 |

### Calibration Fields (v3)

| Field | Type | Example | Source | Description |
|---|---|---|---|---|
| `calibration_bias` | float | `-0.02` | Orchestrator (LightningAI) | avg(predictedPWin) − actual_win_rate (negative = underconfident) |
| `calibration_status` | string | `CALIBRATED` | Orchestrator (LightningAI) | `CALIBRATED`, `OVERCONFIDENT`, `UNDERCONFIDENT`, or `INSUFFICIENT_DATA` |
| `predicted_p_win_raw` | float | `0.91` | Sweep capture (opt-in) | Pre-enforcement p_win (`NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW`) — separates model miscalibration from enforcement shrink |
| `calibration_brier` | float | `0.01` | EOD resolver | Brier score per record: (1−p)² on win / p² on loss (raw p_win when available) |

**Status thresholds:**
| Condition | Label |
|---|---|
| \|bias\| < 0.05 | `CALIBRATED` — predictions match outcomes |
| bias ≥ +0.05 | `OVERCONFIDENT` — too optimistic |
| bias ≤ −0.05 | `UNDERCONFIDENT` — too pessimistic |
| < 20 records (rolling 7-day window) | `INSUFFICIENT_DATA` |

### Sweep Fatigue Fields (v3)

| Field | Type | Example | Source | Description |
|---|---|---|---|---|
| `sweep_skip_until` | timestamp | `1782051223299` | Orchestrator (LightningAI) | Unix ms — heavy sweeps are skipped until this time (12h future when CALIBRATED) |

---

## Heavy Sweep Grid Selection & Reachability (2026-08-25 onward)

The heavy sweep (`regime_platform/sweep_orchestrator/heavy_sweep.py`, weekly
per symbol) runs a Renko brick/SL/TP grid search and picks the best
combo by Sharpe ratio. Multiple independent, shadow-first correctness
layers sit on top of that raw Sharpe pick, each gated by its own
`off | shadow | live` flag in `config/feature_flags.yaml` and each
tracked to a review date in `nt_roadmap`:

| Mechanism | Mode (current) | What it does |
|---|---|---|
| `heavy_sweep_deadline_aware_scoring_mode` | **live** (2026-09-08) | Re-scores every grid candidate with a 48h-deadline-capped Sharpe — a backtest trade that would resolve after the live signal's expiry window no longer counts as a "win" at selection time. |
| `heavy_sweep_atr_anchored_bricks_mode` | **shadow** (2026-09-13, "Phase 3b") | Adds 3 extra ATR-scaled brick candidates to the grid so symbols whose native grid never lands near price (e.g. gold pairs) get a chance at a tighter, more reachable bracket. |
| `reachability_vol_tier` | **live** (2026-09-13) | Buckets historical bracket-resolution expectancy by `(symbol, vol_tier, distance/ATR)` instead of `(symbol, distance/ATR)` alone — the same distance/ATR bucket has very different real win rates depending on the HMM's current vol regime. |
| `heavy_sweep_reachability_mode` | **shadow** (2026-08-28, "Phase 3a") | `reachability_aware_pick()` compares the raw Sharpe-best pick against a reachability/expectancy-aware alternative — if the Sharpe-best bracket lands in a bucket with negative historical expectancy and a better-expectancy alternative exists, shadow mode logs what it *would* have picked instead. |
| `macro_regime_mode` | **shadow** (2026-08-28) | 200-day-SMA trend read per symbol (`macro_filter.py`) + `nt_macro_edge_by_symbol` tracks whether trading with vs. against that trend actually wins historically, per symbol. |

**Review cadence**: `nt_roadmap` tracks each mechanism's shadow-accumulation
progress and review date (Phase 3b's three related rows share
**2026-10-05**). Daily pulse checks re-verify orchestrator health, gate
these review dates, and re-pull the shadow comparison tables — see
`worklog/20260913_PHASE_3B_GRID_WIDENING_SCOPE.md`,
`worklog/20260913_REACHABILITY_VOL_TIER_SCOPE.md`, and
`worklog/20260825_CALIBRATION_REFRAME_SCOPE.md` for the full scope of
each.

**2026-09-14 shadow-composition bug (fixed same day)**: the
deadline-aware and reachability-aware selection stages were each
individually correct in shadow mode, but both re-derived their pick
from the *full* grid — which already includes ATR-anchored candidates
once that mode is `shadow` — silently bypassing the ATR-anchoring
stage's own restriction. Confirmed live (XAUUSD's widened combo shipped
to production despite `heavy_sweep_atr_anchored_bricks_mode="shadow"`).
Fixed by restricting both later stages' candidate pool to whatever the
ATR-anchoring stage actually authorized. See
`worklog/20260914_DEADLINE_AWARE_ATR_ANCHOR_SHADOW_LEAK.md` and
`tests/test_heavy_sweep_shadow_mode_composition.py`.

---

## GPU Acceleration

```bash
pip install pomegranate torch
GET /gpu/capabilities
# -> {"active_device": "cuda", "hmm_backend": "GpuRegimeHMM (pomegranate)"}
```

Transparent fallback to hmmlearn on CPU - no code changes required.

---

## Walk-Forward Backtest Engine

`core/backtester.py` runs a comprehensive walk-forward backtest with HMM regime detection:

1. **Rolling HMM fit** on a trailing window (default 200 bars)
2. **Strategy signal generation** based on detected regime
3. **Regime-gated Kelly position sizing**
4. **Risk check gates** (VaR, max drawdown)
5. **Trade execution** with configurable commission and slippage
6. **30+ performance metrics** computed

Results are persisted to Supabase `ta_backtest_result` table (per-user scoped) for history, comparison, and export.

### Parameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `window` | 200 | 81+ | Trailing HMM window |
| `refit_every` | 50 | 10+ | HMM refit interval |
| `n_hmm_states` | 4 | 2-4 | Number of HMM hidden states |
| `kelly_fraction` | 0.5 | 0.1-1.0 | Fractional Kelly multiplier |
| `target_vol` | 0.15 | 0-1.0 | Annualised target volatility |
| `base_risk_limit` | 0.02 | 0-0.5 | Max daily loss fraction |
| `initial_equity` | 100000 | >0 | Starting equity |
| `commission_bps` | 5.0 | 0-50 | Commission in basis points |
| `slippage_bps` | 2.0 | 0-50 | Slippage in basis points |
| `max_position_pct` | 0.25 | 0-1.0 | Max position as equity fraction |
| `risk_check` | true | - | Enable VaR/drawdown risk gates |
| `regime_gate` | true | - | Only trade in favorable regimes |
| `save` | true | - | Persist result to Supabase |

### Optimization (Grid Search)

`POST /backtest/optimize` accepts a `param_grid` dict mapping parameter names to arrays of values. For each combination, a full walk-forward backtest runs and returns summary metrics. Maximum 50 combinations per request to prevent timeout. Returns `best_by_sharpe` and `best_by_return` highlights.

### Export

`POST /backtest/export` streams a saved result as CSV or JSON with section selection (`summary`, `equity_curve`, `drawdown_curve`, `trade_log`).

---

## Renko HFT Pipeline

The `renko/` module is a self-contained 6-layer Renko brick pipeline with 14 endpoints:

1. **Brick Construction** - Convert ticks to fixed/ATR Renko bricks
2. **Swing Classification** - Label brick sequences as impulse/correction
3. **Pattern Detection** - Identify double-top, head-shoulders, etc.
4. **Signal Filter** - Confirm patterns with volume and trend alignment
5. **Risk Manager** - Position sizing, stop-loss, take-profit
6. **Execution** - Paper-trade signal execution with P&L tracking

### Features
- Supabase caching with 4-hour TTL
- Yahoo Finance warm-up integration
- Per-symbol brick state persistence
- Real-time tick ingestion and brick updates
- Pattern confidence scoring
- Signal-to-trade pipeline

### Endpoints

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `POST` | `/renko/tick` | Process single tick through 6-layer pipeline | Write |
| `POST` | `/renko/tick/batch` | Batch tick processing (warmup/backtest) | Write |
| `GET` | `/renko/state` | Current pipeline state snapshot | Any |
| `GET` | `/renko/stats` | Comprehensive pipeline statistics | Any |
| `GET` | `/renko/bricks` | Recent Renko bricks (last 500) | Any |
| `GET` | `/renko/classified` | Bricks with swing labels (HH/HL/LH/LL) | Any |
| `GET` | `/renko/signals` | Pattern signals history | Any |
| `GET` | `/renko/trades` | Trade journal entries | Any |
| `GET` | `/renko/swing-points` | Detected swing points | Any |
| `POST` | `/renko/regime` | Update HMM regime for gate filter | Write |
| `POST` | `/renko/equity` | Update account equity | Write |
| `POST` | `/renko/config` | Update pipeline configuration (resets pipeline) | Admin |
| `POST` | `/renko/reset` | Reset pipeline for a symbol | Admin |
| `GET` | `/renko/backtest/stats` | Backtest/trading statistics from journal | Any |
| `POST` | `/renko/backtest/run` | Run full Renko pipeline backtest on historical prices | Write |
| `POST` | `/renko/backtest/run/stream` | SSE streaming backtest with progressive results | Write |
| `POST` | `/renko/backtest/compare` | Compare multiple Renko configs side-by-side (2–10) | Any |
| `POST` | `/renko/backtest/optimize` | Grid-search parameter sweep for Renko pipeline (max 50 combos) | Write |
| `POST` | `/renko/statistics/rigor` | Statistical rigor analysis (bootstrap, Monte Carlo) | Any |
| `POST` | `/renko/execution/model` | Execution modeling (slippage, fill simulation) | Any |
| `GET` | `/renko/live/status` | Check live execution mode status | Any |
| `POST` | `/renko/live/toggle` | Enable/disable live execution | Admin |
| `POST` | `/renko/snapshot/restore` | Restore pipeline from Supabase snapshot | Write |

---

## Execution Boundary (P5) — Sweep Orchestrator Only

As of v7.6.0, the FastAPI backend is a **sweep orchestrator only** — it does NOT execute trades, manage positions, or operate as the canonical trade journal. The execution layer has been migrated to the Hermes agent (`noble-trader-agent`, branded **Talaria**) which owns live trade journaling, BayesianAlpha persistence, and order routing to brokers. This split reflects an architectural clarification rather than a capability reduction: the backend was always a research and sweep system at heart, while the agent repo grew into the execution authority organically through P3–P5. The P5 contract codifies the boundary with a single environment flag and a runtime guard that all execution-touching code paths must call before doing anything that would touch a live broker or write to a live trade journal.

| Concern | Backend (this repo) | Agent (Talaria) |
|---|---|---|
| Sweep signal generation | ✓ canonical | delivered to Discord (human-facing); agent execution is independent |
| EV Engine (4-source blend) | ✓ canonical (orchestrator) | consumes synthesized P_win |
| Trade execution | ✗ disabled (P5) | ✓ canonical (MT4/MT5 bridge, Alpaca, Hyperliquid) |
| Live trade journal | ✗ gated behind `assert_execution_enabled()` | ✓ canonical (DuckDB `trade_journal`) |
| Bayesian alpha persistence | ✗ deprecated module (`BayesianUpdater`) | ✓ canonical (`BayesianAlpha`, DuckDB-backed) |
| Risk gates / circuit breakers | ✓ orchestrator-side | ✓ execution-side (defense in depth) |

The default environment flag `NOBLE_TRADER_DISABLE_EXECUTION=true` enforces this boundary at the module level. Any code path that would attempt to write a live trade journal entry, schedule a Supabase calibration write, or route an order to a broker must call `regime_platform.services.execution_deprecation.assert_execution_enabled()` first. Calls that pass the gate in the disabled default state raise `ExecutionDisabledError`, which is caught by the orchestrator's per-symbol error boundary and logged.

### What this means for callers

- **Backtest replay is unaffected.** `renko/trade_journal.py:TradeJournal.record()` still appends to the in-memory deque and runs the full statistical-rigor pipeline for offline backtests. The execution gate only short-circuits the Supabase calibration write — the in-memory data structures remain intact so backtests stay reproducible across environments.
- **Live Supabase calibration writes are silently skipped** with a WARNING log when execution is disabled. The agent's DuckDB-backed `trade_journal` table is the source of truth for live trades; the backend's Supabase calibration tables are no longer written to from the live path and remain queryable for historical replay.
- **BayesianUpdater is formally deprecated.** A `DeprecationWarning` fires on construction; the canonical live-persistence path is the agent's `hermes/agent/bayesian_alpha.py:BayesianAlpha` (decay-weighted, floor/ceiling-clamped, thread-safe, DuckDB-persistent). A `BayesianUpdater.apply_to_ev_engine(ev_engine)` bridge method exists for backtest code that still constructs a `BayesianUpdater` to apply its rolling posterior to an `EVEngine` instance without bypassing the validated `set_weights()` API.

### EV Engine v4 — Public Weight API

The `EVEngine` class (`regime_platform/services/statistics/ev_engine.py`) now exposes a validated public API for runtime weight overrides. This replaces the prior pattern of reaching into `ev_engine._full_weights` directly, which silently bypassed validation and could leave the engine in an inconsistent state on exception. The sweep orchestrator's soft-TimesFM-gate path now uses `with ev_engine.temp_weights({"p_timesfm": base * scale}):` exclusively, guaranteeing that the original weights are restored even when the gated evaluation raises.

| Method | Purpose |
|---|---|
| `set_weights(overrides: dict)` | Persistently overrides one or more of the 4 canonical weight keys (`p_pattern`, `p_regime`, `p_markov_hold_n`, `p_timesfm`). Validates key names and value ranges; raises `ValueError` on unknown keys, `TypeError` on non-dict input, `ValueError` on non-finite or out-of-range values. |
| `temp_weights(overrides: dict)` | Context manager that applies overrides for the duration of a `with`-block, then restores the prior weights in a `finally` clause (survives exceptions, returns, breaks). Validation runs before the body so a bad override set never leaks a partial mutation. |
| `restore_default_weights()` | Restores the default weight vector loaded from `config/default.yaml`. Useful in test teardown and after long-running override sessions. |
| `get_weights()` | Returns a deep copy of the current weight dict. Mutation of the returned dict does not affect the engine. |

The v4 weight keys are non-negotiable: legacy v3 keys like `p_ext_signal` are explicitly rejected with a migration message pointing the caller to the new canonical names. This prevents silent weight-set drift across the codebase as the v3→v4 migration completes.

---

## MCP Server (Model Context Protocol)

The platform exposes an MCP server at `/mcp` via `fastapi-mcp`, enabling AI agents to invoke read-only and analysis endpoints as tools.

### Configuration
The MCP mount is always gated behind the same `X-API-Key` header as every
other endpoint (`require_api_key`) — no separate MCP-specific auth toggle.

### Excluded Operations
Feed start/stop, Renko write operations, stream injection, and backtest delete are excluded from MCP exposure for security.

### Available Tools
All read-only and analysis endpoints are automatically exposed as MCP tools with proper request/response schemas.

---

## Strategy Signals

`POST /strategy/signal` generates a full trading signal based on the current regime:

| Signal | Condition |
|--------|-----------|
| `long` | Low/med-low vol + bull/strong_bull trend |
| `short` | High/med-high vol + bear/strong_bear trend (strength × 0.5) |
| `flat` | All other regimes |

Returns combined regime context, Kelly position sizing, and risk guidance in one response.

## Topological Data Analysis (TDA)

`POST /tda/features` extracts persistent homology features from price series:

- Takens embedding with configurable dimension and delay
- Persistence diagrams (0D and 1D features)
- Betti numbers at each filtration step
- Persistence entropy for regime change detection
- Anomaly score based on deviation from baseline topology

The 10-dimensional TDA feature vector can be appended to the observation vector via `POST /observation/build-v2` with `include_tda=true`.

---

## Discord Notifications

The platform sends real-time alerts to a Discord channel via webhook:

- Regime change alerts (with severity coloring)
- Circuit breaker triggers
- Trade execution confirmations
- Error notifications

Configure via `DISCORD_WEBHOOK_URL` environment variable. Graceful no-op when not configured.

---

## Circuit Breakers

Three-tier circuit breaker system protects against cascading failures:

| Breaker | Threshold | Recovery |
|---------|-----------|----------|
| **Error rate** | >50% errors in 60s | 30s cooldown |
| **Latency** | >5s average in 60s | 30s cooldown |
| **Concurrent** | >100 concurrent requests | 30s cooldown |

Circuit breaker stats are reported in the `/health` endpoint.

---

## Notes

- Minimum **81 price bars** required (`min_bars = max(81, n_components × 20)`)
- `Model is not converging` warnings from hmmlearn are expected on short series - `_sanitize_model()` recovers automatically
- All position fractions are in `[0, 1]` portfolio-fraction space - multiply by notional externally
- VaR/CVaR are **historical (empirical)** - more robust for fat-tailed return distributions
- `covariance_type="diag"` used for rolling windows < 300 bars; switch to `"full"` with 500+ seed bars
- The 4-state HMM produces 4 vol + 4 trend posteriors; the obs builder aggregates these into 3 Markov buckets (indices 14–19), re-normalised after aggregation
