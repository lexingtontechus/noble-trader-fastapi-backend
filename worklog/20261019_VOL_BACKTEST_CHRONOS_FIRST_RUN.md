# Vol backtest — Chronos-2 arms: gapped-calendar fix + first zero-shot run

**Date:** 2026-10-19
**Type:** fix (research tooling) + first measured zero-shot run
**Base:** HEAD `d205d42` · **Plan:** Phase 0 of `.hermes/plans/2026-10-18_120000-timesfm-refined-thesis.md`
**Status:** `vol_backtest/` only. No live-path code touched.

---

## 1. Defect: the chronos arms silently died on gapped calendars

**Symptom.** First real-data run (yfinance proxies, run `20261019_005731`):
`chronos2` / `chronos2_cov` produced **n=0 records on every FX/metals symbol**
(USDCHF, USDJPY, XAGUSD, XAUUSD) and worked only on BTCUSD — the one 24/7
calendar.

**Root cause.** `Chronos2Pipeline.predict_df` builds the future frame via
`make_future_df` **even when `future_df` is passed** (it validates the future
grid). With `freq=None` it calls `pd.infer_freq` on the context, which returns
`None` for weekend-gapped FX/metals calendars → `ValueError: Could not infer
frequency` → the arm's `except Exception: return None` swallowed it. Every
forecast for a non-24/7 symbol died before reaching the model.

**Fix** (`vol_backtest/model_arms.py`):
- `_ChronosBase._predict` now passes **`freq="h"`** explicitly — Phase 0 data
  is hourly bars by construction, and bars are treated as consecutive steps,
  the same univariate convention as the `timesfm25` arm.
- `Chronos2Covariate` builds its future grid on a fixed 1h step (was
  `ts[1] - ts[0]`, which breaks if the window's first two bars straddle a gap).

**Regression tests** (`tests/test_vol_backtest.py`): 2 added via a stub
pipeline that captures `predict_df` kwargs — assert `freq == "h"`, an hourly
future grid, and a finite RV on weekday-only calendars. **26/26 pass.**

---

## 2. First zero-shot results — real proxy data, 60 cutoffs, H ∈ {24, 48}

Run `20261019_010230`: 5 symbols × 60 cutoffs × {har, chronos2, chronos2_cov},
453 s wall on CPU (≈0.38 s/forecast at context 1024; `torch 2.14.1+cpu`,
`chronos-forecasting` 2.x, `amazon/chronos-2`, 24 s model load).

**QLIKE (lower is better):**

| symbol | H | chronos2 | chronos2_cov | har | winner |
|---|---:|---:|---:|---:|---|
| BTCUSD | 24 | 0.0831 | **0.0811** | 0.0984 | chronos2_cov |
| BTCUSD | 48 | 0.0999 | 0.1047 | **0.0606** | har |
| USDCHF | 24 | 0.0627 | **0.0608** | 0.0616 | chronos2_cov |
| USDCHF | 48 | 0.0527 | 0.0546 | **0.0394** | har |
| USDJPY | 24 | 0.1709 | 0.1608 | **0.1201** | har |
| USDJPY | 48 | 0.1241 | 0.1164 | **0.0833** | har |
| XAGUSD | 24 | 0.1135 | 0.1041 | **0.0782** | har |
| XAGUSD | 48 | 0.0688 | 0.0687 | **0.0589** | har |
| XAUUSD | 24 | 0.1262 | 0.1464 | **0.1080** | har |
| XAUUSD | 48 | **0.0835** | 0.1085 | 0.0871 | chronos2 |

- **HAR wins 8/10 cells.** The two chronos2_cov wins (BTCUSD/USDCHF H=24) are
  small and **not** DM-significant.
- `chronos2` is Holm-significantly **worse** than HAR at USDJPY H=24
  (p_holm 0.032) and XAGUSD H=24 (p_holm 0.027).
- **Calibration is the loudest signal:** chronos coverage80 = **0.19–0.42**
  against the [0.80, 0.95] acceptance band — the native quantile spreads are
  far too narrow for RV extraction on hourly returns. (HAR: 0.75–0.78,
  borderline low.) Mincer-Zarnowitz recalibration (thesis Phase 1) is
  mandatory before any TSFM RV is usable.
- Directional accuracy: every arm beats the persistence null (0.28–0.42) by a
  wide margin (0.56–0.83) — direction is not the differentiator; QLIKE and
  calibration are.

Companion baseline run `20261019_005157` (naive/ewma/har/har_ewma, full ~700
cutoffs/symbol, 28.7 s): **HAR beats naive DM-significant in 16/20 cells**
(lower loss in 20/20); `har_ewma` takes best QLIKE in 16/20 cells. The
Log-HAR bar is real on this data.

---

## 3. Interpretation — scoped honestly

Consistent with Brini 2026 (arXiv 2607.05291: TSFMs do not beat Log-HAR
zero-shot) and Rahimikia et al. 2026 (arXiv 2511.18578: zero-shot finance
performance poor). This is **not** a Chronos-2 indictment: 60 cutoffs, proxy
tickers (GC=F / SI=F / JPY=X / CHF=X / BTC-USD), no recalibration, and a
bars-as-consecutive-steps calendar convention. The decision-gate run —
operator's broker-quality CSV exports, full cutoffs, and the `timesfm25` arm
against the live :8002 server — remains the arbiter. This note is the first
measured instalment, and it was only possible after the calendar fix: before
it, the FX/metals half of the benchmark silently produced nothing.

---

## 4. Next steps

1. **Operator decision run:** `--data-dir data/vol_backtest` with 1y hourly
   CSVs for the 5 thesis symbols, full cutoffs, all arms, `server.py` up for
   `timesfm25`.
2. **Recalibration layer** (Mincer-Zarnowitz on log-RV) on top of the chronos
   quantiles before re-judging coverage.
3. Optional calendar-honesty upgrade: reindex the context to a regular grid
   with NaN at closure hours (Chronos-2 handles missing values natively) as
   an arm variant, rather than bars-as-steps.
