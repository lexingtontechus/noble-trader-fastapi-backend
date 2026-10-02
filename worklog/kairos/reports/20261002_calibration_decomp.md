# Is `p_win` calibrated for the event it forecasts?

Generated 2026-10-02 03:38 UTC · lookback 45d · light sweep, buy/sell

The repo's quant audit calls this the core **category error (E1)**: `p_win` is a *directional* confidence (its sources are `p_regime` = P(bull|bear) and `p_timesfm` = P(next bar up)), while the settlement label is a *bracket* event (does price reach +TP before −SL within 48h). This scores `p_win` against **both**, with the Murphy decomposition so calibration and discrimination are not conflated.

## Why the previous 'anti-predictive' verdict was a reference-line artifact

A constant forecast `p ≡ ō` has `REL = 0` and `RES = 0` by construction, so:

```
Brier(constant) = UNC = ō(1 − ō)      # a property of the EVENT, not the model
Brier = REL − RES + UNC                # Murphy (1973)
```

Losing to that constant therefore means `RES ≤ REL` — it does **not** distinguish *'no resolution for this event'* from *'calibrated for a different event'*. The decomposition below separates them.

## 1. `p_win` against both labels

| label | event | n | base rate | Brier | REL | RES | UNC | **BSS** | AUC |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `DIRECTIONAL` | (drift_1h > 0) | 79842 | 0.4802 | 0.2778 | 0.0289 | 0.0011 | 0.2496 | **-0.1129** | 0.5159 |
| `DIRECTIONAL` | (drift_4h > 0) | 79244 | 0.4875 | 0.2797 | 0.0308 | 0.0011 | 0.2498 | **-0.1196** | 0.5113 |
| `DIRECTIONAL` | (drift_24h > 0) | 73461 | 0.5243 | 0.2874 | 0.0375 | 0.0002 | 0.2494 | **-0.1524** | 0.4994 |
| `BRACKET` | (tp_hit before sl_hit) | 41900 | 0.2723 | 0.2596 | 0.0617 | 0.0008 | 0.1982 | **-0.3102** | 0.4863 |
| `SURVIVABLE` | (tp_hit AND mae_24h < sl) | 6279 | 0.1186 | 0.249 | 0.1443 | 0.0005 | 0.1046 | **-1.3812** | 0.4746 |

`BSS = 1 − Brier/UNC` — **positive = beats saying the base rate**. `REL` lower is better, `RES` higher is better. `AUC` 0.5 = no ordering.

### The read

- Against **directional** (drift_1h > 0): BSS -0.1129, AUC 0.5159, REL 0.0289 vs RES 0.0011.
- Against **bracket** (tp_hit): BSS -0.3102, AUC 0.4863, REL 0.0617 vs RES 0.0008.

## 1b. Ghost edge — bracket vs survivable, on the SAME rows

The `SURVIVABLE` label above is computed only on rows where `mae_24h_pct` is populated, so its n is much smaller than `BRACKET`'s — comparing them directly would confound the label with the population. Below, **both** are restricted to the rows that have the MAE column.

Common population: **11841** rows with `mae_24h_pct`.

| label | n | base rate | Brier | REL | RES | AUC |
|---|---:|---:|---:|---:|---:|---:|
| BRACKET (tp_hit) | 6279 | 0.2805 | 0.2698 | 0.0692 | 0.0016 | **0.4705** |
| SURVIVABLE (tp_hit AND mae_24h < sl) | 6279 | 0.1186 | 0.249 | 0.1443 | 0.0005 | **0.4746** |

**If the survivable AUC is lower than the bracket AUC on identical rows, part of the apparent bracket skill was ghost edge** — wins that a real stop would have taken out before TP was reached.

### The level finding — how many 'wins' a stop would have taken out

- bracket wins on this population: **1761** of 6279 (base rate 0.2805)
- of those, **745** survived (never breached the stop distance) — **1016 did not (57.7%)**
- survivable base rate: **0.1186**

**This is the ghost edge, quantified.** The settlement label comes from a close-walk; the MAE column comes from **intrabar highs/lows** (`migration 170`). So the two are not the same measurement, and the 58% figure is an **upper bound** — some of it is the close-vs-intrabar difference rather than a genuine stop-out. The true figure lies between 0 and that number; pinning it needs an intrabar-consistent resolver.

**Note where the effect lands.** AUC barely moves (bracket 0.4705 → survivable 0.4746), but `REL` more than doubles (0.0692 → 0.1443). The ghost edge is a **level** distortion, not a ranking one — which is exactly what the `REL ≫ RES` finding already said. It does not rescue the model's discrimination, and it makes its overconfidence worse.

## 2. Side-by-side on the directional label (drift_1h > 0)

Each candidate scored on its own available rows.

| candidate | n | coverage | base rate | Brier | REL | RES | BSS | AUC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `p_win` | 79842 | 0.953 | 0.4802 | 0.2778 | 0.0289 | 0.0011 | **-0.1129** | 0.5159 |
| `predicted_p_win_raw` | 79842 | 0.953 | 0.4802 | 0.2825 | 0.0331 | 0.001 | **-0.1316** | 0.5191 |
| `p_win_bracket` | 17719 | 0.212 | 0.4712 | 0.3816 | 0.1325 | 0.0015 | **-0.5314** | 0.4637 |
| `p_win_dir` | 18023 | 0.215 | 0.4695 | 0.2982 | 0.0491 | 0.0005 | **-0.1971** | 0.4845 |

`p_win_dir` is stamped as `_p_win_dir = p_win_raw` in `orchestrator.py:4816` — it is the *same variable*, so its numbers should be identical to `predicted_p_win_raw`'s. If they differ, the capture is inconsistent.

## 3. Side-by-side on the bracket label (tp_hit)

| candidate | n | base rate | Brier | REL | RES | BSS | AUC |
|---|---:|---:|---:|---:|---:|---:|---:|
| `p_win` | 41900 | 0.2723 | 0.2596 | 0.0617 | 0.0008 | **-0.3102** | 0.4863 |
| `predicted_p_win_raw` | 41900 | 0.2723 | 0.2627 | 0.0644 | 0.0008 | **-0.3258** | 0.4892 |
| `p_win_bracket` | 9498 | 0.2793 | 0.2195 | 0.0216 | 0.0039 | **-0.0903** | 0.5655 |
| `p_win_dir` | 9761 | 0.2815 | 0.2885 | 0.0869 | 0.002 | **-0.4263** | 0.4558 |

## 4. Per symbol — does `p_win` predict this symbol's direction?

The operational question. A symbol whose AUC sits at 0.5 has a directional score that carries no ordering information for that symbol, regardless of how the system-wide number looks.

| symbol | n | dir base rate | dir AUC | dir BSS | dir REL | dir RES | bracket AUC | bracket BSS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EURUSD | 5734 | 0.5605 | 0.5915 | -0.0333 | 0.0151 | 0.0073 | 0.5435 | -0.2782 |
| USDCHF | 5734 | 0.5673 | 0.5738 | -0.1367 | 0.0421 | 0.0084 | 0.5326 | -0.3675 |
| GBPUSD | 5737 | 0.518 | 0.5279 | -0.1296 | 0.034 | 0.0026 | 0.4274 | -0.6254 |
| AUDUSD | 5734 | 0.5586 | 0.5180 | -0.1605 | 0.0429 | 0.0027 | 0.4731 | -0.2517 |
| XAUEUR | 5724 | 0.3936 | 0.5124 | -0.0754 | 0.0186 | 0.0021 | 0.4361 | -0.4944 |
| XAUUSD | 5720 | 0.4173 | 0.5056 | -0.1010 | 0.0246 | 0.0012 | 0.4479 | -0.4632 |
| USDJPY | 5736 | 0.4325 | 0.5043 | -0.1525 | 0.0392 | 0.0017 | 0.5607 | -0.2160 |
| AAPL | 5624 | 0.2825 | 0.5025 | -0.2248 | 0.0584 | 0.0119 | 0.4516 | -0.5288 |
| XAGUSD | 5650 | 0.5055 | 0.4987 | -0.1167 | 0.0342 | 0.0043 | 0.4939 | -0.1825 |
| XAUAUD | 5627 | 0.4734 | 0.4844 | -0.0949 | 0.0267 | 0.0039 | 0.4221 | -0.3572 |
| US30USD | 5720 | 0.533 | 0.4836 | -0.1592 | 0.0417 | 0.0015 | 0.4399 | -0.3030 |
| BTCUSD | 5638 | 0.4913 | 0.4705 | -0.1205 | 0.0316 | 0.0024 | 0.5047 | -0.1154 |
| USDCAD | 5735 | 0.5271 | 0.4637 | -0.2092 | 0.0526 | 0.0018 | 0.5918 | -0.5385 |
| US500 | 5729 | 0.4591 | 0.4286 | -0.2446 | 0.0684 | 0.0095 | 0.5290 | -0.3670 |

**8 of 14 symbols** have directional AUC > 0.5 (mean 0.5047, min 0.4286, max 0.5915).

On the **bracket** label: 6 of 14 symbols have AUC > 0.5 (mean 0.4896).

## 5. Reliability — `p_win` vs both labels

**DIRECTIONAL (drift_1h > 0)** (n=79842)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 6649 | 0.1532 | 0.4692 | +0.3161 |
| 0.2-0.4 | 23173 | 0.3077 | 0.477 | +0.1693 |
| 0.4-0.6 | 32428 | 0.491 | 0.4624 | -0.0286 |
| 0.6-0.8 | 14004 | 0.7003 | 0.5102 | -0.1901 |
| 0.8-1.0 | 3588 | 0.8418 | 0.5655 | -0.2763 |

**BRACKET (tp_hit)** (n=41900)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 3222 | 0.1583 | 0.3032 | +0.1449 |
| 0.2-0.4 | 13258 | 0.3078 | 0.265 | -0.0428 |
| 0.4-0.6 | 18534 | 0.492 | 0.2802 | -0.2118 |
| 0.6-0.8 | 5735 | 0.6865 | 0.2558 | -0.4307 |
| 0.8-1.0 | 1151 | 0.8458 | 0.2259 | -0.6199 |

`gap = realised − mean_pred`; **negative = overconfident**. If the directional gaps are small and the bracket gaps are large, the miscalibration is *entirely* the label mismatch — not a defect in the model.

## 6. Caveats

- **`drift_*_pct` is a proxy for the directional event.** It is direction-relative post-entry drift as % of entry price (migration 121), computed regardless of outcome. The model's `p_timesfm` is specifically P(**next bar** up), so `drift_1h_pct` is the closest available label; 4h/24h are shown because the signal's own horizon is not 1 bar. `p_regime` is a slower regime posterior and may align better with 24h.
- **Close-only resolution.** Drift and outcomes come from hourly closes; the bracket resolver checks TP before SL on the same bar (optimistic) and does not model gaps. Bracket AUC inherits that.
- **Expired excluded from the bracket label** (neither win nor loss, per the platform's own convention). That is ~49% of light signals, so the bracket label describes the *resolving* sub-population, which is selected — a further reason the bracket AUC is not a clean read on the model.
- **`p_win_bracket` is bucket-derived**, so its n is limited by reachability-table coverage and its values are coarse (piecewise constant across a bucket). Its AUC is correspondingly attenuated.
- **Binning residual.** `Brier = REL − RES + UNC` holds exactly only when stratifying on distinct forecast values; with 10 bins there is a residual, reported in §1's source data.

## Notes

- This module (`calibration_decomp.py`) is the corrected instrument: Murphy decomposition + AUC, against a named label. The earlier `leverage_eval.py` constant-baseline pass remains valid as a *bracket* measurement; it was never a directional one.
- `p_win_dir == p_win_raw` is an open question — if they are the same variable, the capture stamps two column names for one quantity and `p_win_dir` should be dropped or redefined.

