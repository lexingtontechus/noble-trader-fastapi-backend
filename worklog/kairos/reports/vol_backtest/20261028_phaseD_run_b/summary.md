# Vol Forecast Backtest — Phase 0 (measured or nothing)

**Run:** 2026-10-28 03:11:56 · source: `csv:data/vol_backtest` · symbols: USDJPY, USDCHF · horizons: [6, 12, 24, 48] · wall: 511.0s

Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC (overlap-aware) + Holm. Per-symbol rows — not pooled.

## Acceptance criteria check (thesis §benchmark, corrected nulls)

| criterion | verdict per symbol (see dm_tests.csv for stats) |
|---|---|
| H=6 · USDJPY — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1898 dir=0.638 (null=0.406) cov80=0.459 | DM-beats-HAR✗<br>`gjr`: qlike=0.2017 dir=0.642 (null=0.406) cov80=0.445 | DM-beats-HAR✗ |
| H=6 · USDCHF — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1741 dir=0.664 (null=0.376) cov80=0.402 | DM-beats-HAR✗<br>`gjr`: qlike=0.1798 dir=0.633 (null=0.376) cov80=0.397 | DM-beats-HAR✗ |
| H=12 · USDJPY — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1455 dir=0.559 (null=0.376) cov80=0.345 | DM-beats-HAR✗<br>`gjr`: qlike=0.1548 dir=0.563 (null=0.376) cov80=0.376 | DM-beats-HAR✗ |
| H=12 · USDCHF — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1431 dir=0.594 (null=0.345) cov80=0.319 | DM-beats-HAR✗<br>`gjr`: qlike=0.1563 dir=0.563 (null=0.345) cov80=0.293 | DM-beats-HAR✗ |
| H=24 · USDJPY — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1401 dir=0.566 (null=0.351) cov80=0.246 | DM-beats-HAR✗<br>`gjr`: qlike=0.1502 dir=0.566 (null=0.351) cov80=0.307 | DM-beats-HAR✗ |
| H=24 · USDCHF — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1010 dir=0.541 (null=0.341) cov80=0.231 | DM-beats-HAR✗<br>`gjr`: qlike=0.1202 dir=0.541 (null=0.341) cov80=0.210 | DM-beats-HAR✗ |
| H=48 · USDJPY — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.1287 dir=0.504 (null=0.325) cov80=0.127 | DM-beats-HAR✗<br>`gjr`: qlike=0.1310 dir=0.544 (null=0.325) cov80=0.197 | DM-beats-HAR✗ |
| H=48 · USDCHF — MAE vs naive: nan (raw scale; use qlike+DM for decisions) | `egarch`: qlike=0.0814 dir=0.624 (null=0.354) cov80=0.218 | DM-beats-HAR✗<br>`gjr`: qlike=0.1075 dir=0.607 (null=0.354) cov80=0.140 | DM-beats-HAR✗ |

## Metrics (per symbol × horizon × arm)

### horizon = 6 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| USDCHF   | egarch | 229 |  0.1741 |    0.5453 |    0.6638 |                 0.3755 |       0.4017 |
| USDCHF   | garch  | 229 |  0.1635 |    0.5125 |    0.6769 |                 0.3755 |       0.4323 |
| USDCHF   | gjr    | 229 |  0.1798 |    0.5590 |    0.6332 |                 0.3755 |       0.3974 |
| USDJPY   | egarch | 229 |  0.1898 |    0.5449 |    0.6376 |                 0.4061 |       0.4585 |
| USDJPY   | garch  | 229 |  0.2162 |    0.6023 |    0.6463 |                 0.4061 |       0.3843 |
| USDJPY   | gjr    | 229 |  0.2017 |    0.5555 |    0.6419 |                 0.4061 |       0.4454 |

### horizon = 12 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| USDCHF   | egarch | 229 |  0.1431 |    0.4944 |    0.5939 |                 0.3450 |       0.3188 |
| USDCHF   | garch  | 229 |  0.1234 |    0.4353 |    0.6376 |                 0.3450 |       0.3843 |
| USDCHF   | gjr    | 229 |  0.1563 |    0.5299 |    0.5633 |                 0.3450 |       0.2926 |
| USDJPY   | egarch | 229 |  0.1455 |    0.4643 |    0.5590 |                 0.3755 |       0.3450 |
| USDJPY   | garch  | 229 |  0.1645 |    0.5152 |    0.5546 |                 0.3755 |       0.3100 |
| USDJPY   | gjr    | 229 |  0.1548 |    0.4665 |    0.5633 |                 0.3755 |       0.3755 |

### horizon = 24 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| USDCHF   | egarch | 229 |  0.1010 |    0.4127 |    0.5415 |                 0.3406 |       0.2314 |
| USDCHF   | garch  | 229 |  0.0841 |    0.3563 |    0.5852 |                 0.3406 |       0.2926 |
| USDCHF   | gjr    | 229 |  0.1202 |    0.4648 |    0.5415 |                 0.3406 |       0.2096 |
| USDJPY   | egarch | 228 |  0.1401 |    0.4419 |    0.5658 |                 0.3509 |       0.2456 |
| USDJPY   | garch  | 228 |  0.1667 |    0.4732 |    0.5833 |                 0.3509 |       0.2281 |
| USDJPY   | gjr    | 228 |  0.1502 |    0.4352 |    0.5658 |                 0.3509 |       0.3070 |

### horizon = 48 bars

| symbol   | arm    |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:-------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| USDCHF   | egarch | 229 |  0.0814 |    0.3603 |    0.6245 |                 0.3537 |       0.2183 |
| USDCHF   | garch  | 229 |  0.0651 |    0.2973 |    0.6550 |                 0.3537 |       0.2445 |
| USDCHF   | gjr    | 229 |  0.1075 |    0.4370 |    0.6070 |                 0.3537 |       0.1397 |
| USDJPY   | egarch | 228 |  0.1287 |    0.4710 |    0.5044 |                 0.3246 |       0.1272 |
| USDJPY   | garch  | 228 |  0.1347 |    0.4322 |    0.5746 |                 0.3246 |       0.1974 |
| USDJPY   | gjr    | 228 |  0.1310 |    0.4411 |    0.5439 |                 0.3246 |       0.1974 |

### QLIKE winners (lower = better)

| symbol   |   horizon | best_arm   |   qlike |   har_qlike |
|:---------|----------:|:-----------|--------:|------------:|
| USDCHF   |         6 | garch      |  0.1635 |         nan |
| USDCHF   |        12 | garch      |  0.1234 |         nan |
| USDCHF   |        24 | garch      |  0.0841 |         nan |
| USDCHF   |        48 | garch      |  0.0651 |         nan |
| USDJPY   |         6 | egarch     |  0.1898 |         nan |
| USDJPY   |        12 | egarch     |  0.1455 |         nan |
| USDJPY   |        24 | egarch     |  0.1401 |         nan |
| USDJPY   |        48 | egarch     |  0.1287 |         nan |

## Diebold-Mariano (QLIKE, HAC + Holm)

_(no DM rows)_
## Arm runtime

| arm | calls | successes | seconds |
|---|---|---|---|
| `egarch` | 1830 | 1830 | 245.4 |
| `garch` | 1830 | 1830 | 78.6 |
| `gjr` | 1830 | 1830 | 186.8 |

## Honesty notes

- Model arms were executed only if listed above under successes; `chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the :8002 server respectively. Synthetic runs validate METHODOLOGY, not financial skill.
- Coverage is the empirical 80% central-interval coverage; acceptance band [0.80, 0.95] (too narrow or too wide both fail).
- MAE on the raw scale is per-symbol only — do not average across symbols; cross-symbol comparison lives in qlike/mae_log.
- Nothing here is wired into the live orchestrator/EV/Kelly path. Phase 3/4 integration is gated on these results.
