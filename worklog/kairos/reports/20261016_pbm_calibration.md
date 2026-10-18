# PBM Phase B calibration audit — log-score / Brier / reliability

Generated 2026-10-17 00:27 UTC · since 2026-08-18 · join tol 60s · bins 10 · bootstrap 400 (seed 13)

The missing instrument. `measure_pbm_drift_auc.py` scores barrier-orientation **AUC** — a rank statistic, invariant to any monotone recalibration. A layer predicting 0.60 when reality is 0.30 scores the same AUC as one predicting 0.30. **AUC cannot see overconfidence.** This harness measures Brier, log-loss and the reliability curve of each probability layer, era-clean, off the `nt_pbm_shadow` ledger.

## The bar (pre-registered)

Phases doc §5, **Phase A acceptance**: *"posterior reliability ≥ table lookup on the resolved book"*. Operationalised on proper scores: the posterior must show a **lower Brier** than the incumbent table AND a **smaller |bias|**, with the posterior−table Brier difference's bootstrap 95% CI **not straddling zero from above** (CI upper < 0). No discretion — the numbers decide.

## Era discipline

- cutover marker `nt_calibration_cutover`: **2026-10-02T00:00:00Z** (the one marker reachability, the bias loop, atr_reopt and the Phase E harness all exclude on).
- filters applied: `nt_pbm_shadow.era='live'` **AND** `nt_signal_sim.contaminated=false` **AND** `ts >= cutover` — the two are not redundant (see the skill's era-clean standard).
- rows dropped pre-cutover / dirty: **0**

## Population & join

- `nt_pbm_shadow` rows fetched: **1908** · joined to `nt_signal_sim`: **1908** (100.0%) · unmatched (no candidate) 0, out-of-tol 0.
- join = `(symbol, signal)` + NEAREST `ts` within **60s** — the shadow `ts` is the sweep wall-clock (`orchestrator.py:4292`), not the sim row's ts, so an exact `(symbol, ts, signal)` join fails.
- |Δt| median **2.55s** · p90 4.67s · max 21.63s
- era-clean joined: **1908** · scored (all outcomes) **1215** · decided (tp/sl) **1215**
- outcome mix: tp **366** / sl **849** / expired **0** · realized P(tp_hit) **0.3012** over the scored population
- bucket mix: 1-2: 58, 4+: 1128, <0.5: 29
- vol-tier mix: high_vol: 396, low_vol: 191, med_high_vol: 381, med_low_vol: 247
- coverage floor for a verdict: **500** scored AND paired rows — have **1215** scored / **1181** paired.

## Headline — binary P(TP first), all era-clean rows (y = 1[tp_hit])

*Every layer's `p_W` names P(tp_hit); expired rows enter as y=0 (survivorship-honest, not dropped). BSS is the Brier skill vs the constant base-rate predictor — >0 beats climatology, 0 ties it, <0 is worse. ECE/MCE are over equal-width bins.*

| layer | n | mean pred | realized | bias | Brier | BSS | log-loss | ECE | MCE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **PBM posterior p_W** | 1215 | 0.3144 | 0.3012 | +0.0131 | **0.2270** | -0.0786 | 0.6599 | 0.1162 | 0.4483 |
| **reachability table (incumbent)** | 1181 | 0.2518 | 0.3091 | -0.0572 | **0.2512** | -0.1761 | 0.8742 | 0.2049 | 0.7000 |
| **raw directional blend (pre-bracket)** | 1215 | 0.5956 | 0.3012 | +0.2944 | **0.3561** | -0.6918 | 0.9906 | 0.3119 | 0.7287 |
| **analytic barrier null** | 1215 | 0.2969 | 0.3012 | -0.0044 | **0.2104** | +0.0005 | 0.6141 | 0.0556 | 0.1936 |

*Decided-only variant omitted: **0 expired rows** in this window, so the decided population is identical to the all-rows population — the 48-bar deadline has not matured yet, so no expiry mass exists to separate them.*

## Reliability curves (deciles)

### PBM posterior p_W

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 122 | 0.062 | 0.385 | -0.323 |
| [0.1,0.2) | 116 | 0.143 | 0.250 | -0.107 |
| [0.2,0.3) | 180 | 0.258 | 0.144 | +0.113 |
| [0.3,0.4) | 532 | 0.358 | 0.378 | -0.020 |
| [0.4,0.5) | 216 | 0.441 | 0.273 | +0.168 |
| [0.5,0.6) | 49 | 0.530 | 0.082 | +0.448 |

### reachability table (incumbent)

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 205 | 0.020 | 0.244 | -0.223 |
| [0.1,0.2) | 338 | 0.142 | 0.317 | -0.175 |
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
| [0.1,0.2) | 55 | 0.167 | 0.218 | -0.051 |
| [0.2,0.3) | 76 | 0.250 | 0.211 | +0.039 |
| [0.3,0.4) | 119 | 0.354 | 0.420 | -0.066 |
| [0.4,0.5) | 164 | 0.455 | 0.341 | +0.113 |
| [0.5,0.6) | 211 | 0.541 | 0.365 | +0.176 |
| [0.6,0.7) | 128 | 0.649 | 0.305 | +0.344 |
| [0.7,0.8) | 161 | 0.750 | 0.261 | +0.489 |
| [0.8,0.9) | 213 | 0.845 | 0.263 | +0.583 |
| [0.9,1.0) | 86 | 0.938 | 0.209 | +0.729 |

### analytic barrier null

| bin | n | mean pred | realized | gap (over-conf) |
|---|---:|---:|---:|---:|
| [0.0,0.1) | 89 | 0.054 | 0.056 | -0.003 |
| [0.1,0.2) | 187 | 0.143 | 0.337 | -0.194 |
| [0.2,0.3) | 123 | 0.266 | 0.187 | +0.079 |
| [0.3,0.4) | 816 | 0.363 | 0.337 | +0.026 |

## Three-outcome scoring (tp / sl / expired) — all era-clean rows

*Posterior carries the native triple; null is recomputed from the shadow's stored geometry (`barrier_null(tp_atr, sl_atr, t_bars, sigma)`); table is reconstructed from `(p_win_bracket, p_resolve)` as p_E = 1 − p_resolve, p_L = p_resolve − p_win. `raw_directional` stores no resolve probability, so it is scored binary-only — stated, not faked.*

| layer | n | multi-class Brier | multi-class log-loss | mean p_E |
|---|---:|---:|---:|---:|
| **posterior** | 1215 | 0.4853 | 0.7499 | 0.0766 |
| **table** | 1181 | 0.5811 | 1.0584 | 0.1673 |
| **null** | 1215 | 0.4668 | 0.7223 | 0.0959 |

> ⚠️ **No expiry mass in this window** — every scored row resolved tp/sl, so the three-outcome score is effectively two-outcome here. The posterior's mean p_E (0.0766) is a PREDICTION the window cannot yet validate; re-run once the 48-bar deadline matures.

## Conditional read — q = P(tp | resolve) (the expiry-free half)

*The part of the bar that does not depend on the expiry LEVEL. On a young window with zero realized expiries the full-triple score is dominated by the resolve-mass term below, so this is the durable read. `degenerate q∈{0,1}` counts hard 0/1 probabilities — under a log score the 1e-6 clip turns each into a −13.8 penalty that can manufacture an advantage.*

| layer | n | mean q | realized | bias | Brier | log-loss | degenerate q∈{0,1} |
|---|---:|---:|---:|---:|---:|---:|---:|
| PBM posterior p_W | 1215 | 0.3351 | 0.3012 | +0.0338 | 0.2298 | 0.6553 | 0 |
| reachability table (incumbent) | 1181 | 0.2998 | 0.3091 | -0.0093 | 0.2368 | 0.8245 | 103 |
| analytic barrier null | 1215 | 0.3207 | 0.3012 | +0.0194 | 0.2052 | 0.5957 | 0 |

- **ΔBrier (posterior − table)** on 1181 paired rows: **-0.0040**, 95% CI [-0.0138, +0.0048] — CI straddles 0 ⇒ **NOT significant**.
- **ΔBrier excluding degenerate q** on 1078 rows: **-0.0007**, 95% CI [-0.0096, +0.0088] — removes the 1e-6-clip artifact so any advantage is attributable.
- **Δlog-loss excluding degenerate q** on 1078 rows: **-0.0178** — the same removal applied to the log score; if this is ~0 while the all-rows Δlog-loss is large, the gap WAS the clip.

## Log-score decomposition — how much of the delta is the expiry term

`log p(outcome) = log q + log(p_W + p_L)` exactly, so the paired delta splits into:

| term | mean Δ (posterior − table) | 95% CI |
|---|---:|---|
| full  log p(outcome) | +0.2996 | [+0.2238, +0.4040] |
| conditional  log q | +0.1339 | [+0.0682, +0.2188] |
| resolve mass  log(p_W+p_L) | +0.1656 | [+0.1349, +0.1985] |

- implied resolve mass: posterior **0.9078** vs table **0.7692** — realized **1.0000** (0 expired in this window).
- ⚠️ the resolve-mass term is **+0.1656** of the **+0.2996** full delta on a **zero-expiry window** — it is not directional skill and its sign is not durable until expiries land.

## Significance — posterior vs incumbent table (aligned rows)

- common rows: **1181**
- Brier posterior **0.2300** vs table **0.2512**
- ΔBrier (posterior − table) 95% CI **[-0.0307, -0.0113]** — upper < 0 ⇒ posterior significantly better calibrated.

## Per-bucket split (bracket_bucket)

| bucket | layer | n | mean pred | realized | bias | Brier | BSS | ECE |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1-2 | PBM posterior p_W | 58 | 0.356 | 0.121 | +0.235 | 0.1598 | -0.506 | 0.235 |
| 1-2 | reachability table (incumbent) | 55 | 0.195 | 0.127 | +0.067 | 0.1816 | -0.635 | 0.242 |
| 1-2 | raw directional blend (pre-bracket) | 58 | 0.650 | 0.121 | +0.529 | 0.4150 | -2.911 | 0.533 |
| 1-2 | analytic barrier null | 58 | 0.377 | 0.121 | +0.256 | 0.1718 | -0.619 | 0.256 |
| 4+ | PBM posterior p_W | 1128 | 0.312 | 0.317 | -0.006 | 0.2330 | -0.076 | 0.138 |
| 4+ | reachability table (incumbent) | 1126 | 0.255 | 0.318 | -0.063 | 0.2546 | -0.174 | 0.204 |
| 4+ | raw directional blend (pre-bracket) | 1128 | 0.589 | 0.317 | +0.272 | 0.3472 | -0.603 | 0.295 |
| 4+ | analytic barrier null | 1128 | 0.291 | 0.317 | -0.027 | 0.2139 | +0.013 | 0.044 |
| <0.5 | PBM posterior p_W | 29 | 0.341 | 0.034 | +0.307 | 0.1291 | -2.879 | 0.307 |
| <0.5 | raw directional blend (pre-bracket) | 29 | 0.745 | 0.034 | +0.711 | 0.5842 | -16.548 | 0.711 |
| <0.5 | analytic barrier null | 29 | 0.375 | 0.034 | +0.341 | 0.1493 | -3.485 | 0.341 |

## Per-vol-tier split (vol_tier)

| vol_tier | layer | n | mean pred | realized | bias | Brier | BSS | ECE |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| high_vol | PBM posterior p_W | 396 | 0.298 | 0.333 | -0.035 | 0.2292 | -0.031 | 0.167 |
| high_vol | reachability table (incumbent) | 376 | 0.259 | 0.348 | -0.090 | 0.2539 | -0.118 | 0.218 |
| high_vol | raw directional blend (pre-bracket) | 396 | 0.629 | 0.333 | +0.296 | 0.3804 | -0.712 | 0.328 |
| high_vol | analytic barrier null | 396 | 0.271 | 0.333 | -0.062 | 0.2201 | +0.009 | 0.087 |
| low_vol | PBM posterior p_W | 191 | 0.314 | 0.277 | +0.036 | 0.2040 | -0.017 | 0.140 |
| low_vol | reachability table (incumbent) | 186 | 0.259 | 0.285 | -0.026 | 0.2230 | -0.094 | 0.138 |
| low_vol | raw directional blend (pre-bracket) | 191 | 0.576 | 0.277 | +0.299 | 0.3669 | -0.830 | 0.347 |
| low_vol | analytic barrier null | 191 | 0.326 | 0.277 | +0.048 | 0.1885 | +0.060 | 0.054 |
| med_high_vol | PBM posterior p_W | 381 | 0.323 | 0.323 | +0.000 | 0.2477 | -0.133 | 0.202 |
| med_high_vol | reachability table (incumbent) | 379 | 0.256 | 0.325 | -0.068 | 0.2632 | -0.201 | 0.217 |
| med_high_vol | raw directional blend (pre-bracket) | 381 | 0.581 | 0.323 | +0.259 | 0.3231 | -0.478 | 0.287 |
| med_high_vol | analytic barrier null | 381 | 0.291 | 0.323 | -0.032 | 0.2172 | +0.006 | 0.057 |
| med_low_vol | PBM posterior p_W | 247 | 0.327 | 0.235 | +0.092 | 0.2096 | -0.167 | 0.182 |
| med_low_vol | reachability table (incumbent) | 240 | 0.228 | 0.242 | -0.013 | 0.2497 | -0.362 | 0.241 |
| med_low_vol | raw directional blend (pre-bracket) | 247 | 0.579 | 0.235 | +0.345 | 0.3598 | -1.002 | 0.359 |
| med_low_vol | analytic barrier null | 247 | 0.325 | 0.235 | +0.090 | 0.2011 | -0.119 | 0.163 |

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
| USDJPY | 104 | 104 | 0.2789 | 0.4436 | -0.1647 | [-0.2153, -0.1086] | -0.134 | -0.116 |
| XAGUSD | 123 | 123 | 0.1879 | 0.1869 | +0.0010 | [-0.0203, +0.0193] | +0.095 | -0.236 |
| XAUAUD | 102 | 102 | 0.2634 | 0.2705 | -0.0071 | [-0.0417, +0.0286] | +0.020 | -0.204 |
| XAUEUR | 66 | 66 | 0.5209 | 0.4076 | +0.1133 | [+0.0866, +0.1430] | -0.469 | -1.182 |
| XAUUSD | 59 | 59 | 0.0981 | 0.0529 | +0.0452 | [+0.0315, +0.0573] | +0.175 | -1.032 |
*ΔBrier < 0 ⇒ the posterior is better calibrated on that symbol. `n paired` is the rows carrying BOTH layers; a CI prints only at n paired ≥ 30 — thin symbols are diagnostics, not votes.*

## Verdict (mechanically applied bar — no discretion)

### Pooled flip bar (coverage-gated) — **PASS-FLIP**

- ΔBrier -0.0211 < 0 AND CI upper bound -0.0113 < 0 (n=1181, era-clean) — RELATIVE ONLY: posterior BSS -0.0786 < 0, i.e. still worse than the constant base-rate predictor; a table that is itself below climatology is the wrong benchmark to pass.

> The posterior's price is clearly better calibrated than the table lookup on era-clean resolved evidence. Before any live wiring: re-run on a fresh window, read the absolute-skill clause above, and get operator sign-off. This harness never flips a flag.

### Per-layer scorecard (vs the incumbent table)

- **PBM posterior p_W**: **BEATS-TABLE** — Brier 0.2270 vs table 0.2512 ✓; |bias| 0.0131 vs table 0.0572 ✓; ΔBrier 95% CI [-0.0307, -0.0113].
- **reachability table (incumbent)**: **INCUMBENT** — Brier 0.2512, |bias| 0.0572.
- **raw directional blend (pre-bracket)**: **STAYS-BEHIND** — Brier 0.3561 vs table 0.2512 ✗; |bias| 0.2944 vs table 0.0572 ✗.
- **analytic barrier null**: **BEATS-TABLE** — Brier 0.2104 vs table 0.2512 ✓; |bias| 0.0044 vs table 0.0572 ✓.

## Notes / honesty

- **`nt_signal_sim.p_win_dir` is NOT the raw directional layer.** `orchestrator.py:5650` sets `_p_win_dir = _p_win_bracket`, so `p_win_dir == p_win_bracket == p_win` on every live row (verified: 1181/1181 comparable rows). The genuine directional series is `predicted_p_win_raw` (`orchestrator.py:4624`, captured before the bracket swap at `:4631`). Scoring `p_win_dir` as "directional" would silently score the table twice.
- **`nt_pbm_shadow.outcome` is NULL for every row** (migration 211: backfilled by the acceptance harness; the resolver writes the cell posterior, not this column). The join to `nt_signal_sim` is therefore the outcome source — this harness stays read-only and does not backfill.
- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar demands the CI clear zero regardless.
- **Brier/log-loss are proper scores**: they reward honesty, not just ranking. A layer can win on AUC and lose on Brier — that divergence IS the overconfidence signal this instrument exists to surface.
- Expired rows are scored (as y=0 for tp_hit) rather than dropped — the survivorship the Phase A P0 list closes. Dropping them would flatter every layer by removing the outcomes no layer predicted.

