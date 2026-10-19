# Refined Thesis: What TimesFM Should Forecast for Kairos

> **Date**: 2026-10-18 · **Status**: Draft for review · **Supersedes**: `Zeroshot Thesis for TSFM.md`

---

## Goal

Determine what TimesFM should forecast to maximize value to Kairos, backed by empirical evidence from published research — then build the evaluation framework to validate it.

---

## Current context / assumptions

### What the original thesis proposed

The original thesis ("Zeroshot Thesis for TSFM") proposed:
- Fine-tune TSFM from univariate to universal forecasting
- Predict next regime, trend, direction, and price
- Tiered horizons: <30 days rules, 30–90 days statistical, 90+ days TSFM
- Rolling backtests with frozen cutoffs
- Include covariates shifted to publication time
- Score point accuracy and quantiles
- Symbols: BTCUSD, XAGUSD, XAUUSD, USDJPY, USDCHF; 1 year of data

### What the current implementation actually does

| Aspect | Reality |
|--------|---------|
| **Model** | `google/timesfm-2.5-200m-pytorch` zero-shot (no fine-tuning) |
| **Horizon** | 1 bar (next-bar prediction) |
| **Input** | Univariate close price, last 500 bars |
| **Output → probability** | `rel_change → sigmoid(k·rel_change)` or `Φ(rel_change/σ_symbol)` (zmap) |
| **Role in pipeline** | 5/14 weight in logit-pooled P_win blend (`ev_engine.py` line 117) |
| **Gates** | `timesfm_available` (soft), `timesfm_confidence_ok` (min 0.05, soft) |
| **Performance** | Probes showed p_timesfm pinned at 0.496–0.501 — near-zero directional signal |
| **Infrastructure** | Solid: API-key auth, rate limiting, 15-min cache, fallback URLs, probe logging |

### What research actually shows about TSFMs in finance

| Finding | Source | Implication for Kairos |
|---------|--------|----------------------|
| **Zero-shot TSFMs underperform on financial data.** TimesFM 500M zero-shot: R²_OOS = −2.80%, directional accuracy < 50%. Chronos large: R²_OOS = −1.37%. Both lose to CatBoost/LightGBM. | Marconi et al. (2025), "Re(Visiting) TSFMs in Finance" | Current zero-shot usage is near-worthless as a directional signal — confirmed by live probes |
| **Generic pre-training does not transfer to finance.** Finance-native pre-training is essential. | Same | Fine-tuning on Kairos's own symbol data is not optional — it's the difference between noise and signal |
| **Fine-tuned TimesFM outperforms baselines.** Continual pre-training on 100M financial time points → higher accuracy than zero-shot, with improved Sharpe, returns, max drawdown. | Fu et al. (2024), "Financial Fine-tuning a Large Time Series Model" | Fine-tuning is the path to value, but requires training infrastructure |
| **Volatility forecasting is where TSFMs shine.** Incremental fine-tuning essential for learning volatility patterns; fine-tuned variants outperform traditional models (GAS, GARCH). | "Foundation Time-Series AI Model for Realized Volatility Forecasting" (2025) | Volatility is a higher-signal target than direction for TSFMs |
| **Shorter horizons outperform longer ones for financial VaR.** FT1 (1-day) outperforms FT21 and FT63; longer horizons show "significant weaknesses." | "Time-Series Foundation Model for VaR Forecasting" (2024) | The thesis's "90+ days" tier is not supported by evidence |
| **TimesFM 3.0 (Aug 2026) adds native multivariate + covariates.** Rank #1 on fev-bench, TIME, GIFT-Eval. | PyPI/GitHub (Aug 2026) | Upgrading unlocks multivariate inputs and covariate support natively |
| **LoRA fine-tuning works for trading.** <100K trainable params via LoRA adapters; horizon = 48×H1 (2 trading days); covariates: moon phases, economic calendar; Sharpe 1.68 on S&P500. | MQL5 article (2025) | A practical fine-tuning recipe exists and is reproducible |
| **TSFMs require 3–10 fewer years of data than training from scratch.** Pre-training provides sample efficiency even if domain-specific fine-tuning is still needed. | Marconi et al. (2025) | Fine-tuning on Kairos's 1-year history is feasible (would be marginal for from-scratch) |

### What Kairos already has (and doesn't need TSFM to replace)

| Component | What it does | TSFM role |
|-----------|-------------|-----------|
| **HMM regime detection** | Classifies market into bull/bear/neutral states from 24-feature observation vector | TSFM should NOT replace this — HMM is local, interpretable, and already fitted |
| **Markov chain (T^N)** | Multi-step hold probability: "will price reach TP within N bars?" | TSFM should NOT replace this — it's a different question |
| **ATR-based brick sizing** | Adaptive brick geometry from volatility | TSFM volatility forecast could ENHANCE this |
| **EV Engine v6** | Logit-pooled P_win from p_regime, p_markov, p_timesfm (5:4:5) | TSFM contributes 5/14 weight but currently adds near-zero signal |
| **Kelly sizing + Dynamic Masaniello** | Position sizing from P_win and RR | Better volatility forecast → better sizing |

### The core problem

The current implementation uses TimesFM at **horizon=1** (next-bar direction), which:
1. Is the **noisiest, least predictable** horizon — confirmed by research (longer horizons are worse, but horizon=1 is noise-dominated)
2. Produces **p≈0.5 always** — confirmed by live probes
3. Wastes the model's core advantage (capturing **medium-range patterns**)
4. Has **no evidence of value** — no backtest, no baseline comparison

---

## Architecture / proposed approach

### Refined thesis

**TimesFM should forecast realized volatility at a 24–72 hour horizon, fine-tuned on Kairos's own symbol data with covariates (ATR, time-of-day, day-of-week, lagged returns), to enhance volatility-sensitive components (Kelly sizing, brick geometry, risk gates) — not to predict next-bar direction.**

This is because:
1. **Volatility is more predictable than direction** — higher signal-to-noise ratio, stronger empirical evidence for TSFM fine-tuning gains
2. **Kairos already has direction covered** — HMM + Markov chains handle directional/regime questions
3. **Volatility directly improves Kairos's core competency** — position sizing and risk management
4. **24–72h horizon matches Kairos's cadence** — light sweep (15 min) and heavy sweep (weekly) both benefit from medium-horizon volatility forecasts
5. **Fine-tuning on 1 year of data is feasible** — LoRA adapters need <100K params, and pre-training provides sample efficiency

### Evidence hierarchy for the recommendation

| Claim | Evidence strength | Source |
|-------|------------------|--------|
| Zero-shot TSFMs are weak on financial data | **Strong** — multiple independent studies, consistent finding | Marconi et al. 2025, live Kairos probes |
| Fine-tuning is essential for financial TSFMs | **Strong** — demonstrated across price, return, and volatility tasks | Fu et al. 2024, Marconi et al. 2025, Preferred.jp |
| Volatility forecasting is a high-value TSFM target | **Moderate** — strong for volatility specifically, less for direction | Volatility forecasting paper 2024, VaR paper 2024 |
| Shorter horizons outperform longer for financial tasks | **Moderate** — shown for VaR; direction evidence is mixed | VaR paper 2024 |
| LoRA fine-tuning works for trading TSFMs | **Moderate** — one practical implementation (MQL5), Sharpe 1.68 | MQL5 article 2025 |
| Covariates improve TSFM forecasts | **Weak-Moderate** — TimesFM 3.0 supports it; MQL5 demo uses moon phases/economic calendar | PyPI Aug 2026, MQL5 2025 |

### What this means for the EV engine

The current EV blend treats `p_timesfm` as interchangeable with `p_regime` and `p_markov_hold_n` — three conceptually different quantities pooled as "votes" on P(win). Under the refined thesis:
- **Remove `p_timesfm` from the P_win blend** — it's not a directional signal
- **Add a volatility forecast** (`vol_forecast`) as a separate input to Kelly sizing and risk gates
- The EV blend becomes: `p_win = logit_pool(p_regime, p_markov_hold_n)` (2-source, renormalized)
- `vol_forecast` feeds into: `kelly_multiplier`, `brick_size_adjustment`, `position_size_cap`

---

## Step-by-step tasks

### Phase 0: Evaluation framework (blocking — nothing else until this exists)

#### Task 0.1: Create backtest harness for volatility forecasts

**File**: `tests/test_vol_forecast_backtest.py`

Create a rolling-origin backtest that:
1. Takes a price series (1h bars) for a symbol
2. For each cutoff date T in the series (every 24h, min 50 cutoffs):
   - Trains/fits a volatility forecast using only data ≤ T
   - Forecasts realized volatility for [T+1, T+24h]
   - Records forecast vs. actual realized volatility
3. Computes metrics: MAE, RMSE, MAPE, directional accuracy (up/down in vol), quantile coverage

**Verification**: `pytest tests/test_vol_forecast_backtest.py -v` — all pass with naive baseline (last-24h realized vol as forecast).

```python
# tests/test_vol_forecast_backtest.py
"""Rolling-origin backtest for volatility forecasts."""
import numpy as np
import pytest

def realized_volatility(prices: np.ndarray, window: int = 24) -> float:
    """Annualized realized vol from log returns over `window` bars."""
    log_returns = np.diff(np.log(prices[-window:]))
    return float(np.std(log_returns) * np.sqrt(365 * 24))  # hourly → annualized

def naive_vol_forecast(historical_prices: np.ndarray, horizon: int = 24) -> float:
    """Naive: last `horizon` bars' realized vol."""
    return realized_volatility(historical_prices, window=horizon)

class TestVolForecastBacktest:
    def test_naive_baseline_produces_forecast(self):
        prices = np.array([100.0 * (1 + 0.001 * i) for i in range(200)])
        forecast = naive_vol_forecast(prices)
        assert forecast > 0
        assert np.isfinite(forecast)

    def test_rolling_backtest_completes(self):
        prices = np.random.lognormal(0, 0.01, 500).cumprod() * 100
        cutoffs = range(100, 400, 24)
        results = []
        for cutoff in cutoffs:
            hist = prices[:cutoff]
            actual = realized_volatility(prices[cutoff:cutoff+24])
            forecast = naive_vol_forecast(hist)
            results.append((cutoff, forecast, actual))
        assert len(results) == len(list(cutoffs))
        assert all(np.isfinite(f) and np.isfinite(a) for _, f, a in results)

    def test_metrics_computation(self):
        forecasts = np.array([0.2, 0.25, 0.3, 0.15])
        actuals = np.array([0.22, 0.24, 0.28, 0.18])
        mae = np.mean(np.abs(forecasts - actuals))
        assert abs(mae - 0.0225) < 0.001
```

**Expected**: `pytest tests/test_vol_forecast_backtest.py -v` → 3 passed.

#### Task 0.2: Add TimesFM 2.5 volatility forecast to the backtest

**File**: `tests/test_vol_forecast_backtest.py` (extend)

Add a `timesfm_vol_forecast` function that:
1. Takes historical prices
2. Calls the existing `/forecast` endpoint with `horizon=24`, `inputs=[prices]`
3. Returns the point forecast of log realized volatility (forecast log-returns, compute their std)

**Verification**: Test against the live server with a known symbol — the forecast should be finite and positive.

#### Task 0.3: Benchmark TimesFM vs. naive vs. GARCH

**File**: `scripts/benchmark_vol_forecasts.py`

Run the backtest over all 5 thesis symbols (BTCUSD, XAGUSD, XAUUSD, USDJPY, USDCHF) with 1 year of hourly data:
- Naive: last-24h realized vol
- GARCH(1,1): fitted on rolling window (use `arch` library if available, else skip)
- TimesFM zero-shot: current model
- TimesFM fine-tuned: (Phase 4, not yet implemented — stub for now)

Output: MAE, RMSE, MAPE, directional accuracy, quantile coverage per model per symbol.

**Verification**: `python scripts/benchmark_vol_forecasts.py` → CSV output with all symbols, at minimum naive vs. TimesFM comparison.

#### Task 0.4: Statistical significance testing

**File**: `scripts/benchmark_vol_forecasts.py` (extend)

Add Diebold-Mariano test (or Giacomini-White) to determine if TimesFM volatility forecasts are statistically significantly better than naive.

**Verification**: Report p-values; flag any comparison where p > 0.05 as "not significant."

---

### Phase 1: Upgrade to TimesFM 3.0 and add covariates

#### Task 1.1: Upgrade model to TimesFM 3.0

**File**: `server.py` (line 55)

Change:
```python
MODEL_ID = os.getenv("TIMESFM_MODEL_ID", "google/timesfm-2.5-200m-pytorch")
```
To:
```python
MODEL_ID = os.getenv("TIMESFM_MODEL_ID", "google/timesfm-3.0-200m-pytorch")
```

Update `ForecastConfig` to enable multivariate + covariate support (check exact API once 3.0 is confirmed available on HuggingFace).

**Verification**: `python server.py` → health endpoint reports model loaded, `/info` shows `model: google/timesfm-3.0-200m-pytorch`.

#### Task 1.2: Add covariate construction

**File**: `regime_platform/sweep_orchestrator/vol_forecast.py` (new file)

Create a function that builds covariate arrays aligned with price bars:
```python
def build_covariates(
    prices: list[float],
    timestamps: list[float],  # unix timestamps
    atr: list[float] | None = None,
) -> dict[str, list[float]]:
    """Build covariate dict for TimesFM 3.0 multivariate forecasting.
    
    Covariates:
    - hour_of_day: 0-23 (sin/cos encoded)
    - day_of_week: 0-6 (sin/cos encoded)
    - log_return_1h: lagged log return
    - atr_normalized: ATR / price (if available)
    """
```

**Verification**: Unit test with synthetic data — covariates have correct shape, no NaNs.

#### Task 1.3: Extend the forecast endpoint to accept covariates

**File**: `server.py` (lines 201-216)

Update `ForecastRequest` to accept optional `covariates` field, and pass them to `model.forecast()` if the model supports it.

**Verification**: POST to `/forecast` with covariates → 200 response with forecast.

---

### Phase 2: Fine-tune TimesFM on Kairos data

#### Task 2.1: Build training data pipeline

**File**: `scripts/prepare_timesfm_training.py` (new file)

Script that:
1. Fetches 1 year of hourly data for each of the 5 thesis symbols
2. Constructs training examples: (context_window=512 bars, target=next 24 bars of realized vol)
3. Splits: train on data ≤ 2025-06-01, test on data after
4. Saves as numpy arrays or HF dataset format

**Verification**: `python scripts/prepare_timesfm_training.py` → prints dataset shape, saves to `data/timesfm_training/`.

#### Task 2.2: LoRA fine-tuning script

**File`: `scripts/finetune_timesfm_lora.py` (new file)

Fine-tune TimesFM 3.0 using LoRA adapters on the training data:
1. Load base model
2. Attach LoRA adapters (rank=8, alpha=16, target_modules=["q_proj", "v_proj"])
3. Train on volatility prediction task (MSE loss on log realized vol)
4. Save adapter weights to `checkpoints/timesfm_lora_vol/`

**Verification**: `python scripts/finetune_timesfm_lora.py` → trains for N epochs, saves adapter, prints final loss.

#### Task 2.3: Load fine-tuned adapter in server

**File**: `server.py` (lines 74-91)

Update lifespan to load LoRA adapter if present:
```python
adapter_path = Path(__file__).parent / "checkpoints" / "timesfm_lora_vol"
if adapter_path.exists():
    model.load_lora_adapter(str(adapter_path))
```

**Verification**: `python server.py` → log shows "Loaded LoRA adapter from checkpoints/timesfm_lora_vol".

---

### Phase 3: Integrate volatility forecast into Kairos

#### Task 3.1: Add vol_forecast to sweep result

**File**: `regime_platform/sweep_orchestrator/heavy_sweep.py`

After TimesFM fetch (line 696), add:
```python
vol_forecast = await _fetch_vol_forecast_cached(symbol, prices)
result.vol_forecast = vol_forecast
```

**New file**: `regime_platform/sweep_orchestrator/vol_forecast.py`
- `_fetch_vol_forecast_cached(symbol, prices)` — 15-min cache, calls TimesFM with horizon=24
- Returns forecasted realized volatility (annualized) or None

#### Task 3.2: Wire vol_forecast into Kelly sizing

**File**: `regime_platform/sweep_orchestrator/orchestrator.py`

In the Kelly computation, use `vol_forecast` as a volatility multiplier:
```python
if vol_forecast is not None and vol_forecast > 0:
    vol_multiplier = vol_forecast / current_realized_vol
    kelly *= max(0.5, min(2.0, vol_multiplier))  # clamp to [0.5, 2.0]
```

**Verification**: Unit test — with vol_forecast > current vol, Kelly increases; with vol_forecast < current vol, Kelly decreases.

#### Task 3.3: Wire vol_forecast into risk gates

**File**: `regime_platform/core/feature_flags.yaml` + `regime_platform/sweep_orchestrator/orchestrator.py`

Add a `vol_regime_gate` gate:
- If `vol_forecast` implies vol regime shift (>1.5× current), flag as `volatility_regime_shift`
- This gate is `shadow` initially (logs only, doesn't block)

---

### Phase 4: Remove p_timesfm from EV blend, replace with vol_forecast

#### Task 4.1: Remove p_timesfm from P_WIN_WEIGHTS_FULL

**File**: `regime_platform/services/statistics/ev_engine.py` (lines 109-118)

Change:
```python
P_WIN_WEIGHTS_FULL = {
    "p_regime": 5.0 / 9.0,      # was 5/14, renormalized
    "p_markov_hold_n": 4.0 / 9.0,  # was 4/14, renormalized
    # p_timesfm removed — not a directional signal
}
```

**Verification**: Unit test — `compute_ev()` with only p_regime and p_markov produces valid p_win.

#### Task 4.2: Update all references to p_timesfm in EV engine

**Files**: `ev_engine.py`, `orchestrator.py`, `heavy_sweep.py`

Remove or conditionally skip `p_timesfm` in `compute_ev()` calls. Update the `EVFullResult` dataclass to make `p_timesfm` optional (default None).

**Verification**: All existing tests pass; `compute_ev()` works with 2 or 3 sources.

#### Task 4.3: Add vol_forecast as a separate sizing input

**File**: `regime_platform/services/statistics/ev_engine.py`

Add a new method or extend `compute_ev()` to accept an optional `vol_forecast` parameter that adjusts Kelly sizing separately from P_win:
```python
def compute_ev(
    self,
    p_regime: float | None = None,
    p_markov_hold_n: float | None = None,
    p_timesfm: float | None = None,  # deprecated, ignored
    vol_forecast: float | None = None,  # new
    current_vol: float | None = None,
) -> EVFullResult:
```

**Verification**: Unit test — vol_forecast > current_vol increases position size; vol_forecast < current_vol decreases it.

---

### Phase 5: Rolling backtest of the full pipeline

#### Task 5.1: End-to-end backtest script

**File**: `scripts/backtest_kairos_with_vol.py` (new file)

Simulate the full Kairos pipeline (HMM + Markov + EV + Kelly) over a rolling window, with and without the vol_forecast enhancement. Compare:
- Total return, Sharpe, max drawdown
- Win rate, profit factor
- Position sizing distribution

**Verification**: `python scripts/backtest_kairos_with_vol.py` → CSV + summary report.

#### Task 5.2: Segment analysis

**File**: `scripts/backtest_kairos_with_vol.py` (extend)

Break down results by:
- Symbol (BTC vs. forex vs. metals)
- Volatility regime (high vol / low vol periods)
- Trend direction (bull / bear / chop)

**Verification**: Report shows per-segment metrics, not just aggregate.

---

## Tests / validation

### Per-task TDD cycle

For each task above:
1. Write the failing test
2. Run: `pytest tests/test_<task>.py -v` → verify failure
3. Implement minimally
4. Run: `pytest tests/test_<task>.py -v` → verify pass
5. Run full suite: `pytest -v` → verify no regressions

### Integration validation

| Check | Command | Expected |
|-------|---------|----------|
| TimesFM server healthy | `curl http://localhost:8002/health` | `{"status":"ok","model":"google/timesfm-3.0-..."}` |
| Forecast with covariates | `curl -X POST .../forecast -d '{"horizon":24,"inputs":[...],"covariates":[...]}'` | 200, finite forecast |
| EV engine 2-source blend | `pytest tests/test_ev_engine.py -v` | All pass, p_win in [0,1] |
| Vol forecast wired | `pytest tests/test_vol_forecast.py -v` | All pass |
| No p_timesfm in P_win | `grep -r "p_timesfm.*P_WIN" regime_platform/` | 0 matches |

### Benchmark acceptance criteria

The fine-tuned TimesFM volatility forecast must achieve:
1. **MAE improvement ≥ 15%** over naive baseline (last-24h realized vol)
2. **Directional accuracy ≥ 55%** for vol up/down (vs. 50% chance)
3. **DM test p-value < 0.05** vs. naive
4. **Quantile coverage** within [0.80, 0.95] for the 80% interval (not too narrow, not too wide)

If these criteria are not met, the fine-tuning approach is not viable and the thesis should be revised.

---

## Risks, tradeoffs, and open questions

### Risks

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| TimesFM 3.0 not available or API-incompatible | Medium | Pin to 2.5 initially; upgrade when stable |
| LoRA fine-tuning on 1 year of data overfits | High | Use strong regularization, early stopping, validate on holdout |
| Vol forecast doesn't improve Kelly sizing | Medium | A/B test in shadow mode first; measure before/after |
| Removing p_timesfm from EV blend reduces signal | Low | It's currently near-zero; renormalized 2-source blend should be equivalent |
| Covariates introduce lookahead bias | Medium | All covariates must be point-in-time (no future data) |

### Tradeoffs

| Decision | Pros | Cons |
|----------|------|------|
| Volatility vs. direction as TSFM target | Stronger evidence, higher SNR, complements existing HMM | Requires new integration point (Kelly, not EV) |
| LoRA vs. full fine-tuning | <100K params, less overfit risk, faster training | May not capture all domain-specific patterns |
| 24–72h horizon vs. 1 bar | Matches sweep cadence, more predictable | Less frequent signal refresh |
| Remove p_timesfm from EV | Cleaner conceptual model, removes noise source | Loss of a (currently weak) signal source |

### Open questions

1. **Is 1 year of data sufficient for fine-tuning?** Marconi et al. showed TSFMs need 3–10 fewer years than from-scratch, but 1 year is still short. May need synthetic data augmentation or transfer from a model fine-tuned on other financial data.

2. **Should the vol forecast be univariate or multivariate?** TimesFM 3.0 supports multivariate — should we feed correlated symbols (e.g., BTC + ETH) to improve BTC's vol forecast?

3. **What covariates actually help?** The MQL5 demo uses moon phases (questionable) and economic calendar (plausible). Need empirical validation with ATR, time features, and lagged returns.

4. **Does the vol forecast improve live trading, or just backtests?** The ultimate test is paper-trading with vol-enhanced Kelly vs. current Kelly. This requires careful A/B testing.

5. **Is TimesFM the right architecture?** Chronos-2 and Moirai-2 are also strong contenders. The evaluation framework should be model-agnostic.

---

## Summary of changes from original thesis

| Original | Refined | Rationale |
|----------|---------|-----------|
| Predict regime, trend, direction, price | Predict **realized volatility** | Vol is more predictable than direction; complements existing HMM |
| Horizon <30d rules, 30-90d stat, 90+d TSFM | Horizon **24–72 hours** | Research shows shorter horizons outperform for financial tasks; matches Kairos cadence |
| Zero-shot inference | **LoRA fine-tuning** on Kairos data | Zero-shot TSFMs underperform on finance; fine-tuning is essential |
| Univariate price input | **Covariates**: ATR, time features, lagged returns | TimesFM 3.0 supports covariates; proven to improve accuracy |
| Rolling backtest (proposed) | **Rolling backtest (required first phase)** | Nothing else can be validated without it |
| p_timesfm in EV blend (5/14) | **Remove from EV blend**; add vol_forecast to Kelly | P(up) from price forecast is a category error; vol forecast belongs in sizing |
| Score point accuracy and quantiles | **MAE, RMSE, directional accuracy, DM test, quantile coverage** | Specific, measurable acceptance criteria |

---

## Implementation status — 2026-10-18 (post-review build)

**Phase 0 is BUILT** (`vol_backtest/` + `scripts/run_vol_backtest.py` +
`tests/test_vol_backtest.py`; build record
`worklog/20261018_VOL_BACKTEST_PHASE0_BUILD.md`; 24/24 tests green; synthetic
3-symbol × 3-horizon run verified). It implements the **corrected**
methodology, superseding this plan's Tasks 0.1–0.4 as follows:

| Plan item | As built | Reason |
|---|---|---|
| Task 0.2 vol = std of point forecast | **Quantile-spread extraction** (per-step σ from q10/q90; RV = √Σσ²) | Std of a conditional-mean path measures slope, not dispersion — the original extraction was guaranteed to fail regardless of model quality |
| Task 0.3 baselines naive / GARCH / TimesFM | naive, EWMA, **Log-HAR (the bar)**, har_ewma blend, GARCH (optional) | HAR-RV is the literature's bar (Brini 2026: only TTM beat Log-HAR, 1.3–1.8%; TimesFM/Chronos-Bolt did not) |
| MAPE metric | **QLIKE** + log-scale MAE/RMSE; raw MAE per-symbol only | Near-zero weekend RV explodes MAPE; pooling lets BTC dominate |
| "≥55% vs coin flip" | Directional accuracy **with the measured persistence-rule null** | Vol is persistent; the coin-flip null overstates skill |
| DM test (unspecified) | DM with **Newey-West HAC** (lag = horizon/spacing − 1) + **Holm** | Overlapping forecast windows; multiple comparisons |
| Annualize √(365·24) | **Per-asset-class** (crypto 8760 vs FX/metals ~6240 h/yr), display only | Single factor overstated FX ~18% |

**Model decisions since this plan was written:**

1. **TimesFM 3.0 is license-blocked** for this use (self-hosted weights are
   non-commercial; commercial use is Google-Cloud-only, which also blocks
   fine-tuning). Task 1.1 (upgrade to `timesfm-3.0-200m-pytorch`) is VOID —
   the ID is also wrong (3.0 is 330M).
2. **Chronos-2** (Apache-2.0, 120M, native past + known-future covariates,
   multivariate/cross-learning, first-party LoRA) is the **only candidate that
   can reach this thesis's endgame** under the license constraint; it runs as
   `chronos2` and `chronos2_cov` arms. TimesFM 2.5 stays as the incumbent arm
   (`timesfm25`, via the repo's server.py). The framework is model-agnostic
   (Open Q5) — an arm is one class in `vol_backtest/model_arms.py`.
3. Phase 1's covariate construction is retargeted to Chronos-2's
   `predict_df(context_df, future_df)` surface: past covariates (calendar
   encodings, lagged |r|, trailing RV) into the context frame; known-future
   calendar features into the future frame.

**Deferred corrections (binding on Phases 3–4, recorded here so they are not
lost):**

- **Kelly sign (Tasks 3.2/4.3):** `kelly *= clamp(vol_forecast/current_vol,
  0.5, 2.0)` sizes UP into forecast vol spikes — backwards in every Kelly
  formulation. Correct: `kelly *= clamp(current_vol/vol_forecast, lo, hi)`
  with a no-trade cutoff; the unit tests must assert the inverted direction.
- **p_timesfm removal (Phase 4a):** at HEAD `p_markov_hold_n_mode: "off"` —
  removing p_timesfm leaves the blend single-source (p_regime). Do it as a
  flag-level renormalization (the engine already renormalizes on missing
  sources), with recalibration — not as `P_WIN_WEIGHTS_FULL` surgery.
- **Train split bug (Task 2.1):** "1 year of data" with a ≤2025-06-01 cutoff
  (doc dated 2026-10-18) yields an empty train set — the window must widen or
  the split move.
- **Citation:** "Marconi et al., Re(Visiting) TSFMs in Finance" is
  **Rahimikia, Ni & Wang** (arXiv 2511.18578) — and its finding is stronger
  than quoted: general TSFMs underperform on finance in zero-shot AND
  fine-tuning; only from-scratch financial pre-training showed substantial
  gains. Fine-tuning (Phase 2) stays gated on Phase 0 zero-shot results.

**Decision gate (unchanged in spirit, corrected nulls):** any model arm
beating **Log-HAR** per-symbol on QLIKE at DM/Holm significance proceeds to
fine-tuning; if none does, the Brini fallback (equal-weight TSFM+HAR blend)
is the measured answer and this thesis's own escape clause fires.
