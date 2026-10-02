# Does conditioning the reachability cell improve out-of-fold AUC?

Generated 2026-10-02 01:17 UTC · lookback 60d · 5-fold expanding walk-forward · light sweep, resolved rows only

The falsifiable test of `docs/scopes/20261002_SIGNAL_QUALITY_METRIC_AND_REACHABILITY_THESIS.md` §3.3. Every cell rate is estimated on **training rows only**; each fold is scored with rates frozen before it. Adding conditioning always improves in-sample fit, so only out-of-fold AUC is reported.

## Verdict

**Cell alone: AUC 0.4994** (n=19019, base rate 0.3244, fold spread ±0.0566)

| conditioning added | AUC | Δ vs cell alone | read |
|---|---:|---:|---|
| `cell+dir` | 0.5318 | +0.0324 | WITHIN NOISE — AUC 0.4994 → 0.5318 (+0.0324) clears the 0.02 bar but is smaller than the ±0.0566 fold spread |
| `cell+trend` | 0.5301 | +0.0307 | WITHIN NOISE — AUC 0.4994 → 0.5301 (+0.0307) clears the 0.02 bar but is smaller than the ±0.0566 fold spread |
| `cell+dir+trend` | 0.5301 | +0.0307 | WITHIN NOISE — AUC 0.4994 → 0.5301 (+0.0307) clears the 0.02 bar but is smaller than the ±0.0566 fold spread |
| `cell+p_win` | 0.5115 | +0.0121 | MARGINAL — AUC 0.4994 → 0.5115 (+0.0121), below the 0.02 bar |

## All models (walk-forward, out-of-fold)

| model | n | AUC (oof) | AUC (in-sample ceiling) | Brier | REL | RES | BSS |
|---|---:|---:|---:|---:|---:|---:|---:|
| `cell+dir` | 19019 | 0.5318 | 0.7915 | 0.2568 | 0.0413 | 0.0027 | -0.1719 |
| `cell+trend` | 19019 | 0.5301 | 0.8262 | 0.2559 | 0.0382 | 0.0018 | -0.1677 |
| `cell+dir+trend` | 19019 | 0.5301 | 0.8262 | 0.2559 | 0.0382 | 0.0018 | -0.1677 |
| `cell+p_win` | 19019 | 0.5115 | 0.7994 | 0.2559 | 0.0382 | 0.0006 | -0.1675 |
| `cell` | 19019 | 0.4994 | 0.7256 | 0.2523 | 0.0353 | 0.0016 | -0.1512 |

`REL` lower is better, `RES` higher is better, `BSS > 0` beats the base rate. The **in-sample ceiling** is fit and scored on the same rows — it is an upper bound, never a result. It exists to separate *no signal* from *no transferable signal*.

### Did the signal transfer, or was there none?

- `cell+dir` — NO TRANSFER — in-sample 0.7915 but out-of-fold 0.5318: the cell memorises the training window and does not survive time
- `cell+trend` — NO TRANSFER — in-sample 0.8262 but out-of-fold 0.5301: the cell memorises the training window and does not survive time
- `cell+dir+trend` — NO TRANSFER — in-sample 0.8262 but out-of-fold 0.5301: the cell memorises the training window and does not survive time
- `cell+p_win` — NO TRANSFER — in-sample 0.7994 but out-of-fold 0.5115: the cell memorises the training window and does not survive time
- `cell` — NO TRANSFER — in-sample 0.7256 but out-of-fold 0.4994: the cell memorises the training window and does not survive time

## Per-fold AUC (stability)

A gain that only appears in one fold is noise. The bar is a gain that holds across folds.

| model | f1 | f2 | f3 | f4 | f5 |
|---|---:|---:|---:|---:|---:|
| `cell+dir` | 0.4782 | 0.5628 | 0.5260 | 0.6395 | 0.4725 |
| `cell+trend` | 0.4737 | 0.5377 | 0.5539 | 0.6552 | 0.4658 |
| `cell+dir+trend` | 0.4737 | 0.5377 | 0.5539 | 0.6552 | 0.4658 |
| `cell+p_win` | 0.4276 | 0.5569 | 0.5202 | 0.5805 | 0.4902 |
| `cell` | 0.4375 | 0.5442 | 0.4777 | 0.5772 | 0.4773 |

## Coverage

- fetched: **41685** resolved light signals
- usable (ATR + prices + bucketable): **38038** (91.3%)
- dropped: 3647 no ATR, 0 no prices, 0 unbucketable

- distinct cells: **228** across 14 symbols
- bucket mix: 4+=27101, 1-2=4864, 2-4=3090, 0.5-1=2397, <0.5=586
- **concentration: `4+` holds 71.2% of all rows** (27101 of 38038)

> **The bucket space is degenerate.** One bucket holds 71% of the population, so a `(symbol, vol_tier, bucket)` key is barely finer than `(symbol, vol_tier)` — the distance/ATR dimension is not doing the work the thesis assumes. This is the audit's G2 in data: brackets are built in %-price space, so almost everything lands in the widest ATR bucket. A bucket that holds three-quarters of the data cannot discriminate.

## The cell table — per symbol × vol_tier × bucket

This is the model under the thesis: `P(tp_hit | cell)`. Rows below `min_n=20` are unreliable and back off in the ladder.

| symbol | vol_tier | bucket | n | P(tp_hit) |
|---|---|---|---:|---:|
| AAPL | low_vol | 4+ | 3900 | 0.142 |
| XAUEUR | high_vol | 4+ | 1605 | 0.118 |
| BTCUSD | med_low_vol | 4+ | 1282 | 0.399 |
| XAGUSD | med_low_vol | 4+ | 1048 | 0.127 |
| XAUUSD | med_low_vol | 4+ | 1011 | 0.128 |
| XAGUSD | low_vol | 4+ | 943 | 0.243 |
| XAUAUD | med_low_vol | 4+ | 941 | 0.344 |
| USDJPY | med_high_vol | 4+ | 821 | 0.184 |
| XAUAUD | low_vol | 4+ | 789 | 0.294 |
| XAUUSD | low_vol | 4+ | 785 | 0.237 |
| XAUEUR | med_low_vol | 4+ | 730 | 0.227 |
| XAUUSD | high_vol | 4+ | 712 | 0.124 |
| BTCUSD | med_high_vol | 4+ | 667 | 0.352 |
| XAUAUD | med_high_vol | 4+ | 658 | 0.213 |
| XAUEUR | low_vol | 4+ | 630 | 0.119 |
| BTCUSD | high_vol | 4+ | 591 | 0.433 |
| XAUEUR | med_high_vol | 4+ | 555 | 0.202 |
| XAGUSD | high_vol | 1-2 | 525 | 0.937 |
| XAUUSD | med_high_vol | 4+ | 521 | 0.200 |
| USDJPY | high_vol | 4+ | 495 | 0.246 |
| US30USD | med_low_vol | 4+ | 487 | 0.308 |
| US30USD | med_high_vol | 4+ | 485 | 0.285 |
| XAUAUD | high_vol | 2-4 | 484 | 0.002 |
| XAUAUD | high_vol | 4+ | 468 | 0.150 |
| US30USD | low_vol | 4+ | 441 | 0.299 |
| XAGUSD | high_vol | 4+ | 422 | 0.332 |
| US500 | low_vol | 4+ | 411 | 0.406 |
| USDJPY | med_low_vol | 4+ | 393 | 0.351 |
| AAPL | high_vol | 4+ | 388 | 0.291 |
| BTCUSD | low_vol | 4+ | 386 | 0.383 |
| XAGUSD | med_high_vol | 4+ | 366 | 0.257 |
| AAPL | med_low_vol | 4+ | 365 | 0.156 |
| USDCAD | med_low_vol | 4+ | 337 | 0.074 |
| XAGUSD | med_low_vol | 1-2 | 333 | 0.360 |
| XAUAUD | med_low_vol | 0.5-1 | 302 | 0.364 |
| US30USD | high_vol | 4+ | 298 | 0.262 |
| USDCAD | low_vol | 4+ | 268 | 0.146 |
| XAUEUR | low_vol | 1-2 | 263 | 0.335 |
| US500 | med_high_vol | 4+ | 256 | 0.137 |
| XAUUSD | med_low_vol | 1-2 | 255 | 0.329 |
| US500 | med_low_vol | 4+ | 253 | 0.059 |
| US500 | med_high_vol | 1-2 | 245 | 0.188 |
| AUDUSD | med_low_vol | 4+ | 236 | 0.114 |
| XAGUSD | high_vol | 2-4 | 221 | 0.222 |
| US500 | high_vol | 4+ | 216 | 0.019 |
| GBPUSD | med_high_vol | 4+ | 210 | 0.119 |
| US500 | high_vol | 1-2 | 197 | 0.391 |
| US500 | med_low_vol | 1-2 | 173 | 0.434 |
| GBPUSD | med_high_vol | 1-2 | 171 | 0.211 |
| AAPL | med_high_vol | 4+ | 167 | 0.563 |
| USDCHF | med_high_vol | 4+ | 162 | 0.043 |
| EURUSD | med_high_vol | 4+ | 156 | 0.038 |
| EURUSD | med_high_vol | 2-4 | 151 | 0.695 |
| XAUAUD | low_vol | 0.5-1 | 149 | 0.362 |
| USDCAD | low_vol | 0.5-1 | 145 | 0.697 |
| XAGUSD | low_vol | 1-2 | 143 | 0.455 |
| USDJPY | med_low_vol | <0.5 | 141 | 0.504 |
| XAUAUD | med_high_vol | 1-2 | 140 | 0.336 |
| GBPUSD | low_vol | 4+ | 134 | 0.306 |
| USDCHF | med_high_vol | 0.5-1 | 127 | 0.724 |
| USDCHF | med_low_vol | 4+ | 126 | 0.016 |
| USDJPY | low_vol | 4+ | 124 | 0.387 |
| XAGUSD | med_high_vol | 2-4 | 122 | 0.262 |
| BTCUSD | med_high_vol | 1-2 | 119 | 0.454 |
| USDCHF | high_vol | 4+ | 118 | 0.000 |
| USDCHF | low_vol | 0.5-1 | 118 | 0.339 |
| XAGUSD | med_low_vol | 2-4 | 115 | 0.617 |
| XAUUSD | low_vol | 0.5-1 | 111 | 0.414 |
| GBPUSD | med_low_vol | 4+ | 111 | 0.162 |
| USDJPY | med_low_vol | 1-2 | 111 | 0.739 |
| XAUAUD | med_low_vol | 1-2 | 109 | 0.413 |
| USDJPY | med_high_vol | <0.5 | 108 | 0.574 |
| EURUSD | med_low_vol | 4+ | 104 | 0.106 |
| BTCUSD | med_low_vol | 0.5-1 | 102 | 0.471 |
| XAUEUR | high_vol | 1-2 | 101 | 0.455 |
| USDCHF | med_low_vol | 2-4 | 101 | 0.386 |
| USDCAD | med_high_vol | 4+ | 98 | 0.102 |
| GBPUSD | high_vol | 4+ | 98 | 0.082 |
| US30USD | high_vol | 0.5-1 | 98 | 0.633 |
| USDCAD | high_vol | 2-4 | 96 | 0.448 |
| USDCHF | med_high_vol | 2-4 | 94 | 0.479 |
| BTCUSD | high_vol | 2-4 | 93 | 0.473 |
| XAUUSD | high_vol | 1-2 | 93 | 0.366 |
| USDCAD | high_vol | 4+ | 92 | 0.076 |
| XAUAUD | high_vol | 0.5-1 | 90 | 0.467 |
| GBPUSD | med_low_vol | 1-2 | 90 | 0.356 |
| GBPUSD | med_high_vol | 2-4 | 87 | 0.310 |
| BTCUSD | med_low_vol | 2-4 | 82 | 0.293 |
| XAUEUR | med_high_vol | 1-2 | 82 | 0.549 |
| XAUAUD | med_high_vol | 0.5-1 | 82 | 0.439 |
| XAUEUR | med_low_vol | 1-2 | 81 | 0.494 |
| BTCUSD | med_low_vol | <0.5 | 80 | 0.525 |
| BTCUSD | med_low_vol | 1-2 | 80 | 0.475 |
| AUDUSD | med_low_vol | 1-2 | 79 | 0.582 |
| AUDUSD | med_high_vol | 1-2 | 79 | 0.418 |
| USDCAD | low_vol | 2-4 | 79 | 0.152 |
| BTCUSD | low_vol | <0.5 | 78 | 0.321 |
| USDCHF | med_low_vol | 1-2 | 78 | 0.167 |
| US500 | low_vol | 1-2 | 77 | 0.338 |
| BTCUSD | high_vol | 1-2 | 75 | 0.453 |
| XAUUSD | low_vol | 1-2 | 74 | 0.446 |
| US30USD | med_high_vol | 2-4 | 74 | 0.297 |
| US30USD | med_low_vol | 0.5-1 | 74 | 0.473 |
| XAGUSD | med_high_vol | 1-2 | 72 | 0.278 |
| US30USD | med_low_vol | 1-2 | 72 | 0.417 |
| EURUSD | med_high_vol | 1-2 | 70 | 0.443 |
| GBPUSD | med_low_vol | 2-4 | 70 | 0.486 |
| BTCUSD | low_vol | 1-2 | 70 | 0.271 |
| XAUUSD | med_low_vol | 0.5-1 | 68 | 0.485 |
| USDCHF | low_vol | 2-4 | 66 | 0.409 |
| GBPUSD | low_vol | 1-2 | 65 | 0.400 |
| USDCHF | low_vol | 4+ | 63 | 0.000 |
| XAUUSD | high_vol | 0.5-1 | 63 | 0.651 |
| EURUSD | med_low_vol | 2-4 | 63 | 0.429 |
| US30USD | med_low_vol | 2-4 | 62 | 0.306 |
| EURUSD | high_vol | 2-4 | 62 | 0.355 |
| AUDUSD | low_vol | 1-2 | 60 | 0.600 |
| XAUEUR | high_vol | 0.5-1 | 58 | 0.345 |
| USDCHF | high_vol | 2-4 | 57 | 0.404 |
| XAGUSD | low_vol | 2-4 | 56 | 0.250 |
| AUDUSD | med_low_vol | 0.5-1 | 56 | 0.321 |
| AAPL | med_low_vol | 0.5-1 | 56 | 0.964 |
| AUDUSD | med_high_vol | 0.5-1 | 54 | 0.259 |
| USDCAD | med_low_vol | 2-4 | 53 | 0.151 |
| XAUAUD | low_vol | 1-2 | 52 | 0.346 |
| USDJPY | low_vol | 0.5-1 | 52 | 0.558 |
| AUDUSD | med_low_vol | 2-4 | 51 | 0.490 |
| XAUUSD | med_high_vol | 0.5-1 | 50 | 0.460 |
| XAUEUR | med_low_vol | 2-4 | 50 | 0.380 |
| BTCUSD | med_high_vol | 2-4 | 49 | 0.673 |
| AUDUSD | high_vol | 1-2 | 49 | 0.510 |
| US30USD | high_vol | 1-2 | 48 | 0.229 |
| EURUSD | low_vol | 1-2 | 48 | 0.375 |
| US30USD | high_vol | <0.5 | 47 | 0.426 |
| XAUUSD | med_high_vol | 1-2 | 47 | 0.277 |
| US30USD | low_vol | 2-4 | 46 | 0.522 |
| EURUSD | high_vol | 1-2 | 46 | 0.283 |
| USDJPY | med_high_vol | 2-4 | 46 | 0.348 |
| AAPL | low_vol | 0.5-1 | 46 | 0.783 |
| EURUSD | low_vol | 4+ | 45 | 0.067 |
| USDJPY | med_high_vol | 1-2 | 44 | 0.273 |
| US500 | med_high_vol | 2-4 | 43 | 0.628 |
| XAUEUR | low_vol | 2-4 | 43 | 0.581 |
| XAUAUD | med_high_vol | 2-4 | 42 | 0.048 |
| XAUAUD | high_vol | 1-2 | 40 | 0.100 |
| US500 | med_low_vol | 2-4 | 39 | 0.359 |
| US500 | med_high_vol | 0.5-1 | 39 | 0.564 |
| USDCHF | med_high_vol | 1-2 | 38 | 0.316 |
| AUDUSD | high_vol | 0.5-1 | 37 | 0.351 |
| XAUEUR | high_vol | 2-4 | 37 | 0.351 |
| AUDUSD | low_vol | 4+ | 36 | 0.000 |
| US30USD | low_vol | 1-2 | 36 | 0.306 |
| USDCHF | med_low_vol | 0.5-1 | 35 | 0.429 |
| USDJPY | med_high_vol | 0.5-1 | 35 | 0.314 |
| AUDUSD | med_high_vol | 4+ | 34 | 0.206 |
| USDCHF | low_vol | 1-2 | 34 | 0.059 |
| BTCUSD | high_vol | <0.5 | 34 | 0.471 |
| USDCAD | high_vol | 0.5-1 | 33 | 0.273 |
| GBPUSD | low_vol | 2-4 | 33 | 0.485 |
| XAUUSD | low_vol | 2-4 | 32 | 0.875 |
| EURUSD | high_vol | 4+ | 32 | 0.094 |
| AUDUSD | high_vol | 4+ | 31 | 0.032 |
| USDCHF | high_vol | 1-2 | 31 | 0.226 |
| USDCAD | med_high_vol | 2-4 | 31 | 0.645 |
| US500 | low_vol | 2-4 | 30 | 0.000 |
| US500 | low_vol | 0.5-1 | 29 | 0.517 |
| USDCAD | med_low_vol | 0.5-1 | 29 | 0.379 |
| EURUSD | med_low_vol | 1-2 | 28 | 0.714 |
| AUDUSD | med_high_vol | 2-4 | 27 | 0.333 |
| AUDUSD | low_vol | 2-4 | 26 | 0.500 |
| US30USD | med_high_vol | 0.5-1 | 26 | 0.654 |
| US30USD | med_high_vol | 1-2 | 26 | 0.423 |
| US30USD | high_vol | 2-4 | 25 | 0.200 |
| XAUEUR | med_high_vol | 2-4 | 25 | 0.360 |
| US30USD | low_vol | <0.5 | 24 | 0.333 |
| EURUSD | low_vol | 2-4 | 24 | 0.500 |
| USDCAD | high_vol | 1-2 | 23 | 0.261 |
| GBPUSD | high_vol | 1-2 | 23 | 0.043 |
| USDCHF | high_vol | 0.5-1 | 22 | 0.500 |
| US30USD | low_vol | 0.5-1 | 22 | 0.682 |
| BTCUSD | med_high_vol | 0.5-1 | 22 | 0.545 |
| XAUUSD | high_vol | 2-4 | 18 | 1.000 |
| AUDUSD | high_vol | 2-4 | 17 | 0.294 |
| XAGUSD | med_low_vol | 0.5-1 | 17 | 0.118 |
| XAGUSD | high_vol | 0.5-1 | 17 | 0.706 |
| USDJPY | low_vol | 1-2 | 17 | 0.176 |
| USDCAD | med_low_vol | 1-2 | 17 | 0.412 |
| USDJPY | high_vol | 2-4 | 16 | 0.125 |
| GBPUSD | high_vol | 0.5-1 | 16 | 0.250 |
| US500 | med_low_vol | 0.5-1 | 15 | 0.667 |
| USDJPY | med_low_vol | 0.5-1 | 14 | 0.214 |
| USDJPY | med_low_vol | 2-4 | 13 | 0.000 |
| USDJPY | high_vol | <0.5 | 12 | 0.917 |
| US500 | high_vol | <0.5 | 12 | 0.333 |
| USDJPY | high_vol | 1-2 | 12 | 0.333 |
| USDCAD | low_vol | 1-2 | 12 | 0.917 |
| USDJPY | high_vol | 0.5-1 | 11 | 0.818 |
| BTCUSD | high_vol | 0.5-1 | 10 | 0.800 |
| XAUUSD | med_low_vol | 2-4 | 10 | 0.200 |
| EURUSD | high_vol | 0.5-1 | 9 | 0.444 |
| USDCAD | med_high_vol | 1-2 | 9 | 0.444 |
| US30USD | med_low_vol | <0.5 | 8 | 0.625 |
| BTCUSD | low_vol | 2-4 | 8 | 0.250 |
| US500 | low_vol | <0.5 | 8 | 0.000 |
| USDJPY | low_vol | <0.5 | 8 | 0.875 |
| AAPL | med_high_vol | 0.5-1 | 8 | 0.625 |
| GBPUSD | med_high_vol | 0.5-1 | 8 | 0.875 |
| US500 | high_vol | 0.5-1 | 7 | 0.571 |
| XAGUSD | med_high_vol | 0.5-1 | 7 | 0.714 |
| GBPUSD | high_vol | 2-4 | 7 | 0.286 |
| AUDUSD | low_vol | 0.5-1 | 7 | 0.429 |
| GBPUSD | med_low_vol | 0.5-1 | 7 | 0.143 |
| US30USD | med_high_vol | <0.5 | 6 | 0.500 |
| US500 | high_vol | 2-4 | 6 | 0.000 |
| USDJPY | low_vol | 2-4 | 6 | 0.000 |
| US500 | med_low_vol | <0.5 | 5 | 0.000 |
| XAUAUD | med_high_vol | <0.5 | 5 | 0.400 |
| XAUAUD | low_vol | <0.5 | 5 | 0.200 |
| USDCAD | med_high_vol | 0.5-1 | 5 | 1.000 |
| BTCUSD | med_high_vol | <0.5 | 4 | 0.500 |
| XAUEUR | med_low_vol | 0.5-1 | 4 | 0.000 |
| AAPL | high_vol | 1-2 | 2 | 0.000 |
| EURUSD | med_low_vol | 0.5-1 | 2 | 1.000 |
| BTCUSD | low_vol | 0.5-1 | 2 | 0.000 |
| XAUAUD | med_low_vol | 2-4 | 1 | 0.000 |
| AAPL | low_vol | 2-4 | 1 | 0.000 |
| XAGUSD | low_vol | 0.5-1 | 1 | 1.000 |
| US500 | med_high_vol | <0.5 | 1 | 1.000 |

## Caveats

- **Resolved rows only** (`tp_hit`/`sl_hit`). Expired is excluded because it has no TP/SL verdict; that is a selected sub-population and the single biggest remaining threat to this measurement (audit C2/R6). A first-touch resolver over the full population is the fix.
- **`atr` is the stored capture-time ATR** where present. Rows predating the `atr` column are dropped, not proxied — so this cannot inherit the audit's R4 look-ahead (proxy ATR uses *current* ATR for historical rows).
- **Cell rates are fit out-of-fold** but the *bucket definition* (`distance/ATR` thresholds) is fixed a priori, not tuned here — so there is no threshold-selection leak.
- **Shrinkage α is fixed** at the module default across all models, so it cannot favour one. It is not tuned on the evaluation data.
- **5 folds, expanding window.** Per-fold AUCs are shown because a pooled number can hide a gain that lives in one fold.

