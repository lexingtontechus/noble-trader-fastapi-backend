# Kairos V3 step 1: calibration-error persistence into the next qualified signal

Generated 2026-09-25 08:07 UTC by `scripts/kairos_calibration_persistence.py` (scope §14). Data: `nt_signal_sim` sweep-light buy/sell with an outcome, 2026-08-23 → 2026-09-24.

err = p_win − 1{TP first} (+ = overconfident) · acc = 1{TP first} − SL/(TP+SL) (vs no-skill) · evidence = same symbol, any tier, outcome known before the target fired · expired = non-result.

## 0. Population

| | signals | TP | SL | expired | expired share | mean p_win (resolved) | TP rate (resolved) | mean no-skill | mean err | mean acc |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 73,595 | 9,476 | 26,466 | 37,653 | 0.51 | 0.446 | 0.264 | 0.327 | +0.182 | -0.063 |
| qualified | 979 | 116 | 330 | 533 | 0.54 | 0.599 | 0.260 | 0.332 | +0.339 | -0.072 |
| not qualified | 72,616 | 9,360 | 26,136 | 37,120 | 0.51 | 0.444 | 0.264 | 0.327 | +0.180 | -0.063 |

## 1. Persistence of calibration error (targets = qualified resolved, n = 446)

Static part (per-symbol mean of the target — a constant bias, not carryover): AAPL +0.55 · AUDUSD +0.46 · BTCUSD +0.13 · EURUSD +0.13 · GBPUSD +0.65 · US30USD +0.25 · US500 +0.40 · USDCAD +0.24 · USDCHF +0.58 · USDJPY +0.27 · XAGUSD +0.10 · XAUAUD +0.40 · XAUEUR +0.49 · XAUUSD +0.44

| evidence age (h since resolved) | targets with ≥ 5 evidence | β carryover [95% CI] | r | null β median [2.5, 97.5] | p (β ≥ obs) |
|---|---|---|---|---|---|
| 0–6 | 334 | +0.409 [+0.137, +0.635] | +0.289 | +0.113 [-0.052, +0.294] | 0.004 |
| 6–24 | 376 | +0.238 [-0.029, +0.501] | +0.149 | +0.139 [-0.044, +0.356] | 0.192 |
| 24–72 | 361 | +0.032 [-0.250, +0.431] | +0.016 | -0.001 [-0.266, +0.303] | 0.410 |
| 72–168 | 301 | -0.254 [-0.617, +0.176] | -0.107 | -0.162 [-0.528, +0.235] | 0.682 |
| 168–336 | 164 | -0.139 [-0.510, +0.244] | -0.055 | +0.446 [-0.124, +0.957] | 0.976 |

**Decay (exponential half-life h over all prior evidence).** raw = static + dynamic; dyn = within-symbol demeaned (carryover beyond the symbol's constant bias), with its null.

| half-life h | n | β raw | r raw | β dyn | r dyn | null β dyn median | p (dyn) |
|---|---|---|---|---|---|---|---|
| 3h | 419 | +0.354 | +0.224 | +0.188 | +0.112 | -0.025 | 0.060 |
| 6h | 419 | +0.340 | +0.196 | +0.139 | +0.075 | -0.020 | 0.125 |
| 12h | 419 | +0.349 | +0.174 | +0.125 | +0.059 | -0.018 | 0.170 |
| 24h | 419 | +0.320 | +0.132 | +0.098 | +0.039 | -0.029 | 0.250 |
| 48h | 419 | +0.271 | +0.093 | +0.050 | +0.016 | -0.049 | 0.370 |
| 96h | 419 | +0.273 | +0.083 | -0.021 | -0.006 | -0.065 | 0.440 |
| 168h | 419 | +0.273 | +0.080 | -0.103 | -0.026 | -0.063 | 0.560 |
| 336h | 419 | +0.266 | +0.076 | -0.187 | -0.044 | -0.043 | 0.645 |

**Out-of-sample (walk-forward) correction of S2's p_win:** p′ = p_win − β̂·x, β̂ from earlier targets only (clipped to [0, 1]). Negative Δ = better than uncorrected.

| evidence rule | targets scored | Brier uncorrected | Brier corrected | Δ Brier ± SE | Δ log-loss |
|---|---|---|---|---|---|
| flat 7d, ≥ 20 (current eod_correction) | 387 | 0.3184 | 0.2997 | -0.0187 ± 0.0025 | -0.0410 |
| decay h = 3h | 389 | 0.3182 | 0.2652 | -0.0530 ± 0.0056 | -0.1118 |
| decay h = 6h | 389 | 0.3182 | 0.2726 | -0.0456 ± 0.0049 | -0.0971 |
| decay h = 12h | 389 | 0.3182 | 0.2751 | -0.0431 ± 0.0046 | -0.0911 |
| decay h = 24h | 389 | 0.3182 | 0.2799 | -0.0382 ± 0.0042 | -0.0811 |
| decay h = 48h | 389 | 0.3182 | 0.2847 | -0.0335 ± 0.0036 | -0.0729 |
| decay h = 96h | 389 | 0.3182 | 0.2861 | -0.0320 ± 0.0032 | -0.0713 |
| decay h = 168h | 389 | 0.3182 | 0.2870 | -0.0312 ± 0.0030 | -0.0700 |
| decay h = 336h | 389 | 0.3182 | 0.2879 | -0.0303 ± 0.0030 | -0.0682 |

## 2. Persistence of accuracy vs no-skill (targets = qualified resolved, n = 446)

Static part (per-symbol mean of the target — a constant bias, not carryover): AAPL -0.20 · AUDUSD -0.13 · BTCUSD +0.09 · EURUSD +0.15 · GBPUSD -0.35 · US30USD +0.01 · US500 -0.11 · USDCAD +0.04 · USDCHF -0.18 · USDJPY +0.04 · XAGUSD +0.16 · XAUAUD -0.17 · XAUEUR -0.20 · XAUUSD -0.18

| evidence age (h since resolved) | targets with ≥ 5 evidence | β carryover [95% CI] | r | null β median [2.5, 97.5] | p (β ≥ obs) |
|---|---|---|---|---|---|
| 0–6 | 334 | +0.446 [+0.135, +0.660] | +0.286 | +0.142 [-0.044, +0.343] | 0.000 |
| 6–24 | 376 | +0.219 [-0.046, +0.505] | +0.128 | +0.162 [-0.016, +0.381] | 0.272 |
| 24–72 | 361 | +0.082 [-0.172, +0.443] | +0.039 | +0.030 [-0.232, +0.343] | 0.370 |
| 72–168 | 301 | -0.260 [-0.672, +0.185] | -0.092 | -0.048 [-0.488, +0.392] | 0.800 |
| 168–336 | 164 | +0.031 [-0.340, +0.363] | +0.012 | +0.411 [-0.095, +0.908] | 0.912 |

**Decay (exponential half-life h over all prior evidence).** raw = static + dynamic; dyn = within-symbol demeaned (carryover beyond the symbol's constant bias), with its null.

| half-life h | n | β raw | r raw | β dyn | r dyn | null β dyn median | p (dyn) |
|---|---|---|---|---|---|---|---|
| 3h | 419 | +0.331 | +0.195 | +0.150 | +0.083 | +0.016 | 0.120 |
| 6h | 419 | +0.315 | +0.168 | +0.096 | +0.048 | +0.006 | 0.295 |
| 12h | 419 | +0.331 | +0.152 | +0.086 | +0.037 | +0.004 | 0.330 |
| 24h | 419 | +0.325 | +0.123 | +0.063 | +0.023 | +0.018 | 0.420 |
| 48h | 419 | +0.310 | +0.096 | -0.013 | -0.004 | +0.044 | 0.575 |
| 96h | 419 | +0.300 | +0.080 | -0.149 | -0.036 | +0.065 | 0.675 |
| 168h | 419 | +0.274 | +0.070 | -0.275 | -0.061 | +0.073 | 0.730 |
| 336h | 419 | +0.245 | +0.061 | -0.388 | -0.082 | +0.064 | 0.790 |

_Runtime 379s · 500 null draws (decay null: 200) · 1000 cluster-bootstrap draws (symbol-day)._
