# Vol Forecast Backtest — Phase 0 (measured or nothing)

**Run:** 2026-10-28 03:10:10 · source: `csv:data/vol_backtest` · symbols: XAUUSD, XAGUSD · horizons: [6, 12, 24, 48] · wall: 405.0s

Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC (overlap-aware) + Holm. Per-symbol rows — not pooled.

## Acceptance criteria check (thesis §benchmark, corrected nulls)

| criterion | verdict per symbol (see dm_tests.csv for stats) |
|---|---|
| H=6 · XAUUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1541 dir=0.680 (null=0.326) cov80=0.514 | DM-beats-HAR✗<br>`gjr`: qlike=0.1561 dir=0.685 (null=0.326) cov80=0.497 | DM-beats-HAR✗ |
| H=6 · XAGUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1853 dir=0.646 (null=0.365) cov80=0.442 | DM-beats-HAR✗<br>`gjr`: qlike=0.1866 dir=0.630 (null=0.365) cov80=0.398 | DM-beats-HAR✗ |
| H=12 · XAUUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1048 dir=0.608 (null=0.365) cov80=0.414 | DM-beats-HAR✗<br>`gjr`: qlike=0.1110 dir=0.630 (null=0.365) cov80=0.403 | DM-beats-HAR✗ |
| H=12 · XAGUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1454 dir=0.663 (null=0.409) cov80=0.381 | DM-beats-HAR✗<br>`gjr`: qlike=0.1541 dir=0.663 (null=0.409) cov80=0.331 | DM-beats-HAR✗ |
| H=24 · XAUUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.0794 dir=0.580 (null=0.320) cov80=0.249 | DM-beats-HAR✗<br>`gjr`: qlike=0.0849 dir=0.635 (null=0.320) cov80=0.293 | DM-beats-HAR✗ |
| H=24 · XAGUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1017 dir=0.613 (null=0.359) cov80=0.260 | DM-beats-HAR✗<br>`gjr`: qlike=0.1122 dir=0.635 (null=0.359) cov80=0.249 | DM-beats-HAR✗ |
| H=48 · XAUUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.0692 dir=0.522 (null=0.350) cov80=0.167 | DM-beats-HAR✗<br>`gjr`: qlike=0.0778 dir=0.550 (null=0.350) cov80=0.189 | DM-beats-HAR✗ |
| H=48 · XAGUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.0823 dir=0.578 (null=0.361) cov80=0.194 | DM-beats-HAR✗<br>`gjr`: qlike=0.0860 dir=0.567 (null=0.361) cov80=0.150 | DM-beats-HAR✗ |

## Metrics (per symbol × horizon × arm)

### horizon = 6 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| XAGUSD   | egarch | 181 |  0.1853 |    0.5509 |    0.6464 |                 0.3646 |       0.4420 |
| XAGUSD   | garch  | 181 |  0.1654 |    0.4869 |    0.6740 |                 0.3646 |       0.4586 |
| XAGUSD   | gjr    | 181 |  0.1866 |    0.5600 |    0.6298 |                 0.3646 |       0.3978 |
| XAUUSD   | egarch | 181 |  0.1541 |    0.4841 |    0.6796 |                 0.3260 |       0.5138 |
| XAUUSD   | garch  | 181 |  0.1377 |    0.4325 |    0.7127 |                 0.3260 |       0.5414 |
| XAUUSD   | gjr    | 181 |  0.1561 |    0.4886 |    0.6851 |                 0.3260 |       0.4972 |

### horizon = 12 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| XAGUSD   | egarch | 181 |  0.1454 |    0.4678 |    0.6630 |                 0.4088 |       0.3812 |
| XAGUSD   | garch  | 181 |  0.1312 |    0.4127 |    0.7293 |                 0.4088 |       0.4144 |
| XAGUSD   | gjr    | 181 |  0.1541 |    0.4908 |    0.6630 |                 0.4088 |       0.3315 |
| XAUUSD   | egarch | 181 |  0.1048 |    0.3976 |    0.6077 |                 0.3646 |       0.4144 |
| XAUUSD   | garch  | 181 |  0.0879 |    0.3370 |    0.6851 |                 0.3646 |       0.4696 |
| XAUUSD   | gjr    | 181 |  0.1110 |    0.4119 |    0.6298 |                 0.3646 |       0.4033 |

### horizon = 24 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| XAGUSD   | egarch | 181 |  0.1017 |    0.3966 |    0.6133 |                 0.3591 |       0.2597 |
| XAGUSD   | garch  | 181 |  0.0886 |    0.3349 |    0.7127 |                 0.3591 |       0.3204 |
| XAGUSD   | gjr    | 181 |  0.1122 |    0.4233 |    0.6354 |                 0.3591 |       0.2486 |
| XAUUSD   | egarch | 181 |  0.0794 |    0.3507 |    0.5801 |                 0.3204 |       0.2486 |
| XAUUSD   | garch  | 181 |  0.0674 |    0.2860 |    0.6464 |                 0.3204 |       0.3923 |
| XAUUSD   | gjr    | 181 |  0.0849 |    0.3622 |    0.6354 |                 0.3204 |       0.2928 |

### horizon = 48 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| XAGUSD   | egarch | 180 |  0.0823 |    0.3651 |    0.5778 |                 0.3611 |       0.1944 |
| XAGUSD   | garch  | 180 |  0.0653 |    0.2904 |    0.6444 |                 0.3611 |       0.2333 |
| XAGUSD   | gjr    | 180 |  0.0860 |    0.3778 |    0.5667 |                 0.3611 |       0.1500 |
| XAUUSD   | egarch | 180 |  0.0692 |    0.3332 |    0.5222 |                 0.3500 |       0.1667 |
| XAUUSD   | garch  | 180 |  0.0586 |    0.2648 |    0.5889 |                 0.3500 |       0.2944 |
| XAUUSD   | gjr    | 180 |  0.0778 |    0.3458 |    0.5500 |                 0.3500 |       0.1889 |

### QLIKE winners (lower = better)

| symbol   |   horizon | best_arm   |   qlike |   har_qlike |
|:---------|----------:|:-----------|--------:|------------:|
| XAGUSD   |         6 | garch      |  0.1654 |         nan |
| XAGUSD   |        12 | garch      |  0.1312 |         nan |
| XAGUSD   |        24 | garch      |  0.0886 |         nan |
| XAGUSD   |        48 | garch      |  0.0653 |         nan |
| XAUUSD   |         6 | garch      |  0.1377 |         nan |
| XAUUSD   |        12 | garch      |  0.0879 |         nan |
| XAUUSD   |        24 | garch      |  0.0674 |         nan |
| XAUUSD   |        48 | garch      |  0.0586 |         nan |

## Diebold-Mariano (QLIKE, HAC + Holm)

_(no DM rows)_
## Arm runtime

| arm | calls | successes | seconds |
|---|---|---|---|
| `egarch` | 1446 | 1446 | 179.9 |
| `garch` | 1446 | 1446 | 72.4 |
| `gjr` | 1446 | 1446 | 152.5 |

## Honesty notes

- Model arms were executed only if listed above under successes; `chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the :8002 server respectively. Synthetic runs validate METHODOLOGY, not financial skill.
- Coverage is the empirical 80% central-interval coverage; acceptance band [0.80, 0.95] (too narrow or too wide both fail).
- MAE on the raw scale is per-symbol only — do not average across symbols; cross-symbol comparison lives in qlike/mae_log.
- Nothing here is wired into the live orchestrator/EV/Kelly path. Phase 3/4 integration is gated on these results.
