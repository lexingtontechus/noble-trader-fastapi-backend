# Vol Forecast Backtest — Phase 0 (measured or nothing)

**Run:** 2026-10-19 00:30:15 · source: `synthetic(seed=7, days=365)` · symbols: USDJPY, BTCUSD, XAUUSD · horizons: [6, 24, 48] · wall: 0.5s

Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC (overlap-aware) + Holm. Per-symbol rows — not pooled.

## Skipped arms (reported, never silent)

| arm | reason |
|---|---|
| `chronos2` | chronos-forecasting not usable (ModuleNotFoundError: No module named 'chronos') |
| `chronos2_cov` | chronos-forecasting not usable (ModuleNotFoundError: No module named 'chronos') |
| `garch` | arch not installed (ModuleNotFoundError) |
| `timesfm25` | unreachable http://localhost:8002 (ConnectError) |

## Acceptance criteria check (thesis §benchmark, corrected nulls)

| criterion | verdict per symbol (see dm_tests.csv for stats) |
|---|---|
| H=6 · USDJPY — MAE vs naive: 0.00302078 (raw scale; use qlike+DM for decisions) | `timesfm25`: qlike=nan dir=nan (null=0.390) cov80=n/a | DM-beats-HAR✗ |
| H=24 · USDJPY — MAE vs naive: 0.00427954 (raw scale; use qlike+DM for decisions) | `timesfm25`: qlike=nan dir=nan (null=0.288) cov80=n/a | DM-beats-HAR✗ |
| H=48 · USDJPY — MAE vs naive: 0.00710908 (raw scale; use qlike+DM for decisions) | `timesfm25`: qlike=nan dir=nan (null=0.356) cov80=n/a | DM-beats-HAR✗ |

## Metrics (per symbol × horizon × arm)

### horizon = 6 bars

| symbol   | arm       |   n |    qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:----------|----:|---------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma      |  56 |   0.1007 |    0.3466 |    0.7500 |                 0.3571 |       0.7143 |
| BTCUSD   | har       |  55 |   0.0897 |    0.3192 |    0.6364 |                 0.3571 |       0.8545 |
| BTCUSD   | har_ewma  |  55 |   0.0886 |    0.3227 |    0.6909 |                 0.3571 |       0.8000 |
| BTCUSD   | naive     |  56 |   0.0832 |    0.3352 |    0.5357 |                 0.3571 |     nan      |
| USDJPY   | ewma      |  59 |   0.1009 |    0.3698 |    0.5932 |                 0.3898 |       0.6780 |
| USDJPY   | har       |  58 |   0.0882 |    0.3240 |    0.6552 |                 0.3898 |       0.7931 |
| USDJPY   | har_ewma  |  58 |   0.0918 |    0.3464 |    0.6207 |                 0.3898 |       0.6897 |
| USDJPY   | naive     |  59 |   0.1200 |    0.3762 |    0.4576 |                 0.3898 |     nan      |
| USDJPY   | timesfm25 |   0 | nan      |  nan      |  nan      |                 0.3898 |     nan      |
| XAUUSD   | ewma      |  59 |   0.2600 |    0.4681 |    0.5932 |                 0.3220 |       0.6949 |
| XAUUSD   | har       |  58 |   0.2312 |    0.4231 |    0.5862 |                 0.3220 |       0.7759 |
| XAUUSD   | har_ewma  |  58 |   0.2416 |    0.4399 |    0.6034 |                 0.3220 |       0.7414 |
| XAUUSD   | naive     |  59 |   0.2774 |    0.3947 |    0.4576 |                 0.3220 |     nan      |

### horizon = 24 bars

| symbol   | arm       |   n |    qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:----------|----:|---------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma      |  56 |   0.1498 |    0.3287 |    0.5000 |                 0.3750 |       0.5357 |
| BTCUSD   | har       |  55 |   0.1083 |    0.2938 |    0.6909 |                 0.3750 |       0.8545 |
| BTCUSD   | har_ewma  |  55 |   0.1245 |    0.2981 |    0.6727 |                 0.3750 |       0.7455 |
| BTCUSD   | naive     |  56 |   0.1544 |    0.3039 |    0.5000 |                 0.3750 |     nan      |
| USDJPY   | ewma      |  59 |   0.0786 |    0.2858 |    0.5424 |                 0.2881 |       0.5254 |
| USDJPY   | har       |  58 |   0.0819 |    0.3061 |    0.5862 |                 0.2881 |       0.8103 |
| USDJPY   | har_ewma  |  58 |   0.0768 |    0.2875 |    0.6379 |                 0.2881 |       0.7069 |
| USDJPY   | naive     |  59 |   0.0703 |    0.2627 |    0.5424 |                 0.2881 |     nan      |
| USDJPY   | timesfm25 |   0 | nan      |  nan      |  nan      |                 0.2881 |     nan      |
| XAUUSD   | ewma      |  59 |   0.2178 |    0.3845 |    0.5593 |                 0.4407 |       0.4746 |
| XAUUSD   | har       |  58 |   0.1616 |    0.3608 |    0.6552 |                 0.4407 |       0.7931 |
| XAUUSD   | har_ewma  |  58 |   0.1834 |    0.3633 |    0.6724 |                 0.4407 |       0.7069 |
| XAUUSD   | naive     |  59 |   0.2146 |    0.3461 |    0.5932 |                 0.4407 |     nan      |

### horizon = 48 bars

| symbol   | arm       |   n |    qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:----------|----:|---------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma      |  56 |   0.1988 |    0.3513 |    0.4464 |                 0.5357 |       0.4286 |
| BTCUSD   | har       |  55 |   0.1443 |    0.3853 |    0.6909 |                 0.5357 |       0.8545 |
| BTCUSD   | har_ewma  |  55 |   0.1584 |    0.3505 |    0.6545 |                 0.5357 |       0.7273 |
| BTCUSD   | naive     |  56 |   0.2077 |    0.3597 |    0.4107 |                 0.5357 |     nan      |
| USDJPY   | ewma      |  59 |   0.1245 |    0.3184 |    0.5254 |                 0.3559 |       0.3390 |
| USDJPY   | har       |  58 |   0.1272 |    0.3558 |    0.6552 |                 0.3559 |       0.8103 |
| USDJPY   | har_ewma  |  58 |   0.1204 |    0.3249 |    0.5862 |                 0.3559 |       0.7069 |
| USDJPY   | naive     |  59 |   0.1591 |    0.3421 |    0.4576 |                 0.3559 |     nan      |
| USDJPY   | timesfm25 |   0 | nan      |  nan      |  nan      |                 0.3559 |     nan      |
| XAUUSD   | ewma      |  59 |   0.2431 |    0.4333 |    0.5254 |                 0.5763 |       0.3390 |
| XAUUSD   | har       |  58 |   0.1986 |    0.4485 |    0.7759 |                 0.5763 |       0.7069 |
| XAUUSD   | har_ewma  |  58 |   0.1928 |    0.4192 |    0.7414 |                 0.5763 |       0.6207 |
| XAUUSD   | naive     |  59 |   0.2861 |    0.4571 |    0.4576 |                 0.5763 |     nan      |

### QLIKE winners (lower = better)

| symbol   |   horizon | best_arm   |   qlike |   har_qlike |
|:---------|----------:|:-----------|--------:|------------:|
| BTCUSD   |         6 | naive      |  0.0832 |      0.0897 |
| BTCUSD   |        24 | har        |  0.1083 |      0.1083 |
| BTCUSD   |        48 | har        |  0.1443 |      0.1443 |
| USDJPY   |         6 | har        |  0.0882 |      0.0882 |
| USDJPY   |        24 | naive      |  0.0703 |      0.0819 |
| USDJPY   |        48 | har_ewma   |  0.1204 |      0.1272 |
| XAUUSD   |         6 | har        |  0.2312 |      0.2312 |
| XAUUSD   |        24 | har        |  0.1616 |      0.1616 |
| XAUUSD   |        48 | har_ewma   |  0.1928 |      0.1986 |

## Diebold-Mariano (QLIKE, HAC + Holm)

#### vs `har`

| symbol   |   horizon | arm_a     | arm_b   |   n |   mean_loss_diff |   dm_stat |   p_value |   p_holm |   hac_lag | significant_holm   |
|:---------|----------:|:----------|:--------|----:|-----------------:|----------:|----------:|---------:|----------:|:-------------------|
| BTCUSD   |         6 | ewma      | har     |  55 |           0.0078 |    0.5481 |    0.5836 |   1.0000 |         0 | False              |
| BTCUSD   |         6 | har_ewma  | har     |  55 |          -0.0012 |   -0.1317 |    0.8952 |   1.0000 |         0 | False              |
| BTCUSD   |         6 | naive     | har     |  55 |          -0.0107 |   -0.4226 |    0.6726 |   1.0000 |         0 | False              |
| BTCUSD   |        24 | ewma      | har     |  55 |           0.0434 |    1.9908 |    0.0465 |   0.1395 |         0 | False              |
| BTCUSD   |        24 | har_ewma  | har     |  55 |           0.0162 |    1.6667 |    0.0956 |   0.1912 |         0 | False              |
| BTCUSD   |        24 | naive     | har     |  55 |           0.0479 |    1.4205 |    0.1555 |   0.1912 |         0 | False              |
| BTCUSD   |        48 | ewma      | har     |  55 |           0.0577 |    1.4429 |    0.1490 |   0.2981 |         0 | False              |
| BTCUSD   |        48 | har_ewma  | har     |  55 |           0.0142 |    0.9662 |    0.3340 |   0.3340 |         0 | False              |
| BTCUSD   |        48 | naive     | har     |  55 |           0.0670 |    1.8420 |    0.0655 |   0.1964 |         0 | False              |
| USDJPY   |         6 | ewma      | har     |  58 |           0.0138 |    0.9492 |    0.3425 |   1.0000 |         0 | False              |
| USDJPY   |         6 | har_ewma  | har     |  58 |           0.0037 |    0.4083 |    0.6831 |   1.0000 |         0 | False              |
| USDJPY   |         6 | naive     | har     |  58 |           0.0326 |    1.3870 |    0.1654 |   0.6617 |         0 | False              |
| USDJPY   |         6 | timesfm25 | har     |   0 |         nan      |  nan      |    1.0000 |   1.0000 |         0 | False              |
| USDJPY   |        24 | ewma      | har     |  58 |          -0.0021 |   -0.1844 |    0.8537 |   1.0000 |         0 | False              |
| USDJPY   |        24 | har_ewma  | har     |  58 |          -0.0051 |   -0.7311 |    0.4647 |   1.0000 |         0 | False              |
| USDJPY   |        24 | naive     | har     |  58 |          -0.0107 |   -1.2618 |    0.2070 |   0.8280 |         0 | False              |
| USDJPY   |        24 | timesfm25 | har     |   0 |         nan      |  nan      |    1.0000 |   1.0000 |         0 | False              |
| USDJPY   |        48 | ewma      | har     |  58 |          -0.0007 |   -0.0471 |    0.9625 |   1.0000 |         0 | False              |
| USDJPY   |        48 | har_ewma  | har     |  58 |          -0.0069 |   -0.8280 |    0.4076 |   1.0000 |         0 | False              |
| USDJPY   |        48 | naive     | har     |  58 |           0.0346 |    1.4904 |    0.1361 |   0.5444 |         0 | False              |
| USDJPY   |        48 | timesfm25 | har     |   0 |         nan      |  nan      |    1.0000 |   1.0000 |         0 | False              |
| XAUUSD   |         6 | ewma      | har     |  58 |           0.0332 |    1.6142 |    0.1065 |   0.3195 |         0 | False              |
| XAUUSD   |         6 | har_ewma  | har     |  58 |           0.0104 |    0.8782 |    0.3798 |   0.7596 |         0 | False              |
| XAUUSD   |         6 | naive     | har     |  58 |           0.0504 |    0.4954 |    0.6203 |   0.7596 |         0 | False              |
| XAUUSD   |        24 | ewma      | har     |  58 |           0.0595 |    1.8551 |    0.0636 |   0.1907 |         0 | False              |
| XAUUSD   |        24 | har_ewma  | har     |  58 |           0.0218 |    1.6237 |    0.1044 |   0.2089 |         0 | False              |
| XAUUSD   |        24 | naive     | har     |  58 |           0.0563 |    1.5905 |    0.1117 |   0.2089 |         0 | False              |
| XAUUSD   |        48 | ewma      | har     |  58 |           0.0482 |    0.9394 |    0.3475 |   0.6951 |         0 | False              |
| XAUUSD   |        48 | har_ewma  | har     |  58 |          -0.0059 |   -0.1878 |    0.8510 |   0.8510 |         0 | False              |
| XAUUSD   |        48 | naive     | har     |  58 |           0.0922 |    1.6389 |    0.1012 |   0.3037 |         0 | False              |

#### vs `naive`

| symbol   |   horizon | arm_a     | arm_b   |   n |   mean_loss_diff |   dm_stat |   p_value |   p_holm |   hac_lag | significant_holm   |
|:---------|----------:|:----------|:--------|----:|-----------------:|----------:|----------:|---------:|----------:|:-------------------|
| BTCUSD   |         6 | ewma      | naive   |  56 |           0.0175 |    0.5380 |    0.5906 |   1.0000 |         0 | False              |
| BTCUSD   |         6 | har       | naive   |  55 |           0.0107 |    0.4226 |    0.6726 |   1.0000 |         0 | False              |
| BTCUSD   |         6 | har_ewma  | naive   |  55 |           0.0095 |    0.3248 |    0.7453 |   1.0000 |         0 | False              |
| BTCUSD   |        24 | ewma      | naive   |  56 |          -0.0047 |   -0.2911 |    0.7710 |   0.7710 |         0 | False              |
| BTCUSD   |        24 | har       | naive   |  55 |          -0.0479 |   -1.4205 |    0.1555 |   0.4664 |         0 | False              |
| BTCUSD   |        24 | har_ewma  | naive   |  55 |          -0.0317 |   -1.2078 |    0.2271 |   0.4664 |         0 | False              |
| BTCUSD   |        48 | ewma      | naive   |  56 |          -0.0089 |   -0.5067 |    0.6124 |   0.6124 |         0 | False              |
| BTCUSD   |        48 | har       | naive   |  55 |          -0.0670 |   -1.8420 |    0.0655 |   0.1310 |         0 | False              |
| BTCUSD   |        48 | har_ewma  | naive   |  55 |          -0.0528 |   -2.2537 |    0.0242 |   0.0726 |         0 | False              |
| USDJPY   |         6 | ewma      | naive   |  59 |          -0.0191 |   -0.7171 |    0.4733 |   0.9467 |         0 | False              |
| USDJPY   |         6 | har       | naive   |  58 |          -0.0326 |   -1.3870 |    0.1654 |   0.6617 |         0 | False              |
| USDJPY   |         6 | har_ewma  | naive   |  58 |          -0.0289 |   -1.1665 |    0.2434 |   0.7302 |         0 | False              |
| USDJPY   |         6 | timesfm25 | naive   |   0 |         nan      |  nan      |    1.0000 |   1.0000 |         0 | False              |
| USDJPY   |        24 | ewma      | naive   |  59 |           0.0083 |    1.1810 |    0.2376 |   0.8280 |         0 | False              |
| USDJPY   |        24 | har       | naive   |  58 |           0.0107 |    1.2618 |    0.2070 |   0.8280 |         0 | False              |
| USDJPY   |        24 | har_ewma  | naive   |  58 |           0.0056 |    1.0936 |    0.2741 |   0.8280 |         0 | False              |
| USDJPY   |        24 | timesfm25 | naive   |   0 |         nan      |  nan      |    1.0000 |   1.0000 |         0 | False              |
| USDJPY   |        48 | ewma      | naive   |  59 |          -0.0347 |   -2.1279 |    0.0333 |   0.1000 |         0 | False              |
| USDJPY   |        48 | har       | naive   |  58 |          -0.0346 |   -1.4904 |    0.1361 |   0.2722 |         0 | False              |
| USDJPY   |        48 | har_ewma  | naive   |  58 |          -0.0415 |   -2.2429 |    0.0249 |   0.0996 |         0 | False              |
| USDJPY   |        48 | timesfm25 | naive   |   0 |         nan      |  nan      |    1.0000 |   1.0000 |         0 | False              |
| XAUUSD   |         6 | ewma      | naive   |  59 |          -0.0175 |   -0.1971 |    0.8438 |   1.0000 |         0 | False              |
| XAUUSD   |         6 | har       | naive   |  58 |          -0.0504 |   -0.4954 |    0.6203 |   1.0000 |         0 | False              |
| XAUUSD   |         6 | har_ewma  | naive   |  58 |          -0.0401 |   -0.4129 |    0.6797 |   1.0000 |         0 | False              |
| XAUUSD   |        24 | ewma      | naive   |  59 |           0.0032 |    0.2124 |    0.8318 |   0.8318 |         0 | False              |
| XAUUSD   |        24 | har       | naive   |  58 |          -0.0563 |   -1.5905 |    0.1117 |   0.3352 |         0 | False              |
| XAUUSD   |        24 | har_ewma  | naive   |  58 |          -0.0344 |   -1.3612 |    0.1734 |   0.3469 |         0 | False              |
| XAUUSD   |        48 | ewma      | naive   |  59 |          -0.0429 |   -1.9955 |    0.0460 |   0.0920 |         0 | False              |
| XAUUSD   |        48 | har       | naive   |  58 |          -0.0922 |   -1.6389 |    0.1012 |   0.1012 |         0 | False              |
| XAUUSD   |        48 | har_ewma  | naive   |  58 |          -0.0981 |   -2.9198 |    0.0035 |   0.0105 |         0 | True               |

## Arm runtime

| arm | calls | successes | seconds |
|---|---|---|---|
| `ewma` | 522 | 522 | 0.0 |
| `har` | 522 | 513 | 0.2 |
| `har_ewma` | 522 | 513 | 0.3 |
| `naive` | 522 | 522 | 0.0 |
| `timesfm25` | 177 | 0 | 0.0 |

## Honesty notes

- Model arms were executed only if listed above under successes; `chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the :8002 server respectively. Synthetic runs validate METHODOLOGY, not financial skill.
- Coverage is the empirical 80% central-interval coverage; acceptance band [0.80, 0.95] (too narrow or too wide both fail).
- MAE on the raw scale is per-symbol only — do not average across symbols; cross-symbol comparison lives in qlike/mae_log.
- Nothing here is wired into the live orchestrator/EV/Kelly path. Phase 3/4 integration is gated on these results.
