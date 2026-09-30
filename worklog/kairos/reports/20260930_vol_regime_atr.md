# Vol-regime ATR solve — BTCUSD / XAUUSD

2026-08-14 → 2026-09-28 · wr∈[0.40,0.60] · exp≤0.20 · cost 0.1 · both halves EV>0

Entry features are live-gateable (past bars only). Post-absmax is the
aggressive-move hypothesis check only.

## Synthesis (do not wait for 10/3)

**Hypothesis (BTC aggressive trends + prior TP):** half-true. Aggressive
*post-entry* paths pay on **far-TP / low-wr** geometry (~20/2.5, wr≈0.25,
EV≈+2.6, OOS EV≈+3.6) — not on the tight 0.95/0.15, which **loses** on those
paths (EV≈−0.09). Balanced engine bar (w∈[0.40,0.60]) lives elsewhere.

**BTCUSD (n=778):** no pooled balanced cell. **Entry `vol_ratio` low**
(24h rv / 168h rv, tercile) solves **8.07 / 1.77** · wr 0.413 · EV **+1.93**
(h1 +0.64 / h2 +3.23) · e 0.011 · n=186. TP/SL stable vs 14d (8.07/1.63).
Also `disp_atr` mid **10.92/5.66** EV +1.23. Spearman(vol_ratio, post_absmax)
**−0.57** — high entry vol_ratio does *not* predict the big post move.
Production `dynamic_atr_pick` has **no vol gate** yet: BTC cannot ship this
cell live without an entry-time regime filter in `tiered_atr`.

**XAUUSD (n=817):** 14d −EV was **underpowered**. 45d pooled solves
**4.94 / 1.00** · wr 0.405 · EV **+1.23** (h1 +2.12 / h2 +0.34) · n=817.
`vol_ratio` high **8.50 / 0.52** · wr 0.562 · EV **+4.89** (both halves).
`range_atr`/`disp_atr` high gates **fail OOS** (EV ≤ 0) — do not ship those.
Fixed 1.33/0.20 on vol_ratio.high: OOS EV +0.57 wr 0.52 (holds, but dominated).

**XAGUSD (n=804):** pooled 1.88/1.03 EV +0.39; `post_absmax` high
**11.44/2.00** EV +2.98 wr 0.403 — the only symbol where aggressive-path
far-TP clears the balanced band.

**Immediate actions (replay, not calendar):**
1. **XAUUSD** → write `by_symbol.XAUUSD` **4.94 / 1.00** (or gated 8.50/0.52
   after a vol filter exists). Drop 0.91/0.71.
2. **BTCUSD** → implement entry-time `vol_ratio` gate in `tiered_atr` /
   `get_dynamic_atr_config`, then ship **8.07 / 1.77** on low only. Until
   then keep BTC out of live per-symbol (falls to global).
3. Re-run `vol_atr_solver.py` as settles land; 10d production lookback is
   underpowered for these cells (use ≥45d for research).
4. Far-TP “trend runner” (wr≈0.25) is a **different product** — do not fold
   it into the balanced engine bar.


## BTCUSD

paths n=778 · window 2026-08-14 → 2026-09-28

- **pooled**: —


### entry `vol_ratio` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 8.07 | 1.77 | 4.56 | 0.413 | +1.933 | +0.641 | +3.225 | 0.011 | 186 |

| mid | — | | | | | | | | 186 |

| high | 3.82 | 3.79 | 1.01 | 0.560 | +0.470 | +0.828 | +0.116 | 0.189 | 185 |


### entry `range_atr` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 3.96 | 2.35 | 1.69 | 0.452 | +0.387 | +0.368 | +0.406 | 0.088 | 250 |

| mid | 5.95 | 3.08 | 1.93 | 0.404 | +0.501 | +0.710 | +0.294 | 0.145 | 249 |

| high | 7.85 | 4.82 | 1.63 | 0.440 | +0.262 | +0.452 | +0.071 | 0.200 | 250 |


### entry `disp_atr` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 5.44 | 2.72 | 2.00 | 0.408 | +0.603 | +0.603 | +0.603 | 0.108 | 250 |

| mid | 10.92 | 5.66 | 1.93 | 0.418 | +1.230 | +1.779 | +0.685 | 0.193 | 249 |

| high | 2.24 | 0.81 | 2.77 | 0.404 | +0.128 | +0.089 | +0.168 | 0.000 | 250 |


### post `post_absmax` terciles (hypothesis only — not a live gate)

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 0.34 | 0.15 | 2.27 | 0.479 | +0.064 | +0.060 | +0.068 | 0.000 | 259 |

| mid | 10.70 | 7.37 | 1.45 | 0.558 | +2.888 | +2.709 | +3.068 | 0.042 | 260 |

| high | — | | | | | | | | 259 |


entry→post Spearman:

- `vol_ratio` vs `post_absmax`: ρ=-0.567 (n=557)

- `range_atr` vs `post_absmax`: ρ=-0.035 (n=749)

- `disp_atr` vs `post_absmax`: ρ=-0.132 (n=749)


### post `post_range` terciles (hypothesis only — not a live gate)

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 3.29 | 3.00 | 1.10 | 0.514 | +0.239 | +0.400 | +0.079 | 0.181 | 259 |

| mid | 12.88 | 9.22 | 1.40 | 0.521 | +2.594 | +2.226 | +2.962 | 0.181 | 260 |

| high | — | | | | | | | | 259 |


## XAUUSD

paths n=817 · window 2026-08-14 → 2026-09-28

- **pooled**: **4.94** / **1.00** · RR 4.94 · wr 0.405 · EV +1.226 (h1 +2.116 / h2 +0.338) · e 0.000 · n 817


### entry `vol_ratio` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 1.96 | 1.04 | 1.88 | 0.428 | +0.183 | +0.336 | +0.031 | 0.000 | 269 |

| mid | — | | | | | | | | 269 |

| high | 8.50 | 0.52 | 16.35 | 0.562 | +4.889 | +5.430 | +4.351 | 0.186 | 269 |


### entry `range_atr` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | — | | | | | | | | 272 |

| mid | 9.66 | 0.97 | 9.96 | 0.498 | +4.050 | +5.083 | +3.018 | 0.040 | 274 |

| high | 2.79 | 1.02 | 2.74 | 0.410 | +0.202 | +0.120 | +0.284 | 0.000 | 271 |


**gate candidate** `range_atr` high-only 2.79/1.02 — IS EV +0.314 wr 0.455 n=189; OOS EV -0.055 wr 0.305 n=82


### entry `disp_atr` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | — | | | | | | | | 277 |

| mid | 4.05 | 0.73 | 5.55 | 0.429 | +1.267 | +2.217 | +0.317 | 0.000 | 268 |

| high | 2.79 | 0.51 | 5.47 | 0.419 | +0.858 | +1.501 | +0.215 | 0.000 | 272 |


**gate candidate** `disp_atr` high-only 2.79/0.51 — IS EV +1.242 wr 0.479 n=190; OOS EV -0.033 wr 0.280 n=82


### post `post_absmax` terciles (hypothesis only — not a live gate)

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 4.38 | 0.52 | 8.42 | 0.420 | +1.394 | +2.681 | +0.107 | 0.000 | 314 |

| mid | 9.66 | 1.03 | 9.38 | 0.429 | +3.059 | +3.388 | +2.731 | 0.000 | 231 |

| high | — | | | | | | | | 272 |


entry→post Spearman:

- `vol_ratio` vs `post_absmax`: ρ=-0.304 (n=807)

- `range_atr` vs `post_absmax`: ρ=0.049 (n=817)

- `disp_atr` vs `post_absmax`: ρ=-0.074 (n=817)


### post `post_range` terciles (hypothesis only — not a live gate)

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 9.03 | 0.45 | 20.07 | 0.465 | +4.325 | +6.881 | +1.788 | 0.190 | 279 |

| mid | 2.63 | 1.03 | 2.55 | 0.425 | +0.335 | +0.077 | +0.592 | 0.000 | 266 |

| high | — | | | | | | | | 272 |


## XAGUSD

paths n=804 · window 2026-08-14 → 2026-09-28

- **pooled**: **1.88** / **1.03** · RR 1.83 · wr 0.544 · EV +0.390 (h1 +0.495 / h2 +0.284) · e 0.000 · n 804


### entry `vol_ratio` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 1.90 | 0.93 | 2.04 | 0.532 | +0.458 | +0.133 | +0.781 | 0.000 | 265 |

| mid | 4.76 | 3.73 | 1.28 | 0.586 | +1.597 | +0.256 | +2.937 | 0.192 | 266 |

| high | — | | | | | | | | 265 |


### entry `range_atr` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 5.21 | 3.43 | 1.52 | 0.591 | +1.814 | +0.748 | +2.880 | 0.198 | 268 |

| mid | 1.90 | 0.90 | 2.11 | 0.545 | +0.319 | +0.407 | +0.231 | 0.000 | 277 |

| high | 1.07 | 0.23 | 4.65 | 0.409 | +0.189 | +0.138 | +0.240 | 0.000 | 259 |


### entry `disp_atr` terciles

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 7.52 | 4.89 | 1.54 | 0.556 | +2.042 | +1.070 | +3.014 | 0.194 | 268 |

| mid | — | | | | | | | | 268 |

| high | — | | | | | | | | 268 |


### post `post_absmax` terciles (hypothesis only — not a live gate)

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | 1.92 | 0.17 | 11.29 | 0.489 | +0.690 | +0.685 | +0.695 | 0.000 | 268 |

| mid | — | | | | | | | | 268 |

| high | 11.44 | 2.00 | 5.72 | 0.403 | +2.983 | +1.316 | +4.650 | 0.000 | 268 |


entry→post Spearman:

- `vol_ratio` vs `post_absmax`: ρ=-0.189 (n=796)

- `range_atr` vs `post_absmax`: ρ=0.145 (n=804)

- `disp_atr` vs `post_absmax`: ρ=-0.061 (n=804)


### post `post_range` terciles (hypothesis only — not a live gate)

| cell | TP | SL | RR | wr | EV | h1 | h2 | e | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|

| low | — | | | | | | | | 268 |

| mid | 1.88 | 0.26 | 7.23 | 0.571 | +0.738 | +0.333 | +1.142 | 0.000 | 268 |

| high | 7.03 | 2.97 | 2.37 | 0.407 | +0.916 | +0.432 | +1.401 | 0.000 | 268 |
