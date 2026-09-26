# Kairos V3: evidence-tier split for the hourly calibration lever

Generated 2026-09-26 00:46 UTC by `scripts/kairos_calibration_tier_split.py`. Targets (S2): 449 qualified TP/SL signals, 14 symbols, 2026-08-23 → 2026-09-24.

## Evidence pool (resolved TP/SL, light sweep)

| tier | n | share | TP rate | mean err (p_win − y) | tier source stamped / reconstructed |
|---|---|---|---|---|---|
| qualified | 449 | 1.2% | 0.258 | +0.341 | 2 / 447 |
| suppressed | 5224 | 14.4% | 0.247 | +0.386 | 36 / 5188 |
| gate_fail | 30528 | 84.3% | 0.266 | +0.146 | 114 / 30414 |

## Walk-forward Δ Brier on S2 (negative = better)

Own = every S2 the rule can score (coverage differs by pool size). Common = the S2 targets every rule can score (104), the fair comparison. corr = correlation of the evidence mean error with S2's error (higher = more informative).

| evidence pool | rule | S2 scorable (own) | Δ Brier own ± SE | Δ Brier common ± SE | corr(x, S2 err) |
|---|---|---|---|---|---|
| all tiers (production today) | production rule, nightly | 139 (31%) | -0.0349 ± 0.0135 | -0.0398 ± 0.0120 | +0.091 |
| all tiers (production today) | production rule, hourly | 181 (40%) | -0.0625 ± 0.0139 | -0.0552 ± 0.0130 | +0.166 |
| all tiers (production today) | 3h half-life, hourly | 390 (87%) | -0.0443 ± 0.0047 | -0.1010 ± 0.0212 | +0.195 |
| qualified + suppressed | production rule, nightly | 172 (38%) | -0.1007 ± 0.0131 | -0.1210 ± 0.0239 | +0.198 |
| qualified + suppressed | production rule, hourly | 206 (46%) | -0.1185 ± 0.0134 | -0.1223 ± 0.0240 | +0.279 |
| qualified + suppressed | 3h half-life, hourly | 364 (81%) | -0.0123 ± 0.0016 | -0.1036 ± 0.0241 | +0.069 |
| gate_fail only | production rule, nightly | 163 (36%) | -0.0371 ± 0.0132 | -0.0092 ± 0.0124 | +0.025 |
| gate_fail only | production rule, hourly | 188 (42%) | -0.0601 ± 0.0142 | -0.0155 ± 0.0141 | +0.137 |
| gate_fail only | 3h half-life, hourly | 389 (87%) | -0.0379 ± 0.0046 | -0.0675 ± 0.0197 | +0.182 |

Uncorrected Brier on the common subset: 0.3609.

_Runtime 37s._
