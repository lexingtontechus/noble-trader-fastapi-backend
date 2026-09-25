# Kairos V3 step 2 + production benchmark: calibration carryover

Generated 2026-09-25 22:54 UTC by `scripts/kairos_calibration_step2.py` (scope §14). Targets: 449 qualified TP/SL signals (S2), 14 symbols, 2026-08-23 → 2026-09-24. Evidence: same symbol, any tier, TP/SL.

## A. Production benchmark vs latency vs half-life (walk-forward Δ Brier on S2; negative = better)

Production rule = `fetch_calibration_bias`: 7d entry window, episode merge, 3-day half-life on entry age, ≥ 28 raw records, ≥ 2 episodes. Nightly = evidence usable only after the next 01:00 UTC resolver run.

| rule | S2 scorable (own) | Δ Brier own ± SE | Δ Brier on common subset ± SE | corr(x, S2 err) |
|---|---|---|---|---|
| production rule, nightly resolver (today) | 139 (31%) | -0.0349 ± 0.0135 | -0.0253 ± 0.0122 | +0.091 |
| production rule, hourly resolver | 181 (40%) | -0.0625 ± 0.0139 | -0.0429 ± 0.0125 | +0.166 |
| 3h half-life, nightly resolver | 354 (79%) | -0.0494 ± 0.0065 | -0.0419 ± 0.0106 | +0.209 |
| 3h half-life, hourly resolver | 390 (87%) | -0.0443 ± 0.0047 | -0.0938 ± 0.0159 | +0.195 |
| 3h half-life, instant (step 1 bound) | 392 (87%) | -0.0534 ± 0.0055 | -0.1017 ± 0.0168 | +0.224 |
| 24h half-life, hourly resolver | 390 (87%) | -0.0338 ± 0.0038 | -0.0459 ± 0.0130 | +0.118 |

Common subset: 162 targets. Uncorrected Brier on it: 0.3559.

## B. Step 2: does carryover survive a state change? (0–6h evidence, known at exit)

β = slope of S2's error on the evidence mean error. Null = S2 targets circularly shifted within symbol (1000 draws). p = share of null draws with β_same − β_diff ≥ observed.

| split | β same [n] | β different [n] | β same − β diff | null diff median [2.5, 97.5] | p (diff) | p (β same > null) |
|---|---|---|---|---|---|---|
| fired from same regime as S2 | +0.441 [136] | +0.181 [285] | +0.260 | +0.165 [-0.104, +0.431] | 0.277 | 0.022 |
| same signal direction as S2 | +0.282 [256] | +0.103 [221] | +0.178 | +0.094 [-0.168, +0.357] | 0.277 | 0.038 |
| symbol regime unchanged since evidence resolved | +0.548 [171] | +0.253 [257] | +0.295 | +0.129 [-0.108, +0.377] | 0.101 | 0.000 |
| same regime, decayed h=3h | +0.454 [136] | +0.195 [285] | +0.260 | +0.169 [-0.103, +0.433] | 0.275 | 0.018 |
| regime unchanged since resolved, decayed h=3h | +0.561 [171] | +0.263 [257] | +0.298 | +0.128 [-0.112, +0.371] | 0.093 | 0.000 |

Context: of 0–6h evidence, 23% was fired from S2's regime; for 31% the symbol's regime at resolution already equalled S2's.

_Runtime 97s._
