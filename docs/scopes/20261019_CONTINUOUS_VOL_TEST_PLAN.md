# Continuous Vol Backtest — Test Plan & Framework Design

> **Status**: design complete, no code changed · **Date**: 2026-10-19 · **Base**: `vol_backtest/` @ `b509865`/`4fe5203`

---

## 0. Problem statement

Phase 0 built the RV-forecast benchmark framework and ran it once on yfinance
proxy data. Three things are wrong with that for continuous use:

1. **yfinance is not the repo's data source.** The live orchestrator uses
   TDVA (`fetch_tradingview_batch` in `price_fetcher.py`). A benchmark that
   measures a different price feed than production is measuring a different
   quantity — the HAR bar must be set on the same data the live path sees.
2. **One-off runs don't catch regressions.** A model arm that was worse than
   HAR last month but better this month (or vice versa) needs a tracked time
   series, not a snapshot.
3. **No CI integration.** The 26-test suite validates methodology but says
   nothing about whether a code change broke the harness's no-lookahead
   contract or DM/Holm math.

This plan replaces yfinance with TDVA, establishes a tracked baseline, and
defines the continuous test cadence.

---

## 1. Data source: replace yfinance with TDVA

### 1.1 What changes

| Layer | Today (yfinance) | Target (TDVA) |
|---|---|---|
| Loader | `vol_backtest/data.py:load_yfinance()` | `vol_backtest/data.py:load_tdva()` |
| HTTP | `yfinance.download()` (Yahoo) | `price_fetcher.fetch_tradingview_batch()` (RapidAPI) |
| Symbol map | `YAHOO_MAP` (proxy tickers: GC=F, SI=F, JPY=X…) | `nt_symbol.tradingview_symbol` (authoritative: OANDA:XAUUSD…) |
| Timestamps | `df.index` (exchange-local) | `include_timestamps=True` (UTC epoch) |
| Auth | None | `TRADINGVIEW_API_KEY` + `TRADINGVIEW_API_HOST` |

### 1.2 Symbol mapping (authoritative, from `nt_symbol`)

| Internal | TDVA symbol | Asset class |
|---|---|---|
| `BTCUSD` | `COINBASE:BTCUSD` | crypto (24/7) |
| `XAUUSD` | `OANDA:XAUUSD` | metal (~120 h/wk) |
| `XAGUSD` | `OANDA:XAGUSD` | metal (~120 h/wk) |
| `USDJPY` | `OANDA:USDJPY` | forex (~120 h/wk) |
| `USDCHF` | `OANDA:USDCHF` | forex (~120 h/wk) |

The 5 thesis symbols are unchanged. The difference is **data provenance**:
TDVA is the same feed the orchestrator's heavy sweep uses, so the benchmark
measures the same prices the live path would act on.

### 1.3 Implementation sketch

```python
# vol_backtest/data.py — new loader (sibling of load_yfinance)

async def load_tdva(
    symbols: list[str],
    hours: int = 2160,
    interval: str = "1h",
) -> list[SymbolSeries]:
    """Fetch hourly bars from TDVA (the repo's sole production price source).

    Uses regime_platform.sweep_orchestrator.price_fetcher.fetch_tradingview_batch
    with include_timestamps=True. Symbol configs are read from nt_symbol
    via Supabase (symbol → tradingview_symbol mapping).
    """
    from regime_platform.core.settings import settings
    from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch

    # Build symbol configs from the live roster
    configs = _fetch_symbol_configs(symbols)  # → [{"symbol": ..., "tradingview_symbol": ...}]

    prices_dict, ts_dict = await fetch_tradingview_batch(
        configs, hours=hours, interval=interval, include_timestamps=True,
    )

    out = []
    for sym in symbols:
        if sym not in prices_dict:
            continue
        closes = prices_dict[sym]
        ts = ts_dict.get(sym) if ts_dict else None
        out.append(SymbolSeries(symbol=sym, prices=np.array(closes), timestamps=ts))
    return out
```

### 1.4 Fallback chain

The runner's `--data-dir` (CSV) path stays as the offline fallback. The
priority becomes: `--data-dir` (explicit) → `--tdva` (production) →
`--synthetic` (CI/smoke). `--yfinance` is removed.

---

## 2. Baseline establishment

### 2.1 The measured bar (from Phase 0, run `20261019_010230`)

| Metric | Value | Source |
|---|---|---|
| HAR beats naive (DM/Holm significant) | 16/20 cells | `20261019_005157/summary.md` |
| har_ewma beats naive (DM/Holm significant) | 18/20 cells | same |
| har_ewma best QLIKE | 16/20 cells | same |
| HAR coverage80 (acceptance [0.80, 0.95]) | 0.75–0.82 | `20261019_010230/results.csv` |
| Chronos-2 coverage80 | 0.19–0.42 | same |
| HAR QLIKE range | 0.039–0.120 | same |

**The bar is HAR.** Any model arm must beat Log-HAR per-symbol on QLIKE at
DM/Holm significance. The `har_ewma` blend is the fallback (Brini 2026:
"simple average matches the best single model").

### 2.2 Baselines to track continuously

| Baseline | Definition | Why |
|---|---|---|
| `naive` | last-24h RV | The persistence null — every arm must beat this |
| `har` | Log-HAR (Corsi) on hourly RV | **The reference bar** |
| `har_ewma` | equal-weight variance blend | Best scorer in 16/20 cells; the production fallback |
| `ewma` | RiskMetrics EWMA (λ=0.94) | The simplest non-trivial forecaster |

Model arms (`timesfm25`, `chronos2`, `chronos2_cov`) are tracked alongside
but must clear the HAR bar before any integration discussion.

### 2.3 Tracked metrics per run

Per (symbol, horizon, arm):
- QLIKE (primary)
- MAE/RMSE on log(RV)
- Directional accuracy vs persistence null
- 80% interval coverage
- DM vs HAR (p_holm, significant_holm)
- DM vs naive (p_holm, significant_holm)
- Arm runtime (calls, successes, seconds)

---

## 3. Continuous test cadence

### 3.1 Test tiers

| Tier | Trigger | What runs | Output |
|---|---|---|---|
| **CI** (methodology) | Every PR touching `vol_backtest/` | 26 existing tests + `--synthetic --no-models --cutoffs 10` | pytest pass/fail |
| **Regression** (baseline drift) | Daily (after heavy sweep, ~18:00 UTC) | `--tdva --horizons 24,48 --cutoffs 50 --arms naive,ewma,har,har_ewma` | `results.csv`, `dm_tests.csv`, `summary.md` in `worklog/kairos/reports/vol_backtest/<runid>/` |
| **Decision gate** | Weekly (or on model-arm code change) | `--tdva --horizons 6,24,48,72 --cutoffs full --arms all` | Full run + verdict |

### 3.2 CI tier (existing tests, no changes needed)

The 26-test suite already covers:
- Hand-computed RV arithmetic
- Calendar filter (FX weekends vs crypto)
- QLIKE identity + asymmetry
- Directional accuracy + persistence null
- Newey-West HAC closed form
- DM significance + Holm ordering
- Baseline arms produce finite positive forecasts
- HAR beats naive on weekly-cyclic vol (implementation sanity)
- No-lookahead sentinel
- TimesFM 9/10-level quantile layout extraction
- Runner end-to-end subprocess smoke
- Chronos gapped-calendar regression (explicit freq)

**No new tests needed for CI.** The suite is methodology-only (no models,
no network) and runs in ~12s.

### 3.3 Regression tier (daily baseline)

```
python scripts/run_vol_backtest.py \
  --tdva \
  --symbols BTCUSD,XAUUSD,XAGUSD,USDJPY,USDCHF \
  --horizons 24,48 \
  --cutoffs 50 \
  --arms naive,ewma,har,har_ewma \
  --out worklog/kairos/reports/vol_backtest/daily_YYYYMMDD/
```

This runs in seconds (baselines only, no model arms), uses TDVA's production
price feed, and produces the tracked metrics. The output directory uses a
date-stamped name (not timestamp) so the latest run is always at a predictable
path.

**Acceptance criteria for "no regression":**
- HAR beats naive DM-significant in ≥ 14/10 cells (≥ 70% of cells)
- har_ewma best QLIKE in ≥ 8/10 cells (≥ 50%)
- HAR coverage80 ∈ [0.70, 0.95] (widened slightly from [0.80, 0.95] for
  real-data noise; synthetic stays [0.80, 0.95])
- No arm has NaN QLIKE on > 20% of cells

### 3.4 Decision gate tier (weekly / on model change)

```
python scripts/run_vol_backtest.py \
  --tdva \
  --symbols BTCUSD,XAUUSD,XAGUSD,USDJPY,USDCHF \
  --horizons 6,24,48,72 \
  --arms naive,ewma,har,har_ewma,timesfm25,chronos2,chronos2_cov \
  --out worklog/kairos/reports/vol_backtest/weekly_YYYYMMDD/
```

This is the full run with all arms. The decision gate is unchanged from
Phase 0: any model arm beating HAR per-symbol on QLIKE at DM/Holm
significance proceeds to fine-tuning; if none does, the `har_ewma` fallback
is the measured answer.

### 3.5 Scheduler integration

The regression tier hooks into the existing APScheduler sweep orchestrator
as a daily job (like `atr_reopt_urgency` every 4h). The decision-gate tier
is a manual trigger or a weekly cron. Neither is wired yet — that is a Phase
3/4 integration task, gated on the baseline data accumulating first.

---

## 4. What this plan deliberately does NOT do

- **No live-path integration.** `vol_backtest/` stays outside `regime_platform/`.
  Nothing in the orchestrator imports it. The benchmark informs decisions;
  it does not make them.
- **No fine-tuning.** The decision gate measures zero-shot performance.
  Fine-tuning (thesis Phase 2) is gated on the gate clearing.
- **No event-odds covariate arms yet.** The macro event-odds layer
  (`docs/scopes/20261019_MACRO_EVENT_ODDS_LAYER_ASSESSMENT.md`) proposes
  `is_event_day` HAR dummies and `hours_to_event` Chronos-2 covariates.
  Those are build-order steps 1–2 of that assessment and depend on the
  `event_odds.py` poller existing. This plan is about the **baseline
  infrastructure** — the event covariates slot into the same harness.
- **No new database tables for vol results.** The framework writes CSV/MD
  files under `worklog/kairos/reports/vol_backtest/`. That is sufficient for
  research. If continuous testing reveals a need for SQL-persisted results
  (e.g., a dashboard view), that is a separate migration.

---

## 5. Files / lineage

- Modified: `vol_backtest/data.py` (add `load_tdva()`, remove `load_yfinance()`)
- Modified: `scripts/run_vol_backtest.py` (replace `--yfinance` with `--tdva`,
  add `--out-dir` date-stamped convenience)
- Unchanged: `vol_backtest/` all other modules, `tests/test_vol_backtest.py`
- Design doc: `docs/scopes/20261019_CONTINUOUS_VOL_TEST_PLAN.md` (this file)
- Prerequisite: `supabase/migrations/212_nt_event_odds.sql`,
  `213_nt_event_digest.sql`, `214_signal_sim_event_odds_columns.sql`
  (executed 2026-10-19, for the event-odds layer that slots into this
  harness later)

---

## 6. Honest boundaries

- **TDVA data quality is not verified.** The first TDVA run will be the
  first time the benchmark sees production prices. Expect surprises:
  different bar counts, timestamp conventions, occasional gaps.
- **The HAR bar was set on yfinance proxy data.** The first TDVA run
  re-measures the bar. If HAR's QLIKE numbers shift, that is a data-provenance
  correction, not a regression.
- **Chronos-2's coverage problem is unresolved.** Coverage 0.19–0.42 means
  the model's native quantile spreads are too narrow. Recalibration
  (Mincer-Zarnowitz on log-RV) is mandatory before any Chronos RV is usable.
  This plan tracks the problem; it does not solve it.
- **~700 cutoffs at 5 symbols × 4 horizons = 14,000 forecasts per full run.**
  Baselines only: seconds. With Chronos-2: ~450s (from the 453s wall in
  run `20261019_010230`). With TimesFM: additional ~60s. Plan the
  decision-gate run accordingly.
