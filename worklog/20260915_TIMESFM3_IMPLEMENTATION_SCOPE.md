# TimesFM-3 Implementation Scope for Noble Trader

**Status**: Approved for full implementation  
**Date**: 2026-09-15  
**Author**: Agent (noble-trader-fastapi-backend)  
**References**: 
- `commodity_12m_forecast_model_2027.md` (13 symbols, Q4 2026 → Q3 2027)
- `server.py` (TimesFM inference server, port 8000/8002)
- `orchestrator.py` (sweep orchestrator, port 8001)
- `regime_platform/core/feature_flags.py` (config centralization)
- `timesfm_k_adaptive.py` (66 lines, `map_forecast_to_probability` and `compute_adaptive_timesfm_k`)
- TimesFM-3: 330M param multivariate zero-shot foundation model (non-commercial license)

---

## Part 1: Headline Summary

### What Changed (2.5 → 3.0)

| Aspect | TimesFM 2.5 | TimesFM 3.0 |
|--------|-------------|-------------|
| **Model size** | 200M parameters | 330M parameters |
| **Forecast type** | Univariate (one series, its own history) | **Multivariate** (multiple series jointly, single forward pass) |
| **Input types** | Single `inputs` array | `inputs` (targets) + `dynamic_numerical_covariates` + `static_numerical_covariates` + `dynamic_categorical_covariates` + `static_categorical_covariates` |
| **Training data** | ~200B time points (univariate) | **1T+ time points** (multivariate, real + synthetic) |
| **Cross-series learning** | None — each series forecast independently | **Full variate attention** — learns correlations between series natively |
| **Fine-tuning** | Optional task-specific | **Zero-shot** — no task-specific fine-tuning |
| **Leaderboard rank** | Strong | **Top rank** on GIFT-Eval, fev-bench, TIME leaderboard (point + probabilistic) |
| **License** | Apache 2.0 | Weights: `timesfm-non-commercial-license-v1.0` (non-commercial only) |
| **Code** | Apache 2.0 | Apache 2.0 (same repo at `github.com/google-research/timesfm`) |

### What This Means for Noble Trader

**Currently**, the pipeline forecasts each commodity *independently*:
```
_orchestrator → _fetch_timesfm_cached(HO) → server.forecast(inputs=[HO_prices]) → p_timesfm for HO
_orchestrator → _fetch_timesfm_cached(CL) → server.forecast(inputs=[CL_prices]) → p_timesfm for CL
```
Each call has zero knowledge of the other. The `macro_filter.py` tries to compensate with a separate 200-day SMA computation, but that's a lagging indicator on a single series — no cross-asset signal.

**With TimesFM-3's multivariate capability**, the same pipeline becomes a **single request** covering multiple correlated commodities with the 12-month forecast ranges as **past-future covariates**:
```
server.forecast_with_covariates(
    inputs=[HO_prices, CL_prices, B_prices, RB_prices, VGT_prices],
    static_numerical_covariates={
        "HO": {"q4_2026_low": 160, "q4_2026_high": 190},
        "CL": {"q4_2026_low": 70, "q4_2026_high": 90},
        ...
    },
    xreg_mode="xreg + timesfm"
) → p_timesfm for EACH series + learned cross-correlations
```

The model's **full variate attention** mechanism learns that when Brent spikes, HO crack spreads widen, VGT natural gas correlates — **without any explicit macro layer**. The `commodity_12m_forecast_model_2027.json` provides the forward-looking backdrop as static covariates.

### How TimesFM-3 Works (For Noble Trader)

**1. Input Construction**
- `inputs`: A list of time series (one per commodity in the economic cluster). Each series is the symbol's own price history.
- `static_numerical_covariates`: Per-symbol forecast ranges (low/high) from the 12-month forecast model. These are known future values that condition the forecast.
- `dynamic_numerical_covariates`: Live macro indicators (optional) that change over the forecast horizon.
- `xreg_mode`: Controls how covariates interact with the model — `"xreg + timesfm"` fits XReg first, TimesFM forecasts residuals. `"timesfm + xreg"` runs TimesFM first, then XRefines on residuals.

**2. Full Variate Attention**
- The model's attention mechanism attends across ALL input series simultaneously.
- It learns that Brent moves HO, that VGT correlates with HO, etc. — no explicit feature engineering needed.
- This is fundamentally different from running univariate forecasts independently and manually combining them.

**3. XReg Conditioning**
- The static covariates (forecast ranges) anchor the forecast to the known macro backdrop.
- The model then forecasts deviations from this anchor using temporal patterns.
- `xreg_mode="xreg + timesfm"`: XReg fits on targets, TimesFM forecasts residuals — best for when covariates have strong linear relationship with targets.
- `xreg_mode="timesfm + xreg"`: TimesFM gets the base forecast, XRefines on residuals — best when temporal patterns dominate.

**4. Zero-Shot Generalization**
- No fine-tuning on commodity data needed.
- The model was pretrained on 1T+ time points of diverse series and generalizes to the commodity domain.

### What's Different from Current Forecasting

| | Current (2.5) | TimesFM-3 Path |
|---|---|---|
| **Cross-asset signal** | None — each series independent | Full variate attention learns correlations |
| **Macro context** | Separate `macro_filter.py` (lagging SMA) | Baked into forecast via past-future covariates |
| **Forward-looking** | No — only extrapolates from history | Yes — 12-month forecast ranges condition the output |
| **Inference** | N calls for N commodities | **1 call** for N commodities |
| **Confidence** | Single point + quantile per series | Per-series quantile + covariate contribution |
| **p_timesfm driver** | `sigmoid(k * rel_change)` on univariate forecast | Same mapping, but on multivariate forecast |

### Likely Outcomes

- **Higher hit rate** on directional accuracy (cross-asset signal captures moves that single-series cannot)
- **Better calibrated confidence** (probabilistic output + covariate conditioning → more honest uncertainty estimates)
- **Fewer false signals** during correlated moves (XReg separates signal from noise)
- **Faster inference** (one forward pass vs N passes for N commodities)
- **Forward-looking capability** (static covariates from 12-month model → forecast conditioned on known future macro backdrop, not just extrapolating from history)
- **DAP still excluded** (no live feed — unchanged)

### Methodologies

1. **Multivariate Covariate Engineering**: Static numerical covariates = 12-month forecast ranges; dynamic numerical covariates = live macro indicators; categorical covariates = commodity category, exchange, seasonality flags.

2. **Cross-Asset Reference Bar Construction**: Define commodity clusters based on economic relationships:
   - **Energy cluster**: CL, B, HO, RB, VGT, JX, JQ
   - **Agricultural cluster**: ZC, ZW, FCPO, SB, CT, RR, SI
   - **Metals cluster**: HG, SI, GOLD, SILVER

3. **Shadow Mode Validation**: All 12 new commodity symbols run with `macro_regime_mode=shadow`. Multivariate forecast runs in shadow alongside univariate. Comparison metric: `abs(p_multivariate - p_univariate)` — large divergence flags where cross-asset signal matters most.

4. **Backtesting Protocol**: Use 6 months of historical price data. Compare univariate vs multivariate on MAE, pinball loss, and directional accuracy. Statistical test: Diebold-Mariano test for forecast accuracy comparison.

---

## Part 2: Implementation Requirements

### 2.1 Files to Create

| File | Purpose | Size |
|------|---------|------|
| `commodity_12m_forecast_model_2027.json` | Machine-readable forecast model (code-consumable) | ~5KB |
| `regime_platform/sweep_orchestrator/timesfm3_covariate_builder.py` | Constructs multivariate request payloads from forecast model + live prices | ~300 lines |
| `regime_platform/config/timesfm3_config.yaml` | TimesFM-3 feature flags, covariate mappings, cross-asset clusters | ~50 lines |

### 2.2 Files to Modify

| File | Modification | Lines Changed |
|------|-------------|---------------|
| `server.py` | Add `ForecastWithCovariatesRequest` Pydantic model; add `/forecast/covariates` endpoint; detect multi-series → call `forecast_with_covariates()` instead of `forecast()` | ~80 lines |
| `orchestrator.py::_fetch_timesfm_cached()` | Modified to construct multivariate request using `timesfm3_covariate_builder`; pass correlated series as inputs; pass forecast ranges as static covariates | ~60 lines |
| `orchestrator.py` `_PRICE_DECIMALS` | Add 12 commodity entries (FCPO, CT, SB, RR, ZC, ZW, B, CL, RB, HO, JX, SI, VGT) | ~15 lines |
| `regime_platform/core/feature_flags.py` | Add `timesfm3_mode` flag (`off` \| `shadow` \| `live`); add `timesfm3_covariate_clusters` config; add `timesfm3_xreg_mode` config | ~30 lines |
| `regime_platform/core/feature_flags.py` `_DEFAULTS` | Add `timesfm3_mode: "shadow"`, `timesfm3_xreg_mode: "xreg + timesfm"`, `timesfm3_max_series_per_request: 8` | ~10 lines |
| `config/feature_flags.yaml` | Mirror the Python defaults | ~15 lines |
| `timesfm_k_adaptive.py` | Add `map_forecast_to_probability` support for multivariate output (extract per-series p_timesfm from response) | ~20 lines |
| `server.py` `ForecastRequest` | Add optional covariate fields (`dynamic_numerical_covariates`, `static_numerical_covariates`, `xreg_mode`) | ~20 lines |

### 2.3 Key Code Insights from Investigation

**`server.py` is already architected for multivariate input**:
- Line 201-216: `ForecastRequest` already accepts `inputs: list[list[float]]` — a list of time series
- Line 252-275: `/forecast` endpoint calls `state["model"].forecast(horizon=req.horizon, inputs=inputs)` — this already processes multiple series
- **But**: The orchestrator's `_fetch_timesfm_cached()` constructs `request_body = {"horizon": 1, "inputs": [price_samples]}` — a **single-series** request

**`timesfm_k_adaptive.py` (66 lines)** has the `map_forecast_to_probability` function that converts raw forecast → p_timesfm via `sigmoid(k * rel_change)`. This mapping is **identical** for multivariate and univariate — the orchestrator just needs to extract the right per-symbol forecast from the multivariate response.

**`timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID)`** loads the model. The `forecast_with_covariates()` method exists on the class with the signature:
```python
forecast_with_covariates(
    self,
    inputs: list[Sequence[float]],
    dynamic_numerical_covariates: dict[str, Sequence[Sequence[float]]] | None = None,
    static_numerical_covariates: dict[str, Sequence[float]] | None = None,
    xreg_mode: str = "xreg + timesfm",
    ...
)
```

**Critical discovery**: The `server.py` model is loaded as `timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID)` at line 78. The `forecast_with_covariates()` method exists on this class. The `/forecast` endpoint currently calls `.forecast()` but `.forecast_with_covariates()` is available on the same model object.

### 2.4 Configuration Requirements

**Feature flags (`config/feature_flags.yaml` additions):**
```yaml
timesfm3_mode: "shadow"                    # off | shadow | live
timesfm3_xreg_mode: "xreg + timesfm"       # xreg + timesfm | timesfm + xreg
timesfm3_max_series_per_request: 8         # cap on inputs list length
timesfm3_covariate_clusters:               # cross-asset reference groups
  energy: [CL, B, HO, RB, VGT]
  agricultural: [ZC, ZW, SB, CT, RR, SI]
  metals: [HG, SI]
timesfm3_fallback_to_univariate: true      # if multivariate fails, retry univariate
timesfm3_shadow_compare_univariate: true   # run both, compare in logs
```

**Environment variables:**
```bash
TIMESFM_MODEL_ID="google/timesfm-2.5-200m-pytorch"  # Current — 3.0 when license allows
TIMESFM3_ENABLED="true"                              # Feature flag override
TIMESFM3_INFERENCE_URL="http://localhost:8000/forecast/covariates"  # New endpoint
```

### 2.5 `server.py` Changes

**Current `server.py` structure:**
- Line 55: `MODEL_ID = os.getenv("TIMESFM_MODEL_ID", "google/timesfm-2.5-200m-pytorch")`
- Line 78: `model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID)`
- Line 252: `@app.post("/forecast")` → calls `state["model"].forecast(horizon=req.horizon, inputs=inputs)`
- Line 258: `inputs = [np.array(s, dtype=np.float32) for s in req.inputs]`

**New `server.py` additions:**
```python
# NEW Pydantic model
class CovariateSeries(BaseModel):
    symbol: str
    series: list[float]
    dynamic_numerical: dict[str, list[float]] | None = None
    static_numerical: dict[str, float] | None = None

class ForecastWithCovariatesRequest(BaseModel):
    horizon: int = Field(..., ge=1, le=MAX_HORIZON)
    series: list[CovariateSeries]  # Multiple targets
    xreg_mode: str = "xreg + timesfm"
    normalize_xreg_target_per_input: bool = True
    ridge: float = 0.0

# NEW endpoint
@app.post("/forecast/covariates", response_model=ForecastResponse, dependencies=[Depends(verify_api_key)])
def forecast_with_covariates(req: ForecastWithCovariatesRequest):
    if state["model"] is None:
        raise HTTPException(status_code=503, detail="Model still loading")
    try:
        inputs = [np.array(s.series, dtype=np.float32) for s in req.series]
        dynamic_numerical = {
            s.symbol: np.array(v, dtype=np.float32) 
            for s in req.series if s.dynamic_numerical
        }
        static_numerical = {
            s.symbol: np.array(v, dtype=np.float32)
            for s in req.series if s.static_numerical
        }
        t0 = time.time()
        point, quantile = state["model"].forecast_with_covariates(
            horizon=req.horizon,
            inputs=inputs,
            dynamic_numerical_covariates=dynamic_numerical or None,
            static_numerical_covariates=static_numerical or None,
            xreg_mode=req.xreg_mode,
        )
        elapsed_ms = (time.time() - t0) * 1000
        return ForecastResponse(
            horizon=req.horizon,
            num_series=len(req.series),
            point_forecast=point.tolist(),
            quantile_forecast=quantile.tolist(),
            elapsed_ms=round(elapsed_ms, 2),
        )
    except Exception as e:
        log.exception("Forecast with covariates failed")
        raise HTTPException(status_code=500, detail=str(e))
```

### 2.6 `orchestrator.py::_fetch_timesfm_cached()` Changes

**Current flow (single-series):**
```python
request_body = {"horizon": 1, "inputs": [price_samples]}
```

**New flow (multivariate):**
```python
# Build multivariate request from commodity cluster
request_body = timesfm3_covariate_builder.build_multivariate_request(
    symbol="HO",
    prices=price_samples,
    cluster="energy",           # From feature_flags config
    forecast_model=forecast_model_2027,  # Loaded JSON
    horizon=1,
)
# request_body = {
#     "horizon": 1,
#     "series": [
#         {"symbol": "HO", "series": [...], "static_numerical": {"q4_2026_low": 160, "q4_2026_high": 190}},
#         {"symbol": "CL", "series": [...]},
#         {"symbol": "B", "series": [...]},
#         ...
#     ],
#     "xreg_mode": "xreg + timesfm"
# }
```

**Cache key change**: Cache key becomes per-cluster (not per-symbol) since one request covers multiple symbols. Cache TTL remains 900s (15 min).

**Fallback logic**: If `forecast_with_covariates()` fails, retry with univariate `forecast()` for the single symbol. This is the `timesfm3_fallback_to_univariate` safety net.

### 2.7 `timesfm3_covariate_builder.py` Design

```python
def build_multivariate_request(
    symbol: str,           # The target symbol
    prices: list[float],   # Target symbol's price history
    forecast_model: dict,  # commodity_12m_forecast_model_2027.json
    horizon: int = 1,
) -> dict:
    """Build the full multivariate request body."""
    cluster = get_cluster_for_symbol(symbol)
    cluster_symbols = get_cluster_symbols(cluster)
    
    series = []
    for s in cluster_symbols:
        s_prices = get_price_history(s)  # From existing fetch_tradingview_batch
        static_cov = get_forecast_range(s, forecast_model)
        series.append({
            "symbol": s,
            "series": s_prices,
            "static_numerical": static_cov,
        })
    
    return {
        "horizon": horizon,
        "series": series,
        "xreg_mode": get_timesfm3_xreg_mode(),
    }

def get_cluster_for_symbol(symbol: str) -> str:
    """Map symbol to its commodity cluster."""
    # energy: [CL, B, HO, RB, VGT]
    # agricultural: [ZC, ZW, SB, CT, RR, SI]
    # metals: [HG, SI]
    ...

def get_forecast_range(symbol: str, forecast_model: dict) -> dict:
    """Extract q4_2026_low/high from forecast model."""
    ...

def get_price_history(symbol: str) -> list[float]:
    """Fetch price history from TradingView batch API."""
    # Reuse existing fetch_tradingview_batch logic
    ...
```

### 2.8 `_PRICE_DECIMALS` Additions

```python
_PRICE_DECIMALS.update({
    # ── Commodities (12m forecast model 2027) ──
    "FCPO": 2,   # Crude Palm Oil (Bursa Malaysia)
    "CT": 2,     # Cotton #2 (ICE)
    "SB": 2,     # Sugar #11 (ICE)
    "RR": 2,     # Rough Rice (ICE)
    "ZC": 2,     # Corn (CBOT)
    "ZW": 2,     # Wheat (CBOT)
    "B": 3,      # Brent Crude (NYMEX/ICE)
    "CL": 2,     # Light Sweet Crude (NYMEX)
    "RB": 3,     # RBOB Gasoline (NYMEX)
    "HO": 3,     # Heating Oil (NYMEX)
    "JX": 3,     # Jet Fuel (NYMEX)
    "SI": 3,     # Silver (NYMEX/COMEX)
    "VGT": 3,    # European Natural Gas (TTF/EEX)
})
```

### 2.9 `feature_flags.py` Additions

Add after `get_timesfm_k_ceiling()` (line 86) and before `trend` section:

```python
# ── TimesFM-3 Multivariate Configuration ──────────────────────

def get_timesfm3_mode() -> str:
    """``off`` | ``shadow`` | ``live`` — multivariate TimesFM-3
    forecasting mode. Shadow runs multivariate alongside univariate
    and logs divergence. Live replaces univariate for signal
    qualification."""
    return str(_load().get(
        "timesfm3_mode", _DEFAULTS["timesfm3_mode"],
    )).lower()

def get_timesfm3_xreg_mode() -> str:
    """``xreg + timesfm`` | ``timesfm + xreg`` — how covariates
    interact with the TimesFM model."""
    return str(_load().get(
        "timesfm3_xreg_mode", _DEFAULTS["timesfm3_xreg_mode"],
    )).lower()

def get_timesfm3_max_series_per_request() -> int:
    """Maximum number of series in a single multivariate forecast
    request."""
    return int(_load().get(
        "timesfm3_max_series_per_request", _DEFAULTS["timesfm3_max_series_per_request"],
    ))

def get_timesfm3_fallback_to_univariate() -> bool:
    """If multivariate forecast fails, fall back to univariate."""
    return bool(_load().get(
        "timesfm3_fallback_to_univariate", _DEFAULTS["timesfm3_fallback_to_univariate"],
    ))
```

Add to `_DEFAULTS`:
```python
"timesfm3_mode": "shadow",
"timesfm3_xreg_mode": "xreg + timesfm",
"timesfm3_max_series_per_request": 8,
"timesfm3_fallback_to_univariate": True,
```

### 2.10 Shadow Mode Validation Protocol

**Phase 1 (Weeks 1-2)**: Shadow mode only
- `timesfm3_mode = "shadow"` in `feature_flags.yaml`
- Multivariate forecast runs alongside univariate
- Both results logged with divergence metric: `abs(p_multivariate - p_univariate)`
- **No signal qualification changes** — univariate p_timesfm still drives the gates
- After 2 weeks: validate that multivariate p_timesfm has higher directional accuracy on a holdout set

**Phase 2 (Week 3-4)**: Shadow mode with gate comparison
- `timesfm3_mode = "shadow"` still, but now compare which signal (univariate vs multivariate) would have produced a QUALIFIED signal
- Track: false positive rate, false negative rate, hit rate
- If multivariate shows ≥5% improvement in hit rate with no increase in false positives → proceed to Phase 3

**Phase 3 (Week 5+)**: Live mode (if validated)
- `timesfm3_mode = "live"`
- Multivariate p_timesfm replaces univariate for signal qualification
- Continue logging univariate as reference
- Monitor for 1 week before removing shadow fallback

### 2.11 Supabase `nt_symbol` Registration

**12 symbols to register** (shadow mode):

| symbol | tradingview_symbol | commodity | exchange | asset_class | macro_regime_mode | trend_regime_mode |
|--------|-------------------|-----------|----------|-------------|-------------------|-------------------|
| FCPO | TVC:FCPO | Crude Palm Oil | Bursa Malaysia | agricultural | shadow | shadow |
| CT | TVC:CT | Cotton #2 | ICE | agricultural | shadow | shadow |
| SB | TVC:S11 | Sugar #11 | ICE | agricultural | shadow | shadow |
| RR | TVC:RR | Rough Rice | ICE | agricultural | shadow | shadow |
| ZC | TVC:ZC | Corn | CBOT | agricultural | shadow | shadow |
| ZW | TVC:ZW | Wheat | CBOT | agricultural | shadow | shadow |
| B | TVC:B | Brent Crude | NYMEX/ICE | energy | shadow | shadow |
| CL | TVC:CL | Light Sweet Crude | NYMEX | energy | shadow | shadow |
| RB | TVC:RB | RBOB Gasoline | NYMEX | energy | shadow | shadow |
| HO | TVC:HO | Heating Oil | NYMEX | energy | shadow | shadow |
| JX | TVC:JX | Jet Fuel | NYMEX | energy | shadow | shadow |
| SI | TVC:SI | Silver | NYMEX/COMEX | metals | shadow | shadow |
| VGT | TVC:VGT | European Natural Gas | TTF/EEX | energy | shadow | shadow |

**DAP excluded**: No live trading feed available (OTC/spot only, manual/scraped).

### 2.12 Additional Requirements

| # | Requirement | Priority | Status |
|---|------------|----------|--------|
| 1 | `server.py` — Add `/forecast/covariates` endpoint | **Critical** | Not started |
| 2 | `server.py` — Extend `ForecastRequest` with covariate fields | **Critical** | Not started |
| 3 | `orchestrator.py` — Modify `_fetch_timesfm_cached()` for multivariate | **Critical** | Not started |
| 4 | `timesfm3_covariate_builder.py` — Create | **Critical** | Not started |
| 5 | `commodity_12m_forecast_model_2027.json` — Create machine-readable JSON | **High** | Not started |
| 6 | `feature_flags.py` — Add `timesfm3_mode` config | **High** | Not started |
| 7 | `_PRICE_DECIMALS` — Add 12 commodity entries | **High** | Not started |
| 8 | `timesfm_k_adaptive.py` — Handle multivariate response | **High** | Not started |
| 9 | `config/feature_flags.yaml` — Mirror defaults | **High** | Not started |
| 10 | Shadow mode validation protocol | **High** | Not started |
| 11 | Backtesting protocol (Diebold-Mariano) | **Medium** | Not started |
| 12 | Cross-asset cluster definition | **Medium** | Not started |
| 13 | Supabase migration for `nt_symbol` entries | **High** | Not started |
| 14 | TradingView symbol validation (FCPO, VGT, JX, etc.) | **High** | Not started |
| 15 | Tests for `/forecast/covariates` endpoint | **High** | Not started |
| 16 | Tests for `timesfm3_covariate_builder` | **High** | Not started |
| 17 | Performance benchmark (multivariate vs univariate latency) | **Medium** | Not started |
| 18 | Error handling for partial covariate failures | **Medium** | Not started |
| 19 | Monitoring/logging for covariate divergence | **Medium** | Not started |
| 20 | Update `commodity_macro_context_scope.md` with superseding scope | **Medium** | Not started |

---

## Part 3: TimesFM-3 Production Path

**When** the `timesfm-non-commercial-license-v1.0` is updated or a commercial license is obtained:

1. Change `server.py` line 55: `MODEL_ID = os.getenv("TIMESFM_MODEL_ID", "google/timesfm-2.5-200m-pytorch")` → `"google/timesfm-3.0-pytorch"`
2. Restart `server.py` — the model loads automatically (line 78: `timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID)` — note this class name needs to change to `TimesFM_3_330M_torch` or whatever the 3.0 class is)
3. The `/forecast/covariates` endpoint already handles multivariate inputs — no code change needed
4. The existing covariate architecture means the system is **already built for TimesFM-3** — just a config change

**Key insight**: The architecture is forward-compatible. Building the multivariate covariate path now means the switch to TimesFM-3 is a one-line env var change, not a rewrite.

---

## Part 4: Risks & Mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| TimesFM-3 weights remain non-commercial | No production multivariate | Medium | Shadow mode still validates; architecture is ready |
| `forecast_with_covariates()` doesn't work with 2.5 weights | Feature broken | Medium | `xreg_mode` handles covariates on univariate model; fallback to univariate |
| Multivariate request fails due to series length mismatch | Bad forecasts | Medium | `timesfm3_max_series_per_request` cap; validate each series length |
| Cross-asset cluster definition is wrong | Poor signal quality | Medium | Validate with shadow comparison; iterate on cluster definition |
| Performance degradation (more API calls per symbol) | Sweep latency increase | High | Cache at cluster level; limit max series per request |
| `server.py` endpoint complexity increases | Bug surface area | Medium | Add tests; keep univariate endpoint unchanged |
| TradingView data gaps for new symbols | Incomplete forecasts | Medium | Validate symbol availability before registration; shadow mode catches gaps |
| Class name changes for TimesFM-3 (`TimesFM_2p5_200M_torch` → `TimesFM_3_330M_torch`) | Model loading breaks | Low | Only affects production switch; shadow mode uses current class |

---

## Part 5: Verification Plan

### Unit Tests (new)
- `test_timesfm3_covariate_builder.py`: Test `build_multivariate_request()` with known inputs → expected payload structure
- `test_server_covariates_endpoint.py`: Test `POST /forecast/covariates` with mock data → expected response shape
- `test_feature_flags_timesfm3.py`: Test `get_timesfm3_mode()` returns correct default/overrides

### Integration Tests
- Run `_fetch_timesfm_cached()` in shadow mode against a live `server.py` instance
- Verify multivariate response contains per-series point forecasts
- Verify divergence logging works (shadow vs univariate)
- Verify fallback to univariate when multivariate fails

### Validation Tests
- 2-week shadow run comparing multivariate vs univariate hit rates
- Diebold-Mariano test on historical holdout (Jan–Jun 2026)
- Performance benchmark: multivariate latency vs univariate latency

### Acceptance Criteria
- All 12 new symbols registered in `nt_symbol` with shadow mode
- `/forecast/covariates` endpoint returns valid per-series forecasts
- `_fetch_timesfm_cached()` constructs multivariate requests correctly
- Shadow mode logs divergence metrics
- Fallback to univariate works when multivariate fails
- Full pytest suite passes

---

## Part 6: Implementation Sequence

| Phase | Task | Duration | Dependencies |
|-------|------|----------|-------------|
| 1 | Create `commodity_12m_forecast_model_2027.json` | 1 day | Forecast model markdown |
| 2 | Create `timesfm3_covariate_builder.py` | 2 days | Phase 1 |
| 3 | Modify `server.py` (add `/forecast/covariates`) | 2 days | None |
| 4 | Modify `_fetch_timesfm_cached()` in orchestrator | 2 days | Phases 2, 3 |
| 5 | Add `feature_flags.py` config + `config/feature_flags.yaml` | 1 day | None |
| 6 | Add `_PRICE_DECIMALS` entries | 0.5 day | None |
| 7 | Modify `timesfm_k_adaptive.py` | 1 day | Phase 4 |
| 8 | Create Supabase migration for `nt_symbol` | 1 day | None |
| 9 | Write tests | 2 days | Phases 2-8 |
| 10 | Shadow mode validation (2 weeks) | 14 days | Phases 1-9 |
| 11 | Backtesting + Diebold-Mariano | 2 days | Phase 10 |
| 12 | Phase 3 (live mode) if validated | 1 day | Phase 10-11 |

**Total estimated duration**: 5-6 weeks (including shadow validation)

---

## Part 7: Key Code References

### `server.py` (current state)
- **Line 55**: `MODEL_ID = os.getenv("TIMESFM_MODEL_ID", "google/timesfm-2.5-200m-pytorch")`
- **Line 78**: `model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(MODEL_ID)` — loads model
- **Line 201-216**: `ForecastRequest` Pydantic model — `inputs: list[list[float]]`
- **Line 252-275**: `/forecast` endpoint — calls `state["model"].forecast(horizon=req.horizon, inputs=inputs)`
- **Line 258**: `inputs = [np.array(s, dtype=np.float32) for s in req.inputs]` — single-series conversion

### `orchestrator.py::_fetch_timesfm_cached()` (current state, lines 272-504)
- **Line 370-373**: `request_body = {"horizon": 1, "inputs": [price_samples]}` — single-series request
- **Line 354-356**: `TIMESFM_API_KEY = os.getenv("TIMESFM_API_KEYS", "").split(",")[0].strip()`
- **Line 392-410**: HTTP POST to TimesFM server with `request_body`
- **Line 463-496**: Parse `point_forecast[0][0]` → `rel_change` → `sigmoid(k * rel_change)` → `p_timesfm`
- **Lines 489-497**: `map_forecast_to_probability(rel_change, _k, TIMESFM_CLAMP_MIN, TIMESFM_CLAMP_MAX)` — unchanged for multivariate

### `timesfm_k_adaptive.py` (66 lines)
- **`map_forecast_to_probability(rel_change, k, clamp_min, clamp_max)`**: `sigmoid(k * rel_change)` clamped — **unchanged**, works for any forecast input
- **`compute_adaptive_timesfm_k(base_k, trend_strength, floor_k, ceiling_k)`**: Adjusts k based on trend strength — **unchanged**

### `regime_platform/core/feature_flags.py`
- **`_DEFAULTS` dict**: Lines 33-98 — add `timesfm3_mode`, `timesfm3_xreg_mode`, etc.
- **`get_timesfm_gate_mode()`**: Line 454 — existing pattern to follow for new feature flags
- **`_load()`**: Lines 104-132 — reads `config/feature_flags.yaml`, hot-reloads on mtime change

### `config/feature_flags.yaml`
- Must mirror `_DEFAULTS` in Python — same structure, YAML format

---

*End of implementation scope.*
