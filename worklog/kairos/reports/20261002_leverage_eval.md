# Track A — calibration + leverage effect of withdrawing `p_markov_hold_n`

Generated 2026-10-02 00:36 UTC · lookback 45d · join tolerance ±10 min

`docs/scopes/20261001_CALIBRATION_LEVERAGE_NEXT_STEPS.md` §3 Track A. Every decided signal is replayed with the withdrawn source present, via the exact identity `L_off = (L_on − w_m·logit(p_m)) / (1 − w_m)`.

## Verdict — RISK — calibration got WORSE while size rose; revert pending a replacement

| question | answer |
|---|---|
| Did calibration improve? | **NO** (Brier Δ +0.1382; negative = better) |
| Did sizing rise? | **YES** (mean ev_scale Δ +0.2827) |
| Signals going 0 → sized | **23.05%** (7014 of 30433) |

## 1. Headline

n = **30433** decided signals with a reconstructable blend

| metric | with source (actual) | without source (counterfactual) | Δ |
|---|---:|---:|---:|
| **Brier** | 0.2655 | 0.4037 | **+0.1382** |
| mean `p_win` | 0.4424 | 0.6585 | +0.2161 |
| mean `ev_scale` | 0.5576 | 0.8403 | **+0.2827** |

- Population base rate (TP share): **0.2727**
- **Constant-prediction Brier (p = base rate): 0.1983** — the reference line. Actual blend: 0.2655.
- Withdrawn source `p_m`: mean **0.158**, median **0.0026** (vs 0.5 neutral — a source below 0.5 drags the pool down)
- `p_win` shift: median **+0.2861**, max **+0.3808**
- `ev_scale` shift: median **+0.0090**

### Reference line — does the blend beat a constant?

**NO.** The blend's Brier (0.2655) is *worse* than predicting the population base rate (0.2727) for every signal (Brier 0.1983). The blend is adding **negative** information about the outcome. This is a property of the blend itself, independent of which sources are in it — re-weighting cannot fix it.

## 2. Sizing transitions (the risk view)

| transition | n | % of population |
|---|---:|---:|
| unsized → sized (**0 → >0**) | 7014 | **23.05%** |
| partially sized → **full size** | 12383 | 40.69% |
| unsized, before | 8831 | — |
| unsized, after | 3103 | — |

## 3. Reliability (shape, not just level)

| bin | n | mean pred (actual) | realised | mean pred (counterfactual) |
|---|---:|---:|---:|---:|
| 0.0-0.1 | 227 | 0.083 | 0.3877 | 0.0686 |
| 0.1-0.2 | 2986 | 0.164 | 0.285 | 0.1539 |
| 0.2-0.3 | 4594 | 0.248 | 0.2553 | 0.2566 |
| 0.3-0.4 | 5686 | 0.3478 | 0.277 | 0.3529 |
| 0.4-0.5 | 6325 | 0.4518 | 0.2949 | 0.4566 |
| 0.5-0.6 | 4015 | 0.5452 | 0.2339 | 0.548 |
| 0.6-0.7 | 2829 | 0.6493 | 0.2743 | 0.6537 |
| 0.7-0.8 | 2451 | 0.7501 | 0.2864 | 0.7507 |
| 0.8-0.9 | 1179 | 0.8373 | 0.2553 | 0.8499 |
| 0.9-1.0 | 141 | 0.9161 | 0.1986 | 0.9426 |

A pure level shift moves every bin's `mean pred` the same way while `realised` stays put. A slope change is a calibration change. Compare the two pred columns against the realised column.

## 4. Per symbol

| symbol | n | mean p_m | Brier on | Brier off | Δ Brier | mean ev_scale on | off | Δ | 0→size |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| AAPL | 3608 | 0.1053 | 0.2412 | 0.3474 | +0.1063 | 0.4088 | 0.8462 | +0.4374 | 47.56% |
| USDJPY | 2380 | 0.1896 | 0.2694 | 0.3489 | +0.0795 | 0.4121 | 0.8 | +0.3879 | 26.55% |
| USDCHF | 1234 | 0.1239 | 0.2574 | 0.3516 | +0.0942 | 0.4209 | 0.7748 | +0.3539 | 35.49% |
| BTCUSD | 2682 | 0.1277 | 0.2825 | 0.3898 | +0.1073 | 0.5336 | 0.8767 | +0.3431 | 23.01% |
| AUDUSD | 874 | 0.1364 | 0.2796 | 0.3981 | +0.1185 | 0.5019 | 0.8268 | +0.3248 | 23.68% |
| USDCAD | 1291 | 0.1483 | 0.2393 | 0.432 | +0.1927 | 0.5317 | 0.848 | +0.3163 | 27.03% |
| XAUUSD | 2793 | 0.167 | 0.2551 | 0.4211 | +0.1660 | 0.5626 | 0.8516 | +0.2890 | 18.08% |
| XAUAUD | 3206 | 0.1854 | 0.257 | 0.3846 | +0.1276 | 0.5889 | 0.8623 | +0.2734 | 17.69% |
| XAUEUR | 3111 | 0.1559 | 0.2368 | 0.4257 | +0.1889 | 0.5733 | 0.8383 | +0.2650 | 21.79% |
| GBPUSD | 1127 | 0.1682 | 0.318 | 0.4535 | +0.1355 | 0.6097 | 0.8281 | +0.2183 | 16.59% |
| EURUSD | 836 | 0.1306 | 0.2855 | 0.4227 | +0.1373 | 0.6833 | 0.8818 | +0.1985 | 16.27% |
| US30USD | 2162 | 0.1929 | 0.2917 | 0.4398 | +0.1481 | 0.646 | 0.8302 | +0.1842 | 14.8% |
| US500 | 1990 | 0.1549 | 0.2877 | 0.4213 | +0.1336 | 0.6355 | 0.8137 | +0.1782 | 14.82% |
| XAGUSD | 3139 | 0.1921 | 0.2758 | 0.4549 | +0.1791 | 0.7246 | 0.8455 | +0.1210 | 11.69% |

## 5. Caveats (read before acting)

- **`p_m` is a proxy.** The engine consumed `p_markov_hold_n = T^N[current][target]`; what is stored is the single-step `T[current][target]` (`markov_p_up`/`markov_p_dn`). The single-step value is *lower* than any multi-step hold probability, so this **understates** the withdrawal's effect rather than overstating it.
- **`L_on` is the pre-enforcement blend output** (`predicted_p_win_raw`), which is what the engine produced before `apply_calibration_adjustment`. The counterfactual is therefore of the *blend*, not of the final enforced `p_win`.
- **Storage rounding.** Stored values are 4-decimal; the reconstruction inherits that (~1e-3 worst case in logit space). Measured effects are ~1e-1.
- **`ev_scale` is one factor of size.** `effective_kelly = kelly_f × ev_scale`, and `kelly_f` is unchanged by the withdrawal. The Δ here is the *leverage* component only.
- **Join coverage:** 30433 of 41685 sims joined (0 no sweep row, 11252 outside ±10 min, 0 no directional p_m).

## Notes

- Removing a source renormalises the surviving weights by `1/(1−w_m)`, so the withdrawal changes the blend **even where the source said nothing** (p_m = 0.5).
- `p_markov_hold_n_mode: "off"` is live as of 2026-10-01; this measures the change it made.

## 6. Side-by-side — which probability is best calibrated?

Target event: **P(TP before SL within deadline)** (`outcome = tp_hit`). Each candidate is scored on its **own** available rows, against a constant baseline computed on those same rows — the only apples-to-apples read when coverage differs.

`edge = brier_constant − brier`. **Positive = beats saying nothing. Negative = anti-predictive**, and the size is how much worse than a constant.

| candidate | event | n | coverage | Brier | base rate | constant | **edge** |
|---|---|---:|---:|---:|---:|---:|---:|
| `p_win` | P(TP before SL within deadline) | 30433 | 1.000 | 0.2613 | 0.2727 | 0.1983 | **-0.0630** |
| `p_win_raw` | P(TP before SL within deadline) | 30433 | 1.000 | 0.2655 | 0.2727 | 0.1983 | **-0.0672** |
| `p_win_bracket` | P(TP before SL within deadline) | 9276 | 0.305 | 0.2197 | 0.2803 | 0.2017 | **-0.0179** |
| `p_win_dir` | P(TP before SL within deadline) | 9539 | 0.313 | 0.2894 | 0.2825 | 0.2027 | **-0.0867** |
| `p_win_resolve` ⚠ | P(TP | resolved) | 9276 | 0.305 | 0.2172 | 0.2803 | 0.2017 | **-0.0155** |
| `p_resolve` ⚠ | P(resolved before deadline) | 9276 | 0.305 | 0.5021 | 0.2803 | 0.2017 | **-0.3004** |

⚠ = estimates a **different event** than the target. Shown for reference; it cannot be ranked against the others on this target, and treating it as if it could is the category error `p_win_bracket` was built to close.

### Common subset (n = 9276) — same rows for every candidate

Removes the coverage confound: a ranking here is a ranking of the **predictions**, not of the rows each happened to cover.

| candidate | Brier | base rate | constant | **edge** | rank |
|---|---:|---:|---:|---:|---:|
| `p_win_bracket` | 0.2197 | 0.2803 | 0.2017 | **-0.0179** | 1 |
| `p_win` | 0.2816 | 0.2803 | 0.2017 | **-0.0798** | 2 |
| `p_win_raw` | 0.2890 | 0.2803 | 0.2017 | **-0.0873** | 3 |
| `p_win_dir` | 0.2890 | 0.2803 | 0.2017 | **-0.0873** | 4 |

**Best on the common subset: `p_win_bracket`** (edge -0.0179). Worst: `p_win_dir` (edge -0.0873).

> **No candidate beats a constant on the common subset.** Every probability in the table is anti-predictive for this event — which is a statement about the *event definition or the outcome data*, not about any one model. Investigate before choosing between them.

### Reliability (target-event candidates)

**`p_win`** (n=30433, Brier 0.2613)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 2540 | 0.1596 | 0.2988 | +0.1392 |
| 0.2-0.4 | 9751 | 0.3063 | 0.2618 | -0.0445 |
| 0.4-0.6 | 12650 | 0.4906 | 0.2853 | -0.2053 |
| 0.6-0.8 | 4487 | 0.6923 | 0.2518 | -0.4405 |
| 0.8-1.0 | 1005 | 0.8443 | 0.2458 | -0.5985 |

**`p_win_raw`** (n=30433, Brier 0.2655)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 3213 | 0.1583 | 0.2923 | +0.1340 |
| 0.2-0.4 | 10280 | 0.3032 | 0.2673 | -0.0359 |
| 0.4-0.6 | 10340 | 0.4881 | 0.2712 | -0.2169 |
| 0.6-0.8 | 5280 | 0.6961 | 0.2799 | -0.4162 |
| 0.8-1.0 | 1320 | 0.8457 | 0.2492 | -0.5965 |

**`p_win_bracket`** (n=9276, Brier 0.2197)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 4596 | 0.103 | 0.2487 | +0.1457 |
| 0.2-0.4 | 2533 | 0.2938 | 0.261 | -0.0328 |
| 0.4-0.6 | 1585 | 0.4828 | 0.3596 | -0.1232 |
| 0.6-0.8 | 425 | 0.681 | 0.3388 | -0.3422 |
| 0.8-1.0 | 137 | 0.8892 | 0.5985 | -0.2907 |

**`p_win_dir`** (n=9539, Brier 0.2894)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 1201 | 0.151 | 0.3356 | +0.1846 |
| 0.2-0.4 | 2933 | 0.3006 | 0.3041 | +0.0035 |
| 0.4-0.6 | 2803 | 0.4917 | 0.274 | -0.2177 |
| 0.6-0.8 | 2078 | 0.7011 | 0.2632 | -0.4379 |
| 0.8-1.0 | 524 | 0.8376 | 0.1622 | -0.6754 |

