# Is the reachability cell cross-sectional or market-wide?

Generated 2026-10-02 03:00 UTC · lookback 60d · 3d windows · min_n 20 · 4 folds

`reachability_decay.py` found the cell rate drifts hard between eras (mean |Δ| 0.20) yet **no trailing window recovers transfer**. The explanation that fits both is a **shared market-wide factor**: if all cells load on one driver, they move together (high drift), the ordering between them is stable-but-uninformative, and refitting just re-estimates the same factor. This decomposes the cell-rate matrix to test it.

## 1. Variance decomposition

```
R[cell, window] = μ + a[cell] + b[window] + e[cell, window]
                     └ cell ┘  └  time  ┘  └  residual  ┘
```

**MIXED — cell 43%, time 10%, residual 47%. No single component dominates.**

| component | variance | share | what it means |
|---|---:|---:|---|
| **cell** (cross-sectional) | 0.02861 | **43.0%** | does this cell win more than others? |
| **time** (common factor) | 0.00683 | **10.3%** | does this *period* win more? |
| **residual** | 0.03105 | **46.7%** | cell×time interaction + noise |

Observations: **425** (cell, window) pairs over 162 cells × 12 windows · grand mean 0.3036

Unweighted means (each observed pair counts once), so a few very large cells cannot define the "market" factor.

## 2. Does removing the factor reveal an edge?

**NO CROSS-SECTIONAL EDGE — raw 0.5486, factor-removed 0.5100; neither clears 0.55. Ranking cells adds nothing once the period is accounted for, so any edge here is time-series, not cross-sectional**

| ranking | out-of-fold AUC | n |
|---|---:|---:|
| raw cell rate (what the model uses) | 0.5486 | 9517 |
| cell rate **with the period factor removed** | 0.5100 | 9517 |

Both ranked on the same rows with the same fold structure, so a difference is attributable to the factor removal alone.

## 3. Cell effects (a[cell]) — the cross-sectional ranking

Each cell's average deviation from the grand mean, across all windows. This is the ordering a cross-sectional model would exploit.

| cell | a[cell] |
|---|---:|
| AAPL · med_low_vol · 0.5-1 | +0.6607 |
| US500 · med_high_vol · 2-4 | +0.5964 |
| XAUUSD · low_vol · 2-4 | +0.5714 |
| AUDUSD · high_vol · 1-2 | +0.5660 |
| XAGUSD · med_low_vol · 2-4 | +0.4548 |
| AAPL · low_vol · 0.5-1 | +0.4464 |
| XAGUSD · high_vol · 1-2 | +0.4405 |
| USDJPY · low_vol · 0.5-1 | +0.3708 |
| USDCAD · low_vol · 0.5-1 | +0.3686 |
| USDCHF · med_high_vol · 0.5-1 | +0.3653 |
| USDCAD · med_high_vol · 2-4 | +0.3486 |
| US30USD · high_vol · 0.5-1 | +0.3476 |
| AAPL · med_high_vol · 4+ | +0.3468 |
| XAUUSD · high_vol · 0.5-1 | +0.3011 |
| USDJPY · med_high_vol · <0.5 | +0.2887 |
| XAUEUR · med_high_vol · 1-2 | +0.2881 |
| AUDUSD · med_low_vol · 1-2 | +0.2818 |
| XAUEUR · low_vol · 2-4 | +0.2758 |
| US500 · med_high_vol · 0.5-1 | +0.2631 |
| XAUUSD · med_high_vol · 0.5-1 | +0.2616 |

Spread of cell effects: **0.9643** across 162 cells.

## 4. Window effects (b[window]) — the common factor

| window | b[window] |
|---|---:|
| w0 | +0.0159 |
| w1 | -0.0408 |
| w2 | -0.1095 |
| w3 | -0.1752 |
| w4 | +0.0868 |
| w5 | +0.0586 |
| w6 | +0.0170 |
| w7 | -0.0503 |
| w8 | -0.1126 |
| w9 | -0.0733 |
| w10 | +0.0900 |
| w11 | +0.0673 |

Range of the common factor: **-0.1752 to +0.0900** (spread 0.2652).

## 5. Coverage

- fetched: **41824** resolved light signals
- usable: **38177** (91.3%)
- dropped: 3647 no ATR/timestamp, 0 unbucketable
- span: **36 days** · 12 windows of 3d
- ⚠ available history is **36d**, not the 60d requested — `nt_signal_sim` starts 2026-08-23. Only ~12 windows exist, which is the binding limit on this decomposition's power.

## 6. Caveats

- **Resolved rows only** — expired excluded (no TP/SL verdict). A selected sub-population; audit C2/R6.
- **40 days of history.** The whole system started 2026-08-23, so the window count is small and the decomposition's power is bounded. Treat the shares as directional, not precise.
- **Unweighted two-way decomposition** (no interaction term estimated separately); `residual` therefore absorbs both the true cell×time interaction and sampling noise. That is the conservative reading — it cannot manufacture a cell effect.
- **`a[cell]` is an average over the observed windows**, so a cell present in few windows has a noisier effect. `min_n` bounds within-window noise, not across-window coverage.

## Notes

- If **time dominates**, the reachability table is a market-state read, not a cell-quality read — the model should condition on a *market* variable, not enumerate cells.
- If **cell dominates** but the residual test still fails, the cell ordering exists but is not stable enough to trade.
- If **residual dominates**, the cell rate is mostly noise and neither a cell table nor a factor adjustment will help.

