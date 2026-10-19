# Phase 0.5 — RV forecast recalibration (MZ + spread k)

Generated 2026-10-19 22:12 UTC · 100 cells · params: train_frac=0.7, min_n_cell=60, min_n_pooled=80, shrink_n0=20.0, qlike_tol=0.1

Fit on TRAIN cutoffs, scored on LATER lockbox cutoffs (never both).
Acceptance: the thesis band [0.80, 0.95] as a ONE-SIDED Wilson test (reject only when the CI rules the band out) AND QLIKE not degraded > 10%.

## Lockbox verdict per cell

| symbol | H | arm | level | n_fit | n_test | b | k | cov raw→cal (test) | cov 95% CI | qlike raw→cal (test) | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSD | 24 | chronos2 | pooled_symbol_arm | 83 | 17 | 0.68 | 2.43 | 0.35→0.82 | [0.59, 0.94] | 0.0768→0.0541 | ACCEPT |
| BTCUSD | 48 | chronos2 | pooled_symbol_arm | 83 | 17 | 0.68 | 2.43 | 0.29→0.71 | [0.47, 0.87] | 0.1604→0.0964 | ACCEPT |
| USDCHF | 24 | chronos2 | pooled_symbol_arm | 81 | 17 | 0.73 | 2.44 | 0.35→0.82 | [0.59, 0.94] | 0.0491→0.0530 | ACCEPT |
| USDCHF | 48 | chronos2 | pooled_symbol_arm | 81 | 17 | 0.73 | 2.44 | 0.35→0.88 | [0.66, 0.97] | 0.0198→0.0246 | reject |
| USDJPY | 24 | chronos2 | pooled_symbol_arm | 80 | 17 | 0.60 | 2.68 | 0.29→0.59 | [0.36, 0.78] | 0.2465→0.2090 | reject |
| USDJPY | 48 | chronos2 | pooled_symbol_arm | 80 | 17 | 0.60 | 2.68 | 0.12→0.59 | [0.36, 0.78] | 0.1842→0.1434 | reject |
| XAGUSD | 24 | chronos2 | pooled_symbol_arm | 81 | 16 | 0.98 | 2.40 | 0.19→0.69 | [0.44, 0.86] | 0.0933→0.0838 | ACCEPT |
| XAGUSD | 48 | chronos2 | pooled_symbol_arm | 81 | 18 | 0.98 | 2.40 | 0.28→0.72 | [0.49, 0.88] | 0.0553→0.0424 | ACCEPT |
| XAUUSD | 24 | chronos2 | pooled_symbol_arm | 81 | 16 | 0.96 | 2.71 | 0.31→0.62 | [0.39, 0.81] | 0.1527→0.1303 | ACCEPT |
| XAUUSD | 48 | chronos2 | pooled_symbol_arm | 81 | 18 | 0.96 | 2.71 | 0.33→0.72 | [0.49, 0.88] | 0.0640→0.0493 | ACCEPT |
| BTCUSD | 24 | chronos2_cov | pooled_symbol_arm | 83 | 17 | 0.69 | 2.29 | 0.35→0.82 | [0.59, 0.94] | 0.0801→0.0523 | ACCEPT |
| BTCUSD | 48 | chronos2_cov | pooled_symbol_arm | 83 | 17 | 0.69 | 2.29 | 0.29→0.71 | [0.47, 0.87] | 0.1830→0.1050 | ACCEPT |
| USDCHF | 24 | chronos2_cov | pooled_symbol_arm | 81 | 17 | 0.68 | 2.64 | 0.41→0.94 | [0.73, 0.99] | 0.0440→0.0495 | reject |
| USDCHF | 48 | chronos2_cov | pooled_symbol_arm | 81 | 17 | 0.68 | 2.64 | 0.47→0.94 | [0.73, 0.99] | 0.0114→0.0185 | reject |
| USDJPY | 24 | chronos2_cov | pooled_symbol_arm | 80 | 17 | 0.60 | 2.65 | 0.47→0.71 | [0.47, 0.87] | 0.2299→0.1890 | ACCEPT |
| USDJPY | 48 | chronos2_cov | pooled_symbol_arm | 80 | 17 | 0.60 | 2.65 | 0.12→0.65 | [0.41, 0.83] | 0.1796→0.1347 | ACCEPT |
| XAGUSD | 24 | chronos2_cov | pooled_symbol_arm | 81 | 16 | 1.03 | 2.21 | 0.19→0.75 | [0.51, 0.90] | 0.0907→0.0775 | ACCEPT |
| XAGUSD | 48 | chronos2_cov | pooled_symbol_arm | 81 | 18 | 1.03 | 2.21 | 0.17→0.50 | [0.29, 0.71] | 0.0670→0.0492 | reject |
| XAUUSD | 24 | chronos2_cov | pooled_symbol_arm | 81 | 16 | 0.86 | 2.79 | 0.38→0.75 | [0.51, 0.90] | 0.1362→0.1068 | ACCEPT |
| XAUUSD | 48 | chronos2_cov | pooled_symbol_arm | 81 | 18 | 0.86 | 2.79 | 0.17→0.78 | [0.55, 0.91] | 0.0860→0.0689 | ACCEPT |
| BTCUSD | 6 | ewma | cell | 489 | 209 | 0.77 | 1.89 | 0.45→0.71 | [0.64, 0.77] | 0.2228→0.3188 | reject |
| BTCUSD | 24 | ewma | cell | 488 | 209 | 0.58 | 2.83 | 0.31→0.82 | [0.76, 0.86] | 0.1248→0.0972 | ACCEPT |
| BTCUSD | 48 | ewma | cell | 488 | 208 | 0.48 | 3.52 | 0.26→0.83 | [0.78, 0.88] | 0.1176→0.0674 | ACCEPT |
| BTCUSD | 72 | ewma | cell | 487 | 208 | 0.45 | 3.69 | 0.16→0.78 | [0.72, 0.83] | 0.1184→0.0566 | ACCEPT |
| USDCHF | 6 | ewma | cell | 481 | 205 | 0.40 | 5.77 | 0.50→0.78 | [0.72, 0.83] | 0.1743→1.0594 | reject |
| USDCHF | 24 | ewma | cell | 480 | 205 | 0.51 | 5.30 | 0.36→0.77 | [0.70, 0.82] | 0.0878→0.2438 | reject |
| USDCHF | 48 | ewma | cell | 479 | 205 | 0.45 | 7.55 | 0.23→0.76 | [0.70, 0.81] | 0.0750→0.2594 | reject |
| USDCHF | 72 | ewma | cell | 479 | 204 | 0.41 | 10.10 | 0.24→0.75 | [0.69, 0.80] | 0.0647→0.3093 | reject |
| USDJPY | 6 | ewma | cell | 480 | 205 | 0.67 | 1.74 | 0.52→0.74 | [0.68, 0.80] | 0.1534→0.1544 | reject |
| USDJPY | 24 | ewma | cell | 479 | 205 | 0.58 | 3.07 | 0.30→0.62 | [0.56, 0.69] | 0.1355→0.1445 | reject |
| USDJPY | 48 | ewma | cell | 479 | 204 | 0.53 | 4.66 | 0.24→0.58 | [0.51, 0.64] | 0.1403→0.1652 | reject |
| USDJPY | 72 | ewma | cell | 478 | 204 | 0.48 | 6.55 | 0.23→0.56 | [0.49, 0.63] | 0.1561→0.2056 | reject |
| XAGUSD | 6 | ewma | cell | 380 | 162 | 0.70 | 1.74 | 0.54→0.83 | [0.77, 0.88] | 0.1441→0.1772 | reject |
| XAGUSD | 24 | ewma | cell | 379 | 162 | 0.68 | 2.53 | 0.32→0.80 | [0.73, 0.86] | 0.0777→0.0837 | ACCEPT |
| XAGUSD | 48 | ewma | cell | 378 | 162 | 0.68 | 2.82 | 0.27→0.73 | [0.66, 0.80] | 0.0636→0.0641 | reject |
| XAGUSD | 72 | ewma | cell | 378 | 161 | 0.68 | 3.38 | 0.29→0.79 | [0.72, 0.84] | 0.0533→0.0515 | ACCEPT |
| XAUUSD | 6 | ewma | cell | 380 | 162 | 0.86 | 1.68 | 0.54→0.83 | [0.77, 0.88] | 0.1491→0.1729 | reject |
| XAUUSD | 24 | ewma | cell | 379 | 162 | 0.66 | 2.11 | 0.36→0.74 | [0.67, 0.80] | 0.0867→0.0864 | ACCEPT |
| XAUUSD | 48 | ewma | cell | 378 | 162 | 0.63 | 2.78 | 0.29→0.73 | [0.66, 0.79] | 0.0818→0.0762 | reject |
| XAUUSD | 72 | ewma | cell | 378 | 161 | 0.61 | 3.27 | 0.24→0.76 | [0.69, 0.82] | 0.0742→0.0668 | ACCEPT |
| BTCUSD | 6 | har | cell | 488 | 209 | 1.00 | 1.01 | 0.74→0.71 | [0.64, 0.77] | 0.2693→0.3206 | reject |
| BTCUSD | 24 | har | cell | 487 | 208 | 0.93 | 1.04 | 0.79→0.82 | [0.76, 0.86] | 0.1065→0.1049 | ACCEPT |
| BTCUSD | 48 | har | cell | 486 | 207 | 0.90 | 1.07 | 0.80→0.83 | [0.77, 0.87] | 0.0763→0.0742 | ACCEPT |
| BTCUSD | 72 | har | cell | 484 | 207 | 0.89 | 1.11 | 0.79→0.84 | [0.78, 0.88] | 0.0649→0.0626 | ACCEPT |
| USDCHF | 6 | har | cell | 480 | 205 | 0.81 | 1.02 | 0.80→0.81 | [0.75, 0.86] | 0.1419→0.1367 | ACCEPT |
| USDCHF | 24 | har | cell | 479 | 204 | 0.90 | 1.03 | 0.69→0.75 | [0.69, 0.80] | 0.0650→0.0657 | ACCEPT |
| USDCHF | 48 | har | cell | 477 | 204 | 0.80 | 1.01 | 0.71→0.72 | [0.66, 0.78] | 0.0470→0.0466 | reject |
| USDCHF | 72 | har | cell | 476 | 203 | 0.67 | 1.01 | 0.72→0.74 | [0.68, 0.80] | 0.0377→0.0363 | reject |
| USDJPY | 6 | har | cell | 479 | 205 | 1.02 | 1.02 | 0.80→0.80 | [0.74, 0.85] | 0.1639→0.1628 | ACCEPT |
| USDJPY | 24 | har | cell | 478 | 204 | 0.94 | 0.97 | 0.71→0.69 | [0.62, 0.75] | 0.1319→0.1307 | reject |
| USDJPY | 48 | har | cell | 476 | 204 | 0.88 | 1.05 | 0.69→0.72 | [0.65, 0.77] | 0.1240→0.1189 | reject |
| USDJPY | 72 | har | cell | 475 | 203 | 0.83 | 1.10 | 0.66→0.69 | [0.62, 0.74] | 0.1232→0.1145 | reject |
| XAGUSD | 6 | har | cell | 379 | 162 | 0.92 | 1.03 | 0.80→0.82 | [0.76, 0.87] | 0.1367→0.1387 | ACCEPT |
| XAGUSD | 24 | har | cell | 378 | 161 | 0.98 | 1.13 | 0.81→0.86 | [0.80, 0.90] | 0.0562→0.0561 | ACCEPT |
| XAGUSD | 48 | har | cell | 376 | 161 | 0.99 | 1.17 | 0.80→0.87 | [0.81, 0.91] | 0.0388→0.0391 | ACCEPT |
| XAGUSD | 72 | har | cell | 375 | 160 | 0.99 | 1.21 | 0.78→0.90 | [0.84, 0.94] | 0.0316→0.0324 | ACCEPT |
| XAUUSD | 6 | har | cell | 379 | 162 | 1.12 | 1.07 | 0.83→0.86 | [0.80, 0.90] | 0.1365→0.1337 | ACCEPT |
| XAUUSD | 24 | har | cell | 378 | 161 | 0.99 | 1.07 | 0.78→0.81 | [0.75, 0.87] | 0.0609→0.0606 | ACCEPT |
| XAUUSD | 48 | har | cell | 376 | 161 | 0.98 | 1.10 | 0.80→0.83 | [0.76, 0.88] | 0.0475→0.0475 | ACCEPT |
| XAUUSD | 72 | har | cell | 375 | 160 | 0.99 | 1.13 | 0.79→0.84 | [0.77, 0.89] | 0.0399→0.0406 | ACCEPT |
| BTCUSD | 6 | har_ewma | cell | 488 | 209 | 0.88 | 1.39 | 0.63→0.71 | [0.64, 0.77] | 0.2364→0.3238 | reject |
| BTCUSD | 24 | har_ewma | cell | 487 | 208 | 0.76 | 1.53 | 0.60→0.81 | [0.75, 0.85] | 0.1074→0.0996 | ACCEPT |
| BTCUSD | 48 | har_ewma | cell | 486 | 207 | 0.70 | 1.73 | 0.56→0.85 | [0.80, 0.89] | 0.0804→0.0690 | ACCEPT |
| BTCUSD | 72 | har_ewma | cell | 484 | 207 | 0.69 | 1.60 | 0.55→0.80 | [0.74, 0.85] | 0.0708→0.0578 | ACCEPT |
| USDCHF | 6 | har_ewma | cell | 480 | 205 | 0.54 | 2.74 | 0.70→0.76 | [0.69, 0.81] | 0.1524→0.4326 | reject |
| USDCHF | 24 | har_ewma | cell | 479 | 204 | 0.70 | 1.38 | 0.58→0.78 | [0.71, 0.83] | 0.0686→0.0645 | ACCEPT |
| USDCHF | 48 | har_ewma | cell | 477 | 204 | 0.67 | 1.41 | 0.54→0.72 | [0.66, 0.78] | 0.0509→0.0456 | reject |
| USDCHF | 72 | har_ewma | cell | 476 | 203 | 0.63 | 1.49 | 0.53→0.71 | [0.64, 0.77] | 0.0401→0.0347 | reject |
| USDJPY | 6 | har_ewma | cell | 479 | 205 | 0.82 | 1.31 | 0.63→0.78 | [0.72, 0.83] | 0.1463→0.1476 | ACCEPT |
| USDJPY | 24 | har_ewma | cell | 478 | 204 | 0.75 | 1.34 | 0.61→0.71 | [0.64, 0.76] | 0.1238→0.1135 | reject |
| USDJPY | 48 | har_ewma | cell | 476 | 204 | 0.70 | 1.55 | 0.53→0.73 | [0.67, 0.79] | 0.1205→0.1042 | reject |
| USDJPY | 72 | har_ewma | cell | 475 | 203 | 0.66 | 1.60 | 0.46→0.69 | [0.62, 0.75] | 0.1263→0.1037 | reject |
| XAGUSD | 6 | har_ewma | cell | 379 | 162 | 0.80 | 1.34 | 0.69→0.81 | [0.74, 0.86] | 0.1362→0.1565 | reject |
| XAGUSD | 24 | har_ewma | cell | 378 | 161 | 0.84 | 1.51 | 0.60→0.82 | [0.75, 0.87] | 0.0624→0.0644 | ACCEPT |
| XAGUSD | 48 | har_ewma | cell | 376 | 161 | 0.87 | 1.61 | 0.60→0.86 | [0.80, 0.91] | 0.0442→0.0429 | ACCEPT |
| XAGUSD | 72 | har_ewma | cell | 375 | 160 | 0.88 | 1.79 | 0.61→0.91 | [0.86, 0.95] | 0.0352→0.0332 | ACCEPT |
| XAUUSD | 6 | har_ewma | cell | 379 | 162 | 0.98 | 1.35 | 0.70→0.86 | [0.80, 0.90] | 0.1383→0.1536 | reject |
| XAUUSD | 24 | har_ewma | cell | 378 | 161 | 0.83 | 1.44 | 0.59→0.82 | [0.75, 0.87] | 0.0701→0.0707 | ACCEPT |
| XAUUSD | 48 | har_ewma | cell | 376 | 161 | 0.82 | 1.52 | 0.60→0.83 | [0.76, 0.88] | 0.0580→0.0565 | ACCEPT |
| XAUUSD | 72 | har_ewma | cell | 375 | 160 | 0.80 | 1.59 | 0.57→0.85 | [0.79, 0.90] | 0.0496→0.0475 | ACCEPT |
| BTCUSD | 6 | naive | cell | 489 | 209 | 0.56 | - | -→- | - | 0.3889→0.2023 | n/s |
| BTCUSD | 24 | naive | cell | 488 | 209 | 0.45 | - | -→- | - | 0.1716→0.1163 | n/s |
| BTCUSD | 48 | naive | cell | 488 | 208 | 0.46 | - | -→- | - | 0.1130→0.0708 | n/s |
| BTCUSD | 72 | naive | cell | 487 | 208 | 0.52 | - | -→- | - | 0.1025→0.0637 | n/s |
| USDCHF | 6 | naive | cell | 481 | 205 | 0.25 | - | -→- | - | 0.2625→1.8495 | n/s |
| USDCHF | 24 | naive | cell | 480 | 205 | 0.43 | - | -→- | - | 0.1023→0.4726 | n/s |
| USDCHF | 48 | naive | cell | 479 | 205 | 0.46 | - | -→- | - | 0.0735→0.2587 | n/s |
| USDCHF | 72 | naive | cell | 479 | 204 | 0.43 | - | -→- | - | 0.0552→0.2501 | n/s |
| USDJPY | 6 | naive | cell | 480 | 205 | 0.41 | - | -→- | - | 0.2381→1.0126 | n/s |
| USDJPY | 24 | naive | cell | 479 | 205 | 0.53 | - | -→- | - | 0.1629→0.2528 | n/s |
| USDJPY | 48 | naive | cell | 479 | 204 | 0.53 | - | -→- | - | 0.1510→0.1698 | n/s |
| USDJPY | 72 | naive | cell | 478 | 204 | 0.51 | - | -→- | - | 0.1599→0.1598 | n/s |
| XAGUSD | 6 | naive | cell | 380 | 162 | 0.37 | - | -→- | - | 0.3113→0.3035 | n/s |
| XAGUSD | 24 | naive | cell | 379 | 162 | 0.62 | - | -→- | - | 0.0909→0.0932 | n/s |
| XAGUSD | 48 | naive | cell | 378 | 162 | 0.73 | - | -→- | - | 0.0579→0.0587 | n/s |
| XAGUSD | 72 | naive | cell | 378 | 161 | 0.77 | - | -→- | - | 0.0369→0.0378 | n/s |
| XAUUSD | 6 | naive | cell | 380 | 162 | 0.41 | - | -→- | - | 0.2470→0.4374 | n/s |
| XAUUSD | 24 | naive | cell | 379 | 162 | 0.60 | - | -→- | - | 0.0955→0.0892 | n/s |
| XAUUSD | 48 | naive | cell | 378 | 162 | 0.67 | - | -→- | - | 0.0779→0.0732 | n/s |
| XAUUSD | 72 | naive | cell | 378 | 161 | 0.68 | - | -→- | - | 0.0517→0.0521 | n/s |

**46/80** scoreable cells accepted (20 not scoreable: no test rows, no claimed interval, or below the evidence ladder).

## Reading notes

- `b` < 1 → the center over-reacts; b > 1 → under-reacts (MZ slope).
- `k` is the spread inflation on the arm's OWN claimed σ_f; the calibrated interval is F'·exp(±Z80·k·σ_f) — log-symmetric by construction.
- `level` shows where the evidence came from: `cell` = per (symbol×horizon×arm); `pooled_symbol_arm`/`global_arm` = borrowed from a wider window (horizon-mixed for the center, σ_f-standardized for k); `none` = identity, no calibration.
- The verdict is a POINT estimate against the band [0.80, 0.95]. With small n_test a perfectly calibrated cell can land below 0.80 by sampling noise alone — read `cov 95% CI` next to it: 0.78 with CI [0.66, 0.87] is sampling noise; 0.55 is a real defect.
- Honest boundaries: per-regime k and per-quantile (asymmetric) mapping need more history than these runs carry; QLIKE remains the loss of record, so a calibrated arm that still loses calibrated-HAR on QLIKE/DM does not ship.
- Nothing here changes any live path. Consumption gate (scope doc §1.3 step 5): ONLY calibrated spreads feed Kelly vol clamp / brick sizing.
