# Phase 0.5 — RV forecast recalibration (MZ + spread k)

Generated 2026-10-28 03:15 UTC · 60 cells · params: train_frac=0.7, min_n_cell=60, min_n_pooled=80, shrink_n0=20.0, qlike_tol=0.1

Fit on TRAIN cutoffs, scored on LATER lockbox cutoffs (never both).
Acceptance: the thesis band [0.80, 0.95] as a ONE-SIDED Wilson test (reject only when the CI rules the band out) AND QLIKE not degraded > 10%.

## Lockbox verdict per cell

| symbol | H | arm | level | n_fit | n_test | b | k | cov raw→cal (test) | cov 95% CI | qlike raw→cal (test) | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | 6 | egarch | cell | 164 | 69 | 1.12 | 1.82 | 0.55→0.77 | [0.66, 0.85] | 0.2065→0.3615 | reject |
| BTCUSD | 12 | egarch | cell | 164 | 69 | 1.13 | 2.10 | 0.35→0.67 | [0.55, 0.77] | 0.1617→0.1950 | reject |
| BTCUSD | 24 | egarch | cell | 164 | 69 | 1.14 | 2.57 | 0.25→0.75 | [0.64, 0.84] | 0.1753→0.1008 | ACCEPT |
| BTCUSD | 48 | egarch | cell | 163 | 69 | 1.33 | 3.36 | 0.03→0.85 | [0.75, 0.92] | 0.1866→0.0667 | ACCEPT |
| USDCHF | 6 | egarch | cell | 161 | 68 | 1.13 | 1.73 | 0.37→0.79 | [0.68, 0.87] | 0.1868→0.1625 | ACCEPT |
| USDCHF | 12 | egarch | cell | 161 | 68 | 1.09 | 2.29 | 0.38→0.85 | [0.75, 0.92] | 0.1231→0.1029 | ACCEPT |
| USDCHF | 24 | egarch | cell | 161 | 68 | 0.97 | 2.56 | 0.22→0.84 | [0.73, 0.91] | 0.0908→0.0631 | ACCEPT |
| USDCHF | 48 | egarch | cell | 161 | 68 | 0.83 | 2.65 | 0.23→0.77 | [0.65, 0.85] | 0.0596→0.0370 | ACCEPT |
| USDJPY | 6 | egarch | cell | 161 | 68 | 0.96 | 1.64 | 0.47→0.75 | [0.64, 0.84] | 0.1987→0.1848 | ACCEPT |
| USDJPY | 12 | egarch | cell | 161 | 68 | 0.81 | 2.05 | 0.27→0.69 | [0.57, 0.79] | 0.1703→0.1511 | reject |
| USDJPY | 24 | egarch | cell | 160 | 68 | 0.78 | 2.46 | 0.19→0.69 | [0.57, 0.79] | 0.1575→0.1435 | reject |
| USDJPY | 48 | egarch | cell | 160 | 68 | 0.88 | 3.50 | 0.16→0.79 | [0.68, 0.87] | 0.1482→0.1099 | ACCEPT |
| XAGUSD | 6 | egarch | cell | 127 | 54 | 1.30 | 1.62 | 0.44→0.74 | [0.61, 0.84] | 0.1468→0.1497 | ACCEPT |
| XAGUSD | 12 | egarch | cell | 127 | 54 | 1.36 | 2.12 | 0.41→0.78 | [0.65, 0.87] | 0.1074→0.1114 | ACCEPT |
| XAGUSD | 24 | egarch | cell | 127 | 54 | 1.33 | 2.10 | 0.32→0.69 | [0.55, 0.79] | 0.0705→0.0742 | reject |
| XAGUSD | 48 | egarch | cell | 126 | 54 | 1.15 | 3.13 | 0.18→0.72 | [0.59, 0.82] | 0.0739→0.0515 | ACCEPT |
| XAUUSD | 6 | egarch | cell | 127 | 54 | 1.33 | 1.50 | 0.54→0.80 | [0.67, 0.88] | 0.1799→0.2025 | reject |
| XAUUSD | 12 | egarch | cell | 127 | 54 | 1.39 | 1.84 | 0.56→0.81 | [0.69, 0.90] | 0.1004→0.1185 | reject |
| XAUUSD | 24 | egarch | cell | 127 | 54 | 1.41 | 2.53 | 0.30→0.78 | [0.65, 0.87] | 0.0712→0.0845 | reject |
| XAUUSD | 48 | egarch | cell | 126 | 54 | 1.08 | 3.10 | 0.18→0.83 | [0.71, 0.91] | 0.0648→0.0587 | ACCEPT |
| BTCUSD | 6 | garch | cell | 164 | 69 | 1.04 | 1.86 | 0.54→0.77 | [0.66, 0.85] | 0.2215→0.3550 | reject |
| BTCUSD | 12 | garch | cell | 164 | 69 | 1.00 | 2.15 | 0.30→0.64 | [0.52, 0.74] | 0.1613→0.1933 | reject |
| BTCUSD | 24 | garch | cell | 164 | 69 | 0.91 | 2.57 | 0.30→0.75 | [0.64, 0.84] | 0.1466→0.1034 | ACCEPT |
| BTCUSD | 48 | garch | cell | 163 | 69 | 0.80 | 3.53 | 0.13→0.87 | [0.77, 0.93] | 0.1390→0.0670 | ACCEPT |
| USDCHF | 6 | garch | cell | 161 | 68 | 1.68 | 5.46 | 0.40→0.79 | [0.68, 0.87] | 0.1825→4.0242 | reject |
| USDCHF | 12 | garch | cell | 161 | 68 | 1.56 | 4.60 | 0.40→0.78 | [0.67, 0.86] | 0.1106→0.8597 | reject |
| USDCHF | 24 | garch | cell | 161 | 68 | 0.94 | 2.68 | 0.22→0.85 | [0.75, 0.92] | 0.0759→0.0635 | ACCEPT |
| USDCHF | 48 | garch | cell | 161 | 68 | 0.78 | 2.81 | 0.29→0.79 | [0.68, 0.87] | 0.0461→0.0372 | ACCEPT |
| USDJPY | 6 | garch | cell | 161 | 68 | 0.93 | 1.77 | 0.37→0.69 | [0.57, 0.79] | 0.2423→0.1934 | reject |
| USDJPY | 12 | garch | cell | 161 | 68 | 0.76 | 2.10 | 0.23→0.62 | [0.50, 0.72] | 0.1987→0.1588 | reject |
| USDJPY | 24 | garch | cell | 160 | 68 | 0.52 | 5.23 | 0.19→0.52 | [0.40, 0.63] | 0.1850→0.3810 | reject |
| USDJPY | 48 | garch | cell | 160 | 68 | 0.58 | 4.12 | 0.18→0.57 | [0.46, 0.68] | 0.1622→0.1629 | reject |
| XAGUSD | 6 | garch | cell | 127 | 54 | 1.15 | 1.70 | 0.43→0.76 | [0.63, 0.85] | 0.1408→0.1823 | reject |
| XAGUSD | 12 | garch | cell | 127 | 54 | 1.20 | 2.19 | 0.44→0.80 | [0.67, 0.88] | 0.1004→0.1284 | reject |
| XAGUSD | 24 | garch | cell | 127 | 54 | 1.27 | 2.09 | 0.41→0.57 | [0.44, 0.70] | 0.0708→0.0953 | reject |
| XAGUSD | 48 | garch | cell | 126 | 54 | 1.18 | 3.14 | 0.26→0.78 | [0.65, 0.87] | 0.0479→0.0576 | reject |
| XAUUSD | 6 | garch | cell | 127 | 54 | 1.20 | 1.48 | 0.52→0.81 | [0.69, 0.90] | 0.1800→0.2026 | reject |
| XAUUSD | 12 | garch | cell | 127 | 54 | 1.22 | 1.75 | 0.54→0.78 | [0.65, 0.87] | 0.1034→0.1229 | reject |
| XAUUSD | 24 | garch | cell | 127 | 54 | 1.30 | 2.12 | 0.46→0.69 | [0.55, 0.79] | 0.0762→0.0897 | reject |
| XAUUSD | 48 | garch | cell | 126 | 54 | 1.09 | 2.68 | 0.35→0.72 | [0.59, 0.82] | 0.0653→0.0707 | ACCEPT |
| BTCUSD | 6 | gjr | cell | 164 | 69 | 0.99 | 1.89 | 0.52→0.77 | [0.66, 0.85] | 0.2241→0.3783 | reject |
| BTCUSD | 12 | gjr | cell | 164 | 69 | 0.91 | 2.23 | 0.29→0.65 | [0.53, 0.75] | 0.1679→0.2030 | reject |
| BTCUSD | 24 | gjr | cell | 164 | 69 | 0.84 | 2.61 | 0.29→0.78 | [0.67, 0.86] | 0.1711→0.1047 | ACCEPT |
| BTCUSD | 48 | gjr | cell | 163 | 69 | 0.77 | 3.51 | 0.03→0.85 | [0.75, 0.92] | 0.1970→0.0673 | ACCEPT |
| USDCHF | 6 | gjr | cell | 161 | 68 | 0.98 | 1.76 | 0.38→0.81 | [0.70, 0.89] | 0.1908→0.1650 | ACCEPT |
| USDCHF | 12 | gjr | cell | 161 | 68 | 0.88 | 2.31 | 0.35→0.87 | [0.77, 0.93] | 0.1284→0.1080 | ACCEPT |
| USDCHF | 24 | gjr | cell | 161 | 68 | 0.86 | 2.53 | 0.18→0.79 | [0.68, 0.87] | 0.0980→0.0678 | ACCEPT |
| USDCHF | 48 | gjr | cell | 161 | 68 | 0.77 | 2.73 | 0.19→0.75 | [0.64, 0.84] | 0.0688→0.0427 | ACCEPT |
| USDJPY | 6 | gjr | cell | 161 | 68 | 0.83 | 1.68 | 0.38→0.73 | [0.62, 0.83] | 0.2169→0.1890 | ACCEPT |
| USDJPY | 12 | gjr | cell | 161 | 68 | 0.66 | 2.22 | 0.25→0.60 | [0.48, 0.71] | 0.1832→0.1695 | reject |
| USDJPY | 24 | gjr | cell | 160 | 68 | 0.56 | 4.18 | 0.21→0.56 | [0.44, 0.67] | 0.1728→0.2592 | reject |
| USDJPY | 48 | gjr | cell | 160 | 68 | 0.53 | 5.34 | 0.18→0.48 | [0.37, 0.60] | 0.1625→0.2478 | reject |
| XAGUSD | 6 | gjr | cell | 127 | 54 | 1.26 | 1.65 | 0.46→0.78 | [0.65, 0.87] | 0.1383→0.2023 | reject |
| XAGUSD | 12 | gjr | cell | 127 | 54 | 1.29 | 2.15 | 0.39→0.78 | [0.65, 0.87] | 0.1007→0.1647 | reject |
| XAGUSD | 24 | gjr | cell | 127 | 54 | 1.31 | 2.07 | 0.35→0.59 | [0.46, 0.71] | 0.0648→0.1434 | reject |
| XAGUSD | 48 | gjr | cell | 126 | 54 | 1.16 | 2.98 | 0.15→0.69 | [0.55, 0.79] | 0.0455→0.0830 | reject |
| XAUUSD | 6 | gjr | cell | 127 | 54 | 1.12 | 1.52 | 0.52→0.74 | [0.61, 0.84] | 0.1816→0.2237 | reject |
| XAUUSD | 12 | gjr | cell | 127 | 54 | 1.15 | 1.85 | 0.54→0.81 | [0.69, 0.90] | 0.1028→0.1510 | reject |
| XAUUSD | 24 | gjr | cell | 127 | 54 | 1.15 | 2.50 | 0.50→0.83 | [0.71, 0.91] | 0.0731→0.1093 | reject |
| XAUUSD | 48 | gjr | cell | 126 | 54 | 0.82 | 3.31 | 0.26→0.80 | [0.67, 0.88] | 0.0614→0.0988 | reject |

**24/60** scoreable cells accepted (0 not scoreable: no test rows, no claimed interval, or below the evidence ladder).

## Reading notes

- `b` < 1 → the center over-reacts; b > 1 → under-reacts (MZ slope).
- `k` is the spread inflation on the arm's OWN claimed σ_f; the calibrated interval is F'·exp(±Z80·k·σ_f) — log-symmetric by construction.
- `level` shows where the evidence came from: `cell` = per (symbol×horizon×arm); `pooled_symbol_arm`/`global_arm` = borrowed from a wider window (horizon-mixed for the center, σ_f-standardized for k); `none` = identity, no calibration.
- The verdict is a POINT estimate against the band [0.80, 0.95]. With small n_test a perfectly calibrated cell can land below 0.80 by sampling noise alone — read `cov 95% CI` next to it: 0.78 with CI [0.66, 0.87] is sampling noise; 0.55 is a real defect.
- Honest boundaries: per-regime k and per-quantile (asymmetric) mapping need more history than these runs carry; QLIKE remains the loss of record, so a calibrated arm that still loses calibrated-HAR on QLIKE/DM does not ship.
- Nothing here changes any live path. Consumption gate (scope doc §1.3 step 5): ONLY calibrated spreads feed Kelly vol clamp / brick sizing.
