# Is the reachability cell's failure non-stationarity?

Generated 2026-10-02 02:52 UTC · lookback 90d · 4-fold expanding walk-forward · light sweep, resolved rows only

`cell_conditioning.py` measured the cell at in-sample AUC 0.7256 but out-of-fold 0.4994 — real signal that does not transfer. This separates the two explanations: **(a) non-stationarity** (the rate moves, so fit it on a trailing window) vs **(b) overfitting** (no signal; the in-sample number is memorised noise).

## Test 1 — does recency recover transfer?

**RECENCY DOES NOT FIX IT — best trailing window 14d reaches only AUC 0.5093 (expanding 0.5003); no window clears the 0.55 bar**

| training lookback | out-of-fold AUC | Brier | BSS | mean train n | folds > 0.5 |
|---|---:|---:|---:|---:|---:|
| 7d | 0.4905 | 0.2651 | -0.2109 | 7945.2 | 2/4 |
| 14d | 0.5093 | 0.2524 | -0.1530 | 16579.5 | 3/4 |
| 30d | 0.5003 | 0.2529 | -0.1548 | 26082.2 | 1/4 |
| 60d | 0.5003 | 0.2529 | -0.1550 | 26246.0 | 1/4 |
| all history (baseline) | 0.5003 | 0.2529 | -0.1550 | 26246.0 | 1/4 |

Same folds, same shrinkage, same population — only the training lookback changes. So a difference between rows is attributable to recency alone.

## Test 2 — is the rate actually unstable?

**UNSTABLE — a cell's rate moves 0.200 on average between eras (bar 0.1), split-half corr 0.1636. The rate is not a fixed property; any all-history estimate averages over regimes that no longer apply.**

| metric | value | meaning |
|---|---:|---|
| cells scored | 166 | present in ≥1 era with adequate n |
| mean abs drift | 0.1996 | avg \|rate(e+1) − rate(e)\| |
| mean range | 0.3461 | avg (max − min) across eras |
| sign-flip share | 0.2410 | consecutive pairs crossing 0.5 |
| split-half corr | 0.1636 | first-half vs second-half rate |
| consecutive pairs | 166 | denominator for drift/flips |

A stable rate that still fails to transfer points at overfitting from a different direction; a rate that swings is direct evidence of non-stationarity. Test 1 says whether the fix works; Test 2 says whether the diagnosis is right.

## Era-by-era cell rates

6 equal-*time* chunks of the history. Cells below `min_n=20` in an era are omitted from that era.

| cell | era1 | era2 | era3 | era4 | era5 | era6 |
|---|---:|---:|---:|---:|---:|---:|
| AAPL · low_vol · 4+ | 0.00 | 0.06 | 0.03 | 0.63 | 0.35 | 0.12 |
| XAUAUD · high_vol · 2-4 | 0.00 | — | — | — | — | — |
| XAGUSD · high_vol · 1-2 | 1.00 | — | — | — | — | 0.48 |
| BTCUSD · med_low_vol · 4+ | 0.49 | 0.37 | 0.52 | — | 0.21 | 0.00 |
| AAPL · med_low_vol · 4+ | — | — | — | — | 0.02 | 0.18 |
| XAGUSD · med_low_vol · 1-2 | — | — | — | 0.53 | — | 0.32 |
| XAUUSD · med_low_vol · 1-2 | — | — | — | — | — | 0.31 |
| XAUAUD · med_low_vol · 0.5-1 | — | — | — | 0.55 | — | 0.30 |
| US500 · med_high_vol · 1-2 | — | — | — | 0.57 | — | 0.11 |
| XAUEUR · high_vol · 4+ | 0.12 | 0.25 | 0.05 | 0.46 | 0.26 | 0.01 |
| XAUAUD · med_high_vol · 4+ | 0.25 | 0.27 | 0.30 | — | 0.09 | — |
| XAUAUD · low_vol · 4+ | 0.38 | 0.29 | 0.45 | 0.14 | 0.23 | — |

_Twelve largest cells by median era count. A row that stays flat is stable; a row that walks is the non-stationarity being tested._

## Coverage

- fetched: **41824** resolved light signals
- usable: **38177** (91.3%)
- dropped: 0 no timestamp, 3647 no ATR, 0 unbucketable
- span: **36 days** (2026-08-26 → 2026-10-02)

## Caveats

- ⚠ **The available history is only 36 days**, not the 90d requested — `nt_signal_sim` does not reach further back for resolved light rows. Every window ≥ the span is therefore identical to 'all history' (see the train_n column), and each era is only ~6 days. **This bounds the test**: a trailing window cannot be meaningfully shorter than an era without fitting on a single regime sample.

- **Resolved rows only** (`tp_hit`/`sl_hit`). Expired has no TP/SL verdict and is excluded — a selected sub-population, and the biggest remaining threat to this measurement (audit C2/R6).
- **A short window trades bias for variance.** A 7d window may be *more* current but fits cell rates on very few rows, so a low AUC there is not evidence against recency — check `mean train n` beside it.
- **The era split is equal-*time*, not equal-count**, because the question is whether one calendar period predicts the next.
- **`split_half_corr` needs ≥3 cells** with varying rates to be defined; `None` means it could not be computed, not zero correlation.
- **Rows lacking stored ATR are dropped**, not proxied — avoids the audit's R4 look-ahead.

## Notes

- If Test 1 finds a working window **and** Test 2 finds drift, the fix is a decayed/rolling cell rate in `nt_bracket_reachability`.
- If Test 2 finds the rate **stable** but Test 1 finds no window works, the cell is not the model and the thesis needs replacing, not tuning.

