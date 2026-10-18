# PBM Phase B calibration audit — log-score / Brier / reliability

Generated 2026-10-16 23:11 UTC · since 2026-08-17 · join tol 60s · bins 10 · bootstrap 400 (seed 13)

The missing instrument. `measure_pbm_drift_auc.py` scores barrier-orientation **AUC** — a rank statistic, invariant to any monotone recalibration. A layer predicting 0.60 when reality is 0.30 scores the same AUC as one predicting 0.30. **AUC cannot see overconfidence.** This harness measures Brier, log-loss and the reliability curve of each probability layer, era-clean, off the `nt_pbm_shadow` ledger.

## The bar (pre-registered)

Phases doc §5, **Phase A acceptance**: *"posterior reliability ≥ table lookup on the resolved book"*. Operationalised on proper scores: the posterior must show a **lower Brier** than the incumbent table AND a **smaller |bias|**, with the posterior−table Brier difference's bootstrap 95% CI **not straddling zero from above** (CI upper < 0). No discretion — the numbers decide.

## Era discipline

- cutover marker `nt_calibration_cutover`: **2026-10-02T00:00:00Z** (the one marker reachability, the bias loop, atr_reopt and the Phase E harness all exclude on).
- filters applied: `nt_pbm_shadow.era='live'` **AND** `nt_signal_sim.contaminated=false` **AND** `ts >= cutover` — the two are not redundant (see the skill's era-clean standard).
- rows dropped pre-cutover / dirty: **0**

## Population & join

- **symbol filter**: AAPL, AUDUSD, BTCUSD, EURUSD, GBPUSD, US30USD, US500, USDCAD, USDCHF, XAGUSD, XAUAUD, XAUEUR, XAUUSD (13 of the active book)
- `nt_pbm_shadow` rows fetched: **1752** · joined to `nt_signal_sim`: **1752** (100.0%) · unmatched (no candidate) 0, out-of-tol 0.
- join = `(symbol, signal)` + NEAREST `ts` within **60s** — the shadow `ts` is the sweep wall-clock (`orchestrator.py:4292`), not the sim row's ts, so an exact `(symbol, ts, signal)` join fails.
- |Δt| median **2.55s** · p90 4.65s · max 21.63s
- era-clean joined: **1752** · scored (all outcomes) **1111** · decided (tp/sl) **1111**
- outcome mix: tp **313** / sl **798** / expired **0** · realized P(tp_hit) **0.2817** over the scored population
- bucket mix: 1-2: 58, 4+: 1024, <0.5: 29
- vol-tier mix: high_vol: 387, low_vol: 154, med_high_vol: 344, med_low_vol: 226
- coverage floor for a verdict: **500** scored AND paired rows — have **1111** scored / **1077** paired.

## Headline — binary P(TP first), all era-clean rows (y = 1[tp_hit])

*Every layer's `p_W` names P(tp_hit); expired rows enter as y=0 (survivorship-honest, not dropped). BSS is the Brier skill vs the constant base-rate predictor — >0 beats climatology, 0 ties it, <0 is worse. ECE/MCE are over equal-width bins.*

| layer | n | mean pred | realized | bias | Brier | BSS | log-loss | ECE | MCE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **PBM posterior p_W** | 1111 | 0.3086 | 0.2817 | +0.0269 | **0.2222** | -0.0980 | 0.6511 | 0.1202 | 0.4483 |
| **reachability table (incumbent)** | 1077 | 0.2678 | 0.2897 | -0.0219 | **0.2326** | -0.1303 | 0.7097 | 0.1838 | 0.7000 |
| **raw directional blend (pre-bracket)** | 1111 | 0.5972 | 0.2817 | +0.3155 | **0.3631** | -0.7944 | 1.0110 | 0.3264 | 0.7287 |
| **analytic barrier null** | 1111 | 0.2899 | 0.2817 | +0.0081 | **0.2049** | -0.0125 | 0.6030 | 0.0737 | 0.1936 |

*Decided-only variant omitted: **0 expired rows** in this window, so the decided population is identical to the all-rows population — the 48-bar deadline has not matured yet, so no expiry mass exists to separate them.*

## Reliability curves (deciles)

### PBM posterior p_W

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 122 | 0.062 | 0.385 | -0.323 |
| [0.1,0.2) | 116 | 0.143 | 0.250 | -0.107 |
| [0.2,0.3) | 180 | 0.258 | 0.144 | +0.113 |
| [0.3,0.4) | 455 | 0.358 | 0.349 | +0.008 |
| [0.4,0.5) | 189 | 0.442 | 0.254 | +0.188 |
| [0.5,0.6) | 49 | 0.530 | 0.082 | +0.448 |

### reachability table (incumbent)

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 170 | 0.024 | 0.171 | -0.147 |
| [0.1,0.2) | 269 | 0.146 | 0.279 | -0.133 |
| [0.2,0.3) | 250 | 0.264 | 0.464 | -0.200 |
| [0.3,0.4) | 119 | 0.347 | 0.143 | +0.204 |
| [0.4,0.5) | 106 | 0.432 | 0.302 | +0.130 |
| [0.5,0.6) | 113 | 0.527 | 0.319 | +0.208 |
| [0.6,0.7) | 46 | 0.636 | 0.130 | +0.505 |
| [0.7,0.8) | 2 | 0.700 | 0.000 | +0.700 |
| [0.9,1.0) | 2 | 1.000 | 0.500 | +0.500 |

### raw directional blend (pre-bracket)

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 2 | 0.093 | 0.000 | +0.093 |
| [0.1,0.2) | 50 | 0.164 | 0.180 | -0.016 |
| [0.2,0.3) | 71 | 0.250 | 0.183 | +0.067 |
| [0.3,0.4) | 109 | 0.355 | 0.404 | -0.048 |
| [0.4,0.5) | 148 | 0.454 | 0.358 | +0.096 |
| [0.5,0.6) | 192 | 0.542 | 0.349 | +0.193 |
| [0.6,0.7) | 114 | 0.650 | 0.272 | +0.378 |
| [0.7,0.8) | 150 | 0.750 | 0.233 | +0.517 |
| [0.8,0.9) | 189 | 0.847 | 0.228 | +0.620 |
| [0.9,1.0) | 86 | 0.938 | 0.209 | +0.729 |

### analytic barrier null

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 89 | 0.054 | 0.056 | -0.003 |
| [0.1,0.2) | 187 | 0.143 | 0.337 | -0.194 |
| [0.2,0.3) | 123 | 0.266 | 0.187 | +0.079 |
| [0.3,0.4) | 712 | 0.362 | 0.312 | +0.050 |

## Three-outcome scoring (tp / sl / expired) — all era-clean rows

*Posterior carries the native triple; null is recomputed from the shadow's stored geometry (`barrier_null(tp_atr, sl_atr, t_bars, sigma)`); table is reconstructed from `(p_win_bracket, p_resolve)` as p_E = 1 − p_resolve, p_L = p_resolve − p_win. `raw_directional` stores no resolve probability, so it is scored binary-only — stated, not faked.*

| layer | n | multi-class Brier | multi-class log-loss | mean p_E |
|---|---:|---:|---:|---:|
| **posterior** | 1111 | 0.4788 | 0.7487 | 0.0827 |
| **table** | 1077 | 0.5141 | 0.8359 | 0.1172 |
| **null** | 1111 | 0.4604 | 0.7204 | 0.1036 |

> ⚠️ **No expiry mass in this window** — every scored row resolved tp/sl, so the three-outcome score is effectively two-outcome here. The posterior's mean p_E (0.0827) is a PREDICTION the window cannot yet validate; re-run once the 48-bar deadline matures.

## Conditional read — q = P(tp | resolve) (the expiry-free half)

*The part of the bar that does not depend on the expiry LEVEL. On a young window with zero realized expiries the full-triple score is dominated by the resolve-mass term below, so this is the durable read. `degenerate q∈{0,1}` counts hard 0/1 probabilities — under a log score the 1e-6 clip turns each into a −13.8 penalty that can manufacture an advantage.*

| layer | n | mean q | realized | bias | Brier | log-loss | degenerate q∈{0,1} |
|---|---:|---:|---:|---:|---:|---:|---:|
| PBM posterior p_W | 1111 | 0.3309 | 0.2817 | +0.0491 | 0.2253 | 0.6463 | 0 |
| reachability table (incumbent) | 1077 | 0.3049 | 0.2897 | +0.0152 | 0.2237 | 0.6852 | 82 |
| analytic barrier null | 1111 | 0.3154 | 0.2817 | +0.0337 | 0.1994 | 0.5832 | 0 |

- **ΔBrier (posterior − table)** on 1077 paired rows: **+0.0048**, 95% CI [-0.0046, +0.0137] — CI straddles 0 ⇒ **NOT significant**.
- **ΔBrier excluding degenerate q** on 995 rows: **+0.0040**, 95% CI [-0.0048, +0.0127] — removes the 1e-6-clip artifact so any advantage is attributable.
- **Δlog-loss excluding degenerate q** on 995 rows: **+0.0025** — the same removal applied to the log score; if this is ~0 while the all-rows Δlog-loss is large, the gap WAS the clip.

## Log-score decomposition — how much of the delta is the expiry term

`log p(outcome) = log q + log(p_W + p_L)` exactly, so the paired delta splits into:

| term | mean Δ (posterior − table) | 95% CI |
|---|---:|---|
| full  log p(outcome) | +0.0774 | [+0.0296, +0.1356] |
| conditional  log q | +0.0318 | [-0.0168, +0.0822] |
| resolve mass  log(p_W+p_L) | +0.0457 | [+0.0271, +0.0658] |

- implied resolve mass: posterior **0.9003** vs table **0.8601** — realized **1.0000** (0 expired in this window).
- ⚠️ the resolve-mass term is **+0.0457** of the **+0.0774** full delta on a **zero-expiry window** — it is not directional skill and its sign is not durable until expiries land.

## Significance — posterior vs incumbent table (aligned rows)

- common rows: **1077**
- Brier posterior **0.2253** vs table **0.2326**
- ΔBrier (posterior − table) 95% CI **[-0.0154, 0.0024]** — straddles/includes 0 ⇒ not separable on this population.

## Per-bucket split (bracket_bucket)

| bucket | layer | n | mean pred | realized | bias | Brier | BSS | ECE |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1-2 | PBM posterior p_W | 58 | 0.356 | 0.121 | +0.235 | 0.1598 | -0.506 | 0.235 |
| 1-2 | reachability table (incumbent) | 55 | 0.195 | 0.127 | +0.067 | 0.1816 | -0.635 | 0.242 |
| 1-2 | raw directional blend (pre-bracket) | 58 | 0.650 | 0.121 | +0.529 | 0.4150 | -2.911 | 0.533 |
| 1-2 | analytic barrier null | 58 | 0.377 | 0.121 | +0.256 | 0.1718 | -0.619 | 0.256 |
| 4+ | PBM posterior p_W | 1024 | 0.305 | 0.298 | +0.007 | 0.2283 | -0.092 | 0.137 |
| 4+ | reachability table (incumbent) | 1022 | 0.272 | 0.298 | -0.027 | 0.2353 | -0.124 | 0.182 |
| 4+ | raw directional blend (pre-bracket) | 1024 | 0.590 | 0.298 | +0.292 | 0.3539 | -0.692 | 0.309 |
| 4+ | analytic barrier null | 1024 | 0.283 | 0.298 | -0.015 | 0.2083 | +0.004 | 0.056 |
| <0.5 | PBM posterior p_W | 29 | 0.341 | 0.034 | +0.307 | 0.1291 | -2.879 | 0.307 |
| <0.5 | raw directional blend (pre-bracket) | 29 | 0.745 | 0.034 | +0.711 | 0.5842 | -16.548 | 0.711 |
| <0.5 | analytic barrier null | 29 | 0.375 | 0.034 | +0.341 | 0.1493 | -3.485 | 0.341 |

## Per-vol-tier split (vol_tier)

| vol_tier | layer | n | mean pred | realized | bias | Brier | BSS | ECE |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| high_vol | PBM posterior p_W | 387 | 0.296 | 0.318 | -0.022 | 0.2264 | -0.044 | 0.185 |
| high_vol | reachability table (incumbent) | 367 | 0.263 | 0.332 | -0.070 | 0.2403 | -0.083 | 0.202 |
| high_vol | raw directional blend (pre-bracket) | 387 | 0.628 | 0.318 | +0.310 | 0.3860 | -0.780 | 0.336 |
| high_vol | analytic barrier null | 387 | 0.269 | 0.318 | -0.049 | 0.2160 | +0.004 | 0.074 |
| low_vol | PBM posterior p_W | 154 | 0.308 | 0.253 | +0.054 | 0.1926 | -0.019 | 0.183 |
| low_vol | reachability table (incumbent) | 149 | 0.289 | 0.262 | +0.027 | 0.2049 | -0.060 | 0.133 |
| low_vol | raw directional blend (pre-bracket) | 154 | 0.590 | 0.253 | +0.336 | 0.3814 | -1.017 | 0.367 |
| low_vol | analytic barrier null | 154 | 0.314 | 0.253 | +0.061 | 0.1773 | +0.062 | 0.065 |
| med_high_vol | PBM posterior p_W | 344 | 0.316 | 0.302 | +0.014 | 0.2435 | -0.154 | 0.193 |
| med_high_vol | reachability table (incumbent) | 342 | 0.275 | 0.304 | -0.029 | 0.2422 | -0.144 | 0.193 |
| med_high_vol | raw directional blend (pre-bracket) | 344 | 0.576 | 0.302 | +0.273 | 0.3298 | -0.564 | 0.305 |
| med_high_vol | analytic barrier null | 344 | 0.283 | 0.302 | -0.020 | 0.2116 | -0.003 | 0.078 |
| med_low_vol | PBM posterior p_W | 226 | 0.319 | 0.208 | +0.111 | 0.2026 | -0.230 | 0.210 |
| med_low_vol | reachability table (incumbent) | 219 | 0.250 | 0.215 | +0.036 | 0.2234 | -0.325 | 0.214 |
| med_low_vol | raw directional blend (pre-bracket) | 226 | 0.583 | 0.208 | +0.375 | 0.3621 | -1.199 | 0.375 |
| med_low_vol | analytic barrier null | 226 | 0.320 | 0.208 | +0.112 | 0.1944 | -0.180 | 0.192 |

## Per-symbol (all era-clean rows)

| symbol | n | n paired | posterior Brier | table Brier | ΔBrier (post−table) | 95% CI | posterior bias | posterior BSS |
|---|---:|---:|---:|---:|---:|---|---:|---:|
| AAPL | 53 | 24 | 0.1102 | 0.0267 | +0.0607 | [—, —] | +0.301 | -4.956 |
| AUDUSD | 103 | 103 | 0.2626 | 0.3185 | -0.0559 | [-0.0891, -0.0260] | -0.080 | -0.051 |
| BTCUSD | 121 | 121 | 0.1667 | 0.1477 | +0.0190 | [+0.0043, +0.0319] | -0.036 | -0.083 |
| EURUSD | 85 | 85 | 0.1784 | 0.2706 | -0.0922 | [-0.1177, -0.0668] | +0.275 | -0.885 |
| GBPUSD | 87 | 87 | 0.2690 | 0.3326 | -0.0635 | [-0.1144, -0.0117] | -0.031 | -0.094 |
| US30USD | 59 | 56 | 0.1723 | 0.1953 | -0.0213 | [-0.0783, +0.0302] | +0.215 | -0.470 |
| US500 | 42 | 40 | 0.0217 | 0.0509 | -0.0281 | [-0.1008, +0.0472] | -0.008 | +0.064 |
| USDCAD | 108 | 108 | 0.2523 | 0.2634 | -0.0110 | [-0.0391, +0.0156] | -0.011 | -0.062 |
| USDCHF | 103 | 103 | 0.2595 | 0.2448 | +0.0147 | [-0.0014, +0.0306] | +0.015 | -0.075 |
| XAGUSD | 123 | 123 | 0.1879 | 0.1869 | +0.0010 | [-0.0203, +0.0193] | +0.095 | -0.236 |
| XAUAUD | 102 | 102 | 0.2634 | 0.2705 | -0.0071 | [-0.0417, +0.0286] | +0.020 | -0.204 |
| XAUEUR | 66 | 66 | 0.5209 | 0.4076 | +0.1133 | [+0.0866, +0.1430] | -0.469 | -1.182 |
| XAUUSD | 59 | 59 | 0.0981 | 0.0529 | +0.0452 | [+0.0315, +0.0573] | +0.175 | -1.032 |
*ΔBrier < 0 ⇒ the posterior is better calibrated on that symbol. `n paired` is the rows carrying BOTH layers; a CI prints only at n paired ≥ 30 — thin symbols are diagnostics, not votes.*

## Verdict (mechanically applied bar — no discretion)

### Pooled flip bar (coverage-gated) — **PASS-POINT**

- ΔBrier -0.0073 < 0 but CI upper bound +0.0024 ≥ 0 — point estimate clears, confidence does not: keep collecting, do not flip — RELATIVE ONLY: posterior BSS -0.0980 < 0, i.e. still worse than the constant base-rate predictor; a table that is itself below climatology is the wrong benchmark to pass.

> Point estimate clears the bar but the confidence interval does not. **Keep collecting ledger depth; do not flip.**

### Per-layer scorecard (vs the incumbent table)

- **PBM posterior p_W**: **STAYS-BEHIND** — Brier 0.2222 vs table 0.2326 ✓; |bias| 0.0269 vs table 0.0219 ✗; ΔBrier 95% CI [-0.0154, 0.0024].
- **reachability table (incumbent)**: **INCUMBENT** — Brier 0.2326, |bias| 0.0219.
- **raw directional blend (pre-bracket)**: **STAYS-BEHIND** — Brier 0.3631 vs table 0.2326 ✗; |bias| 0.3155 vs table 0.0219 ✗.
- **analytic barrier null**: **BEATS-TABLE** — Brier 0.2049 vs table 0.2326 ✓; |bias| 0.0081 vs table 0.0219 ✓.

## Notes / honesty

- **`nt_signal_sim.p_win_dir` is NOT the raw directional layer.** `orchestrator.py:5650` sets `_p_win_dir = _p_win_bracket`, so `p_win_dir == p_win_bracket == p_win` on every live row (verified: 1181/1181 comparable rows). The genuine directional series is `predicted_p_win_raw` (`orchestrator.py:4624`, captured before the bracket swap at `:4631`). Scoring `p_win_dir` as "directional" would silently score the table twice.
- **`nt_pbm_shadow.outcome` is NULL for every row** (migration 211: backfilled by the acceptance harness; the resolver writes the cell posterior, not this column). The join to `nt_signal_sim` is therefore the outcome source — this harness stays read-only and does not backfill.
- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar demands the CI clear zero regardless.
- **Brier/log-loss are proper scores**: they reward honesty, not just ranking. A layer can win on AUC and lose on Brier — that divergence IS the overconfidence signal this instrument exists to surface.
- Expired rows are scored (as y=0 for tp_hit) rather than dropped — the survivorship the Phase A P0 list closes. Dropping them would flatter every layer by removing the outcomes no layer predicted.

