# Is `p_win` calibrated for the event it forecasts?

Generated 2026-10-02 00:47 UTC · lookback 45d · light sweep, buy/sell

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
| `DIRECTIONAL` | (drift_1h > 0) | 79682 | 0.4805 | 0.2778 | 0.0289 | 0.0011 | 0.2496 | **-0.1130** | 0.5157 |
| `DIRECTIONAL` | (drift_4h > 0) | 79038 | 0.4874 | 0.2799 | 0.0309 | 0.0011 | 0.2498 | **-0.1204** | 0.5106 |
| `DIRECTIONAL` | (drift_24h > 0) | 73305 | 0.5244 | 0.2874 | 0.0376 | 0.0002 | 0.2494 | **-0.1524** | 0.4994 |
| `BRACKET` | (tp_hit before sl_hit) | 41685 | 0.2725 | 0.2597 | 0.0617 | 0.0008 | 0.1982 | **-0.3100** | 0.4860 |

`BSS = 1 − Brier/UNC` — **positive = beats saying the base rate**. `REL` lower is better, `RES` higher is better. `AUC` 0.5 = no ordering.

### The read

- Against **directional** (drift_1h > 0): BSS -0.1130, AUC 0.5157, REL 0.0289 vs RES 0.0011.
- Against **bracket** (tp_hit): BSS -0.3100, AUC 0.4860, REL 0.0617 vs RES 0.0008.

## 2. Side-by-side on the directional label (drift_1h > 0)

Each candidate scored on its own available rows.

| candidate | n | coverage | base rate | Brier | REL | RES | BSS | AUC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `p_win` | 79682 | 0.952 | 0.4805 | 0.2778 | 0.0289 | 0.0011 | **-0.1130** | 0.5157 |
| `predicted_p_win_raw` | 79682 | 0.952 | 0.4805 | 0.2825 | 0.0332 | 0.0011 | **-0.1317** | 0.5190 |
| `p_win_bracket` | 17559 | 0.210 | 0.4721 | 0.3824 | 0.1331 | 0.0014 | **-0.5343** | 0.4646 |
| `p_win_dir` | 17863 | 0.213 | 0.4704 | 0.2984 | 0.0493 | 0.0006 | **-0.1980** | 0.4838 |

`p_win_dir` is stamped as `_p_win_dir = p_win_raw` in `orchestrator.py:4816` — it is the *same variable*, so its numbers should be identical to `predicted_p_win_raw`'s. If they differ, the capture is inconsistent.

## 3. Side-by-side on the bracket label (tp_hit)

| candidate | n | base rate | Brier | REL | RES | BSS | AUC |
|---|---:|---:|---:|---:|---:|---:|---:|
| `p_win` | 41685 | 0.2725 | 0.2597 | 0.0617 | 0.0008 | **-0.3100** | 0.4860 |
| `predicted_p_win_raw` | 41685 | 0.2725 | 0.2628 | 0.0644 | 0.0008 | **-0.3257** | 0.4889 |
| `p_win_bracket` | 9283 | 0.2802 | 0.2196 | 0.0214 | 0.0041 | **-0.0888** | 0.5658 |
| `p_win_dir` | 9546 | 0.2824 | 0.2894 | 0.0875 | 0.002 | **-0.4280** | 0.4542 |

## 4. Per symbol — does `p_win` predict this symbol's direction?

The operational question. A symbol whose AUC sits at 0.5 has a directional score that carries no ordering information for that symbol, regardless of how the system-wide number looks.

| symbol | n | dir base rate | dir AUC | dir BSS | dir REL | dir RES | bracket AUC | bracket BSS |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EURUSD | 5724 | 0.5611 | 0.5909 | -0.0342 | 0.0152 | 0.0072 | 0.5399 | -0.2787 |
| USDCHF | 5725 | 0.5679 | 0.5736 | -0.1376 | 0.0424 | 0.0084 | 0.5356 | -0.3541 |
| GBPUSD | 5726 | 0.518 | 0.5273 | -0.1306 | 0.0342 | 0.0026 | 0.4233 | -0.6285 |
| AUDUSD | 5722 | 0.5585 | 0.5177 | -0.1609 | 0.0431 | 0.0028 | 0.4696 | -0.2525 |
| XAUEUR | 5715 | 0.3939 | 0.5124 | -0.0753 | 0.0186 | 0.0021 | 0.4358 | -0.4952 |
| XAUUSD | 5707 | 0.417 | 0.5046 | -0.1019 | 0.0247 | 0.0011 | 0.4453 | -0.4645 |
| USDJPY | 5727 | 0.4323 | 0.5043 | -0.1528 | 0.0393 | 0.0017 | 0.5614 | -0.2167 |
| AAPL | 5600 | 0.2838 | 0.5040 | -0.2166 | 0.057 | 0.012 | 0.4523 | -0.5189 |
| XAGUSD | 5635 | 0.5056 | 0.4977 | -0.1176 | 0.0345 | 0.0043 | 0.4914 | -0.1857 |
| XAUAUD | 5618 | 0.4737 | 0.4840 | -0.0951 | 0.0268 | 0.0039 | 0.4214 | -0.3577 |
| US30USD | 5711 | 0.5332 | 0.4836 | -0.1594 | 0.0417 | 0.0015 | 0.4399 | -0.3030 |
| BTCUSD | 5629 | 0.4916 | 0.4710 | -0.1202 | 0.0315 | 0.0024 | 0.5088 | -0.1130 |
| USDCAD | 5726 | 0.5272 | 0.4634 | -0.2097 | 0.0527 | 0.0018 | 0.5895 | -0.5420 |
| US500 | 5717 | 0.4588 | 0.4283 | -0.2451 | 0.0686 | 0.0095 | 0.5279 | -0.3682 |

**8 of 14 symbols** have directional AUC > 0.5 (mean 0.5045, min 0.4283, max 0.5909).

On the **bracket** label: 6 of 14 symbols have AUC > 0.5 (mean 0.4887).

## 5. Reliability — `p_win` vs both labels

**DIRECTIONAL (drift_1h > 0)** (n=79682)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 6633 | 0.1532 | 0.4696 | +0.3164 |
| 0.2-0.4 | 23108 | 0.3077 | 0.4777 | +0.1700 |
| 0.4-0.6 | 32380 | 0.491 | 0.4622 | -0.0288 |
| 0.6-0.8 | 13986 | 0.7003 | 0.5102 | -0.1901 |
| 0.8-1.0 | 3575 | 0.8418 | 0.5673 | -0.2745 |

**BRACKET (tp_hit)** (n=41685)

| bin | n | mean pred | realised | gap |
|---|---:|---:|---:|---:|
| 0.0-0.2 | 3188 | 0.1584 | 0.3033 | +0.1450 |
| 0.2-0.4 | 13166 | 0.3077 | 0.2658 | -0.0420 |
| 0.4-0.6 | 18483 | 0.492 | 0.2799 | -0.2121 |
| 0.6-0.8 | 5712 | 0.6865 | 0.2554 | -0.4311 |
| 0.8-1.0 | 1136 | 0.8459 | 0.228 | -0.6179 |

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

