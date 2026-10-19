# Vol Forecast Backtest — Phase 0 (measured or nothing)

**Run:** 2026-10-19 01:10:03 · source: `yfinance(hourly)` · symbols: BTCUSD, XAGUSD, XAUUSD, USDJPY, USDCHF · horizons: [24, 48] · wall: 453.0s

Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC (overlap-aware) + Holm. Per-symbol rows — not pooled.

## Acceptance criteria check (thesis §benchmark, corrected nulls)

| criterion | verdict per symbol (see dm_tests.csv for stats) |
|---|---|
| H=24 · BTCUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.0831 dir=0.763 (null=0.373) cov80=0.339 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.0811 dir=0.831 (null=0.373) cov80=0.390 | DM-beats-HAR✗ |
| H=24 · XAGUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.1135 dir=0.618 (null=0.364) cov80=0.345 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.1041 dir=0.709 (null=0.364) cov80=0.291 | DM-beats-HAR✗ |
| H=24 · XAUUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.1262 dir=0.691 (null=0.345) cov80=0.364 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.1464 dir=0.673 (null=0.345) cov80=0.418 | DM-beats-HAR✗ |
| H=24 · USDJPY — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.1709 dir=0.684 (null=0.281) cov80=0.298 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.1608 dir=0.684 (null=0.281) cov80=0.351 | DM-beats-HAR✗ |
| H=24 · USDCHF — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.0627 dir=0.672 (null=0.379) cov80=0.379 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.0608 dir=0.672 (null=0.379) cov80=0.414 | DM-beats-HAR✗ |
| H=48 · BTCUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.0999 dir=0.586 (null=0.431) cov80=0.276 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.1047 dir=0.569 (null=0.431) cov80=0.190 | DM-beats-HAR✗ |
| H=48 · XAGUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.0688 dir=0.600 (null=0.417) cov80=0.317 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.0687 dir=0.633 (null=0.417) cov80=0.317 | DM-beats-HAR✗ |
| H=48 · XAUUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.0835 dir=0.700 (null=0.517) cov80=0.367 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.1085 dir=0.717 (null=0.517) cov80=0.333 | DM-beats-HAR✗ |
| H=48 · USDJPY — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.1241 dir=0.684 (null=0.474) cov80=0.228 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.1164 dir=0.632 (null=0.474) cov80=0.246 | DM-beats-HAR✗ |
| H=48 · USDCHF — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `chronos2`: qlike=0.0527 dir=0.702 (null=0.421) cov80=0.246 | DM-beats-HAR✗<br>`chronos2_cov`: qlike=0.0546 dir=0.667 (null=0.421) cov80=0.368 | DM-beats-HAR✗ |

## Metrics (per symbol × horizon × arm)

### horizon = 24 bars

| symbol   | arm          |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | chronos2     |  59 |  0.0831 |    0.3139 |    0.7627 |                 0.3729 |       0.3390 |
| BTCUSD   | chronos2_cov |  59 |  0.0811 |    0.3060 |    0.8305 |                 0.3729 |       0.3898 |
| BTCUSD   | har          |  58 |  0.0984 |    0.3373 |    0.6207 |                 0.3729 |       0.8103 |
| USDCHF   | chronos2     |  58 |  0.0627 |    0.2782 |    0.6724 |                 0.3793 |       0.3793 |
| USDCHF   | chronos2_cov |  58 |  0.0608 |    0.2754 |    0.6724 |                 0.3793 |       0.4138 |
| USDCHF   | har          |  57 |  0.0616 |    0.2740 |    0.7193 |                 0.3793 |       0.7719 |
| USDJPY   | chronos2     |  57 |  0.1709 |    0.3842 |    0.6842 |                 0.2807 |       0.2982 |
| USDJPY   | chronos2_cov |  57 |  0.1608 |    0.3564 |    0.6842 |                 0.2807 |       0.3509 |
| USDJPY   | har          |  56 |  0.1201 |    0.3313 |    0.7500 |                 0.2807 |       0.7679 |
| XAGUSD   | chronos2     |  55 |  0.1135 |    0.3395 |    0.6182 |                 0.3636 |       0.3455 |
| XAGUSD   | chronos2_cov |  55 |  0.1041 |    0.3188 |    0.7091 |                 0.3636 |       0.2909 |
| XAGUSD   | har          |  54 |  0.0782 |    0.2801 |    0.7222 |                 0.3636 |       0.7963 |
| XAUUSD   | chronos2     |  55 |  0.1262 |    0.3474 |    0.6909 |                 0.3455 |       0.3636 |
| XAUUSD   | chronos2_cov |  55 |  0.1464 |    0.3462 |    0.6727 |                 0.3455 |       0.4182 |
| XAUUSD   | har          |  54 |  0.1080 |    0.3429 |    0.6852 |                 0.3455 |       0.6667 |

### horizon = 48 bars

| symbol   | arm          |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | chronos2     |  58 |  0.0999 |    0.3017 |    0.5862 |                 0.4310 |       0.2759 |
| BTCUSD   | chronos2_cov |  58 |  0.1047 |    0.3204 |    0.5690 |                 0.4310 |       0.1897 |
| BTCUSD   | har          |  57 |  0.0606 |    0.2539 |    0.7368 |                 0.4310 |       0.8246 |
| USDCHF   | chronos2     |  57 |  0.0527 |    0.2448 |    0.7018 |                 0.4211 |       0.2456 |
| USDCHF   | chronos2_cov |  57 |  0.0546 |    0.2391 |    0.6667 |                 0.4211 |       0.3684 |
| USDCHF   | har          |  56 |  0.0394 |    0.2155 |    0.7500 |                 0.4211 |       0.7500 |
| USDJPY   | chronos2     |  57 |  0.1241 |    0.3341 |    0.6842 |                 0.4737 |       0.2281 |
| USDJPY   | chronos2_cov |  57 |  0.1164 |    0.3110 |    0.6316 |                 0.4737 |       0.2456 |
| USDJPY   | har          |  56 |  0.0833 |    0.2961 |    0.6607 |                 0.4737 |       0.7679 |
| XAGUSD   | chronos2     |  60 |  0.0688 |    0.2534 |    0.6000 |                 0.4167 |       0.3167 |
| XAGUSD   | chronos2_cov |  60 |  0.0687 |    0.2568 |    0.6333 |                 0.4167 |       0.3167 |
| XAGUSD   | har          |  59 |  0.0589 |    0.2418 |    0.6780 |                 0.4167 |       0.7797 |
| XAUUSD   | chronos2     |  60 |  0.0835 |    0.2634 |    0.7000 |                 0.5167 |       0.3667 |
| XAUUSD   | chronos2_cov |  60 |  0.1085 |    0.2904 |    0.7167 |                 0.5167 |       0.3333 |
| XAUUSD   | har          |  59 |  0.0871 |    0.2752 |    0.5593 |                 0.5167 |       0.6780 |

### QLIKE winners (lower = better)

| symbol   |   horizon | best_arm     |   qlike |   har_qlike |
|:---------|----------:|:-------------|--------:|------------:|
| BTCUSD   |        24 | chronos2_cov |  0.0811 |      0.0984 |
| BTCUSD   |        48 | har          |  0.0606 |      0.0606 |
| USDCHF   |        24 | chronos2_cov |  0.0608 |      0.0616 |
| USDCHF   |        48 | har          |  0.0394 |      0.0394 |
| USDJPY   |        24 | har          |  0.1201 |      0.1201 |
| USDJPY   |        48 | har          |  0.0833 |      0.0833 |
| XAGUSD   |        24 | har          |  0.0782 |      0.0782 |
| XAGUSD   |        48 | har          |  0.0589 |      0.0589 |
| XAUUSD   |        24 | har          |  0.1080 |      0.1080 |
| XAUUSD   |        48 | chronos2     |  0.0835 |      0.0871 |

## Diebold-Mariano (QLIKE, HAC + Holm)

#### vs `har`

| symbol   |   horizon | arm_a        | arm_b   |   n |   mean_loss_diff |   dm_stat |   p_value |   p_holm |   hac_lag | significant_holm   |
|:---------|----------:|:-------------|:--------|----:|-----------------:|----------:|----------:|---------:|----------:|:-------------------|
| BTCUSD   |        24 | chronos2     | har     |  58 |          -0.0155 |   -0.9566 |    0.3387 |   0.6054 |         0 | False              |
| BTCUSD   |        24 | chronos2_cov | har     |  58 |          -0.0172 |   -1.0307 |    0.3027 |   0.6054 |         0 | False              |
| BTCUSD   |        48 | chronos2     | har     |  57 |           0.0382 |    2.0095 |    0.0445 |   0.0835 |         0 | False              |
| BTCUSD   |        48 | chronos2_cov | har     |  57 |           0.0433 |    2.0362 |    0.0417 |   0.0835 |         0 | False              |
| USDCHF   |        24 | chronos2     | har     |  57 |          -0.0046 |   -0.5179 |    0.6045 |   1.0000 |         0 | False              |
| USDCHF   |        24 | chronos2_cov | har     |  57 |          -0.0067 |   -0.6268 |    0.5308 |   1.0000 |         0 | False              |
| USDCHF   |        48 | chronos2     | har     |  56 |           0.0115 |    1.1912 |    0.2336 |   0.4300 |         0 | False              |
| USDCHF   |        48 | chronos2_cov | har     |  56 |           0.0133 |    1.2400 |    0.2150 |   0.4300 |         0 | False              |
| USDJPY   |        24 | chronos2     | har     |  56 |           0.0456 |    2.4122 |    0.0159 |   0.0317 |         0 | False              |
| USDJPY   |        24 | chronos2_cov | har     |  56 |           0.0343 |    1.5898 |    0.1119 |   0.1119 |         0 | False              |
| USDJPY   |        48 | chronos2     | har     |  56 |           0.0397 |    2.0529 |    0.0401 |   0.0802 |         0 | False              |
| USDJPY   |        48 | chronos2_cov | har     |  56 |           0.0316 |    1.4744 |    0.1404 |   0.1404 |         0 | False              |
| XAGUSD   |        24 | chronos2     | har     |  54 |           0.0373 |    2.4659 |    0.0137 |   0.0273 |         0 | False              |
| XAGUSD   |        24 | chronos2_cov | har     |  54 |           0.0277 |    2.0513 |    0.0402 |   0.0402 |         0 | False              |
| XAGUSD   |        48 | chronos2     | har     |  59 |           0.0108 |    1.5224 |    0.1279 |   0.2558 |         0 | False              |
| XAGUSD   |        48 | chronos2_cov | har     |  59 |           0.0105 |    1.1536 |    0.2487 |   0.2558 |         0 | False              |
| XAUUSD   |        24 | chronos2     | har     |  54 |           0.0202 |    1.9329 |    0.0533 |   0.1065 |         0 | False              |
| XAUUSD   |        24 | chronos2_cov | har     |  54 |           0.0404 |    1.8396 |    0.0658 |   0.1065 |         0 | False              |
| XAUUSD   |        48 | chronos2     | har     |  59 |          -0.0029 |   -0.4295 |    0.6675 |   0.6675 |         0 | False              |
| XAUUSD   |        48 | chronos2_cov | har     |  59 |           0.0219 |    1.7453 |    0.0809 |   0.1619 |         0 | False              |

## Arm runtime

| arm | calls | successes | seconds |
|---|---|---|---|
| `chronos2` | 576 | 576 | 91.1 |
| `chronos2_cov` | 576 | 576 | 361.1 |
| `har` | 576 | 566 | 0.8 |

## Honesty notes

- Model arms were executed only if listed above under successes; `chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the :8002 server respectively. Synthetic runs validate METHODOLOGY, not financial skill.
- Coverage is the empirical 80% central-interval coverage; acceptance band [0.80, 0.95] (too narrow or too wide both fail).
- MAE on the raw scale is per-symbol only — do not average across symbols; cross-symbol comparison lives in qlike/mae_log.
- Nothing here is wired into the live orchestrator/EV/Kelly path. Phase 3/4 integration is gated on these results.
