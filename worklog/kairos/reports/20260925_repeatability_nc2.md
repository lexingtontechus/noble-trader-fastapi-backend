# Kairos repeatability: is the edge/decay map stable out of sample? (n_confirm 2)

Generated 2026-09-25 06:12 UTC by `scripts/kairos_repeatability.py` · 47,304 signals · 5 symbols · 100 circular-shift null draws · runtime 363s.

e_h = label-following move in ATR **minus the symbol's trailing 30-day drift** (no look-ahead). m_h = raw label-following move (the P1 measure). ρ = Spearman rank correlation of the map between blocks; p = share of null draws (labels circularly shifted vs prices) at or above the observed value.

## 1. Does the map repeat?

| map | measure | keys | ρ H1→H2 | null ρ | p | ρ consecutive quarters | null ρ | p | |t|≥2 keys in H1 | same sign in H2 | null | p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cell×age | e4 | 31 | +0.09 | -0.01 | 0.310 | -0.05 | +0.00 | 0.770 | 1 | 0.00 | 0.50 | 1.000 |
| cell×age | e24 | 31 | -0.08 | -0.01 | 0.590 | -0.01 | -0.01 | 0.490 | 1 | 1.00 | 0.50 | 0.276 |
| symbol×cell | e4 | 32 | -0.09 | -0.00 | 0.690 | -0.04 | -0.02 | 0.610 | 4 | 0.75 | 0.50 | 0.338 |
| symbol×cell | e24 | 32 | +0.17 | +0.01 | 0.240 | -0.06 | -0.04 | 0.670 | 3 | 0.00 | 0.50 | 1.000 |
| symbol×cell×age | e4 | 90 | +0.02 | -0.00 | 0.410 | -0.02 | -0.02 | 0.500 | 9 | 0.67 | 0.50 | 0.343 |
| symbol×cell×age | e24 | 90 | +0.02 | +0.01 | 0.490 | -0.04 | -0.02 | 0.640 | 2 | 0.50 | 0.50 | 0.560 |
| cell×age | m4 | 31 | +0.16 | +0.01 | 0.220 | -0.02 | +0.04 | 0.790 | 3 | 0.67 | 0.50 | 0.370 |
| cell×age | m24 | 31 | +0.05 | +0.08 | 0.560 | +0.15 | +0.13 | 0.410 | 7 | 0.71 | 0.60 | 0.230 |
| symbol×cell | m4 | 32 | -0.04 | -0.02 | 0.540 | +0.06 | +0.05 | 0.360 | 4 | 0.50 | 0.50 | 0.520 |
| symbol×cell | m24 | 32 | +0.05 | -0.01 | 0.370 | +0.02 | +0.09 | 0.830 | 8 | 0.50 | 0.50 | 0.600 |
| symbol×cell×age | m4 | 90 | +0.06 | -0.00 | 0.300 | +0.05 | +0.03 | 0.340 | 7 | 0.43 | 0.50 | 0.680 |
| symbol×cell×age | m24 | 90 | +0.03 | +0.02 | 0.420 | +0.08 | +0.07 | 0.440 | 11 | 0.55 | 0.50 | 0.430 |

## 2. Where: pooled cell × age map of e₂₄ (ATR), first half vs second half

| cell | age | H1 mean | H1 t | H2 mean | H2 t | days H1/H2 |
|---|---|---|---|---|---|---|
| med_high_vol|bear | 24–48h | -3.464 | -0.9 | -2.197 | -1.3 | 14/27 |
| med_low_vol|bear | 12–24h | -3.230 | -2.5 | -0.344 | -0.3 | 77/85 |
| low_vol|bull | 24–48h | -2.109 | -0.8 | +0.749 | +0.3 | 23/11 |
| med_low_vol|bear | 6–12h | -1.371 | -1.7 | -0.732 | -0.9 | 176/175 |
| low_vol|bull | 12–24h | -1.368 | -1.3 | +1.262 | +0.8 | 83/60 |
| med_high_vol|bear | 12–24h | -1.127 | -0.8 | -0.972 | -0.7 | 71/84 |
| med_low_vol|bull | 0–6h | -0.917 | -1.9 | -0.454 | -0.7 | 405/450 |
| med_high_vol|bear | 6–12h | -0.856 | -1.0 | -0.527 | -0.5 | 164/157 |
| low_vol|bear | 0–6h | -0.661 | -1.0 | +0.502 | +0.7 | 319/250 |
| high_vol|bear | 24–48h | -0.624 | -0.3 | +3.364 | +1.5 | 18/20 |
| high_vol|bull | 0–6h | -0.616 | -1.0 | +0.320 | +0.5 | 358/354 |
| high_vol|bear | 0–6h | -0.604 | -1.0 | +0.358 | +0.5 | 338/296 |
| high_vol|bull | 6–12h | -0.590 | -0.6 | -1.836 | -2.0 | 160/160 |
| high_vol|bear | 12–24h | -0.441 | -0.4 | -0.746 | -0.6 | 66/75 |
| low_vol|bull | 6–12h | -0.414 | -0.5 | -0.195 | -0.2 | 164/119 |
| med_low_vol|bull | 6–12h | -0.248 | -0.3 | +0.602 | +0.7 | 200/224 |
| med_high_vol|bull | 12–24h | -0.144 | -0.1 | -0.917 | -0.6 | 78/74 |
| med_high_vol|bull | 0–6h | -0.110 | -0.2 | +1.105 | +2.0 | 370/427 |
| med_low_vol|bear | 0–6h | -0.050 | -0.1 | -0.014 | -0.0 | 359/364 |
| low_vol|bull | 0–6h | +0.007 | +0.0 | -1.257 | -1.9 | 352/277 |
| med_high_vol|bear | 0–6h | +0.147 | +0.3 | +0.729 | +1.2 | 387/349 |
| med_high_vol|bull | 6–12h | +0.571 | +0.6 | +1.433 | +1.3 | 155/180 |
| low_vol|bear | 12–24h | +0.947 | +0.7 | -0.506 | -0.3 | 77/57 |
| med_low_vol|bear | 24–48h | +1.050 | +0.3 | -1.170 | -0.3 | 11/21 |
| high_vol|bear | 6–12h | +1.087 | +1.2 | +0.027 | +0.0 | 142/135 |
| med_low_vol|bull | 12–24h | +1.371 | +0.8 | +0.624 | +0.7 | 92/123 |
| low_vol|bear | 6–12h | +1.876 | +1.9 | +0.352 | +0.3 | 143/108 |
| high_vol|bull | 12–24h | +2.052 | +1.2 | -2.168 | -1.6 | 66/80 |
| med_low_vol|bull | 24–48h | +2.151 | +0.7 | -3.234 | -1.1 | 14/22 |
| high_vol|bull | 24–48h | +3.229 | +1.0 | +2.744 | +0.8 | 17/14 |
| low_vol|bear | 24–48h | +3.764 | +1.6 | -6.040 | -1.2 | 16/10 |

## 3. When (horizon): mean e_h by cell, full sample (ATR)

| cell | e1 | e4 | e12 | e24 | e48 |
|---|---|---|---|---|---|
| high_vol|bear | +0.016 | +0.078 | +0.153 | +0.469 | +1.118 |
| high_vol|bull | +0.014 | +0.087 | -0.086 | -0.315 | -0.797 |
| low_vol|bear | -0.001 | +0.037 | +0.220 | +0.382 | +0.156 |
| low_vol|bull | -0.057 | -0.146 | -0.041 | -0.414 | -0.985 |
| med_high_vol|bear | -0.006 | +0.020 | -0.086 | -0.277 | -0.421 |
| med_high_vol|bull | +0.042 | +0.118 | +0.147 | +0.521 | +1.409 |
| med_low_vol|bear | +0.002 | -0.024 | -0.046 | -0.295 | -0.875 |
| med_low_vol|bull | -0.019 | -0.082 | -0.101 | -0.126 | -0.604 |

## 4. When (age): does decay with regime age repeat quarter to quarter?

| quarter | signals | slope of e₂₄ on log(1+age) | share age ≥ 12h |
|---|---|---|---|
| 2025Q1 | 4769 | -0.347 | 0.200 |
| 2025Q2 | 5679 | +0.805 | 0.185 |
| 2025Q3 | 5730 | -0.338 | 0.201 |
| 2025Q4 | 5633 | +0.488 | 0.177 |
| 2026Q1 | 7429 | -0.935 | 0.202 |
| 2026Q2 | 7876 | -0.342 | 0.209 |
| 2026Q3 | 7268 | +0.133 | 0.191 |

Negative-slope quarters: 57% · null median 43% · p 0.460

## 5. Regime-exit hazard by age band, per quarter (exits per hour at risk)

| quarter | runs | median length h | 0–6h | 6–12h | 12–24h | 24–48h | ≥48h |
|---|---|---|---|---|---|---|---|
| 2025Q1 | 535 | 7 | 0.089 | 0.109 | 0.125 | 0.110 | 0.000 |
| 2025Q2 | 653 | 7 | 0.087 | 0.129 | 0.121 | 0.119 | – |
| 2025Q3 | 659 | 7 | 0.090 | 0.118 | 0.128 | 0.098 | 1.000 |
| 2025Q4 | 676 | 7 | 0.096 | 0.132 | 0.111 | 0.118 | 0.125 |
| 2026Q1 | 857 | 6 | 0.095 | 0.114 | 0.134 | 0.103 | 0.067 |
| 2026Q2 | 905 | 6 | 0.100 | 0.114 | 0.107 | 0.138 | 0.500 |
| 2026Q3 | 865 | 6 | 0.098 | 0.121 | 0.131 | 0.107 | 0.118 |
