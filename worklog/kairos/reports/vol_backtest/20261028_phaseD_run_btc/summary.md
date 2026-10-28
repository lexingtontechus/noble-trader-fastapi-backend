# Vol Forecast Backtest — Phase 0 (measured or nothing)

**Run:** 2026-10-28 03:15:23 · source: `csv:data/vol_backtest` · symbols: BTCUSD · horizons: [6, 12, 24, 48] · wall: 174.0s

Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC (overlap-aware) + Holm. Per-symbol rows — not pooled.

## Acceptance criteria check (thesis §benchmark, corrected nulls)

| criterion | verdict per symbol (see dm_tests.csv for stats) |
|---|---|
| H=6 · BTCUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.2067 dir=0.609 (null=0.399) cov80=0.433 | DM-beats-HAR✗<br>`gjr`: qlike=0.2145 dir=0.584 (null=0.399) cov80=0.425 | DM-beats-HAR✗ |
| H=12 · BTCUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1704 dir=0.618 (null=0.382) cov80=0.318 | DM-beats-HAR✗<br>`gjr`: qlike=0.1761 dir=0.635 (null=0.382) cov80=0.322 | DM-beats-HAR✗ |
| H=24 · BTCUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1486 dir=0.618 (null=0.361) cov80=0.215 | DM-beats-HAR✗<br>`gjr`: qlike=0.1540 dir=0.588 (null=0.361) cov80=0.236 | DM-beats-HAR✗ |
| H=48 · BTCUSD — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1624 dir=0.547 (null=0.371) cov80=0.091 | DM-beats-HAR✗<br>`gjr`: qlike=0.1967 dir=0.513 (null=0.371) cov80=0.082 | DM-beats-HAR✗ |

## Metrics (per symbol × horizon × arm)

### horizon = 6 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | egarch | 233 |  0.2067 |    0.5889 |    0.6094 |                 0.3991 |       0.4335 |
| BTCUSD   | garch  | 233 |  0.2005 |    0.5652 |    0.6009 |                 0.3991 |       0.4378 |
| BTCUSD   | gjr    | 233 |  0.2145 |    0.5951 |    0.5837 |                 0.3991 |       0.4249 |

### horizon = 12 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | egarch | 233 |  0.1704 |    0.5432 |    0.6180 |                 0.3820 |       0.3176 |
| BTCUSD   | garch  | 233 |  0.1558 |    0.5053 |    0.6609 |                 0.3820 |       0.3433 |
| BTCUSD   | gjr    | 233 |  0.1761 |    0.5505 |    0.6352 |                 0.3820 |       0.3219 |

### horizon = 24 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | egarch | 233 |  0.1486 |    0.5072 |    0.6180 |                 0.3605 |       0.2146 |
| BTCUSD   | garch  | 233 |  0.1229 |    0.4338 |    0.6137 |                 0.3605 |       0.3476 |
| BTCUSD   | gjr    | 233 |  0.1540 |    0.5146 |    0.5880 |                 0.3605 |       0.2361 |

### horizon = 48 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | egarch | 232 |  0.1624 |    0.5595 |    0.5474 |                 0.3707 |       0.0905 |
| BTCUSD   | garch  | 232 |  0.1333 |    0.4847 |    0.5474 |                 0.3707 |       0.1422 |
| BTCUSD   | gjr    | 232 |  0.1967 |    0.6343 |    0.5129 |                 0.3707 |       0.0819 |

### QLIKE winners (lower = better)

| symbol   |   horizon | best_arm   |   qlike |   har_qlike |
|:---------|----------:|:-----------|--------:|------------:|
| BTCUSD   |         6 | garch      |  0.2005 |         nan |
| BTCUSD   |        12 | garch      |  0.1558 |         nan |
| BTCUSD   |        24 | garch      |  0.1229 |         nan |
| BTCUSD   |        48 | garch      |  0.1333 |         nan |

## Diebold-Mariano (QLIKE, HAC + Holm)

_(no DM rows)_
## Arm runtime

| arm | calls | successes | seconds |
|---|---|---|---|
| `egarch` | 931 | 931 | 88.5 |
| `garch` | 931 | 931 | 24.8 |
| `gjr` | 931 | 931 | 60.7 |

## Honesty notes

- Model arms were executed only if listed above under successes; `chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the :8002 server respectively. Synthetic runs validate METHODOLOGY, not financial skill.
- Coverage is the empirical 80% central-interval coverage; acceptance band [0.80, 0.95] (too narrow or too wide both fail).
- MAE on the raw scale is per-symbol only — do not average across symbols; cross-symbol comparison lives in qlike/mae_log.
- Nothing here is wired into the live orchestrator/EV/Kelly path. Phase 3/4 integration is gated on these results.
