# Kairos repeatability: is the edge/decay map stable out of sample? (n_confirm 1)

Generated 2026-09-25 06:13 UTC by `scripts/kairos_repeatability.py` · 47,304 signals · 5 symbols · 100 circular-shift null draws · runtime 368s.

e_h = label-following move in ATR **minus the symbol's trailing 30-day drift** (no look-ahead). m_h = raw label-following move (the P1 measure). ρ = Spearman rank correlation of the map between blocks; p = share of null draws (labels circularly shifted vs prices) at or above the observed value.

## 1. Does the map repeat?

| map | measure | keys | ρ H1→H2 | null ρ | p | ρ consecutive quarters | null ρ | p | |t|≥2 keys in H1 | same sign in H2 | null | p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cell×age | e4 | 13 | -0.16 | -0.03 | 0.660 | -0.32 | -0.04 | 0.980 | 0 | nan | 1.00 | nan |
| cell×age | e24 | 13 | -0.26 | +0.03 | 0.730 | +0.05 | -0.01 | 0.390 | 1 | 1.00 | 1.00 | 0.607 |
| symbol×cell | e4 | 32 | -0.10 | -0.03 | 0.680 | -0.19 | -0.05 | 0.990 | 1 | 1.00 | 0.50 | 0.456 |
| symbol×cell | e24 | 32 | +0.30 | -0.01 | 0.050 | -0.20 | -0.12 | 0.890 | 0 | nan | 0.25 | nan |
| symbol×cell×age | e4 | 32 | -0.10 | -0.03 | 0.660 | -0.21 | -0.05 | 0.990 | 1 | 1.00 | 0.50 | 0.446 |
| symbol×cell×age | e24 | 32 | +0.28 | -0.01 | 0.060 | -0.20 | -0.12 | 0.920 | 0 | nan | 0.25 | nan |
| cell×age | m4 | 13 | -0.04 | +0.11 | 0.650 | -0.01 | +0.14 | 0.820 | 4 | 0.75 | 0.50 | 0.383 |
| cell×age | m24 | 13 | -0.13 | +0.23 | 0.850 | +0.36 | +0.28 | 0.230 | 8 | 1.00 | 0.71 | 0.110 |
| symbol×cell | m4 | 32 | -0.09 | -0.10 | 0.430 | -0.05 | +0.07 | 0.990 | 5 | 0.40 | 0.50 | 0.790 |
| symbol×cell | m24 | 32 | +0.46 | -0.10 | 0.000 | +0.10 | +0.18 | 0.960 | 11 | 0.91 | 0.41 | 0.000 |
| symbol×cell×age | m4 | 32 | -0.12 | -0.12 | 0.540 | -0.06 | +0.08 | 1.000 | 5 | 0.40 | 0.50 | 0.790 |
| symbol×cell×age | m24 | 32 | +0.48 | -0.10 | 0.000 | +0.10 | +0.18 | 0.950 | 11 | 0.91 | 0.42 | 0.000 |

## 2. Where: pooled cell × age map of e₂₄ (ATR), first half vs second half

| cell | age | H1 mean | H1 t | H2 mean | H2 t | days H1/H2 |
|---|---|---|---|---|---|---|
| med_low_vol|bear | 6–12h | -5.211 | -1.6 | +3.067 | +0.8 | 11/17 |
| high_vol|bear | 0–6h | -0.954 | -2.4 | -0.002 | -0.0 | 756/700 |
| high_vol|bull | 6–12h | -0.827 | -0.3 | -2.954 | -0.9 | 11/12 |
| low_vol|bear | 0–6h | -0.435 | -1.0 | -1.133 | -2.2 | 703/584 |
| med_high_vol|bull | 0–6h | -0.360 | -1.0 | -0.154 | -0.4 | 876/871 |
| low_vol|bull | 0–6h | -0.236 | -0.6 | -0.052 | -0.1 | 753/649 |
| high_vol|bull | 0–6h | -0.186 | -0.5 | -0.179 | -0.4 | 776/748 |
| low_vol|bull | 6–12h | -0.138 | -0.1 | +4.836 | +2.6 | 21/13 |
| med_high_vol|bear | 0–6h | -0.103 | -0.3 | +0.033 | +0.1 | 858/832 |
| med_low_vol|bear | 0–6h | -0.045 | -0.1 | -0.214 | -0.5 | 852/835 |
| med_low_vol|bull | 0–6h | +0.075 | +0.2 | -0.003 | -0.0 | 885/890 |
| med_low_vol|bull | 6–12h | +3.753 | +1.3 | -1.874 | -0.9 | 15/35 |
| low_vol|bear | 6–12h | +6.085 | +1.8 | -2.937 | -0.7 | 14/10 |

## 3. When (horizon): mean e_h by cell, full sample (ATR)

| cell | e1 | e4 | e12 | e24 | e48 |
|---|---|---|---|---|---|
| high_vol|bear | +0.010 | +0.095 | +0.010 | +0.235 | +0.650 |
| high_vol|bull | +0.005 | +0.099 | +0.050 | +0.053 | +0.070 |
| low_vol|bear | -0.012 | -0.126 | -0.213 | -0.171 | +0.028 |
| low_vol|bull | -0.007 | -0.011 | +0.024 | -0.162 | -0.873 |
| med_high_vol|bear | +0.028 | +0.139 | +0.110 | +0.109 | -0.156 |
| med_high_vol|bull | +0.008 | -0.016 | -0.172 | -0.073 | +0.024 |
| med_low_vol|bear | +0.006 | -0.049 | -0.084 | -0.032 | -0.560 |
| med_low_vol|bull | +0.002 | -0.081 | -0.213 | -0.066 | -0.182 |

## 4. When (age): does decay with regime age repeat quarter to quarter?

| quarter | signals | slope of e₂₄ on log(1+age) | share age ≥ 12h |
|---|---|---|---|
| 2025Q1 | 5038 | +0.536 | 0.007 |
| 2025Q2 | 5959 | +0.126 | 0.002 |
| 2025Q3 | 6078 | -0.054 | 0.004 |
| 2025Q4 | 5971 | +0.221 | 0.001 |
| 2026Q1 | 7792 | +0.843 | 0.002 |
| 2026Q2 | 8135 | -0.435 | 0.004 |
| 2026Q3 | 7559 | -0.330 | 0.003 |

Negative-slope quarters: 43% · null median 57% · p 0.810

## 5. Regime-exit hazard by age band, per quarter (exits per hour at risk)

| quarter | runs | median length h | 0–6h | 6–12h | 12–24h | 24–48h | ≥48h |
|---|---|---|---|---|---|---|---|
| 2025Q1 | 3479 | 1 | 0.688 | 0.256 | 0.143 | 0.333 | – |
| 2025Q2 | 4074 | 1 | 0.676 | 0.379 | 0.200 | – | – |
| 2025Q3 | 3976 | 1 | 0.652 | 0.241 | 0.308 | – | – |
| 2025Q4 | 4093 | 1 | 0.676 | 0.342 | 0.250 | – | – |
| 2026Q1 | 5173 | 1 | 0.660 | 0.379 | 0.278 | – | – |
| 2026Q2 | 5332 | 1 | 0.654 | 0.306 | 0.138 | 0.000 | – |
| 2026Q3 | 5049 | 1 | 0.656 | 0.333 | 0.200 | – | – |
