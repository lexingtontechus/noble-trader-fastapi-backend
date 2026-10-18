# PBM calibration audit — Phase B flip bar (measured or nothing)

Generated 2026-10-16 21:45 UTC · since 2026-10-14T00:00:00Z · bootstrap 200 resamples (seed 13)
Population: nt_pbm_shadow stamps joined to resolved nt_signal_sim rows on (symbol, ts, signal) — migration 211's designated acceptance replay.

## The flip bar (pre-registered)

`pbm_ev_mode` may flip `off → shadow → live` only when the **Phase A ledger log-score ≥ table lookup, era-clean** (worklog 20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md §4). Proper scoring rules measure what AUC cannot: AUC is monotone-invariant, structurally blind to overconfidence. PASS-FLIP requires mean Δ(pbm − table) full-triple ≥ 0 AND bootstrap 95% CI lower bound ≥ 0 on ≥ 500 joined-resolved and ≥ 500 paired rows; PASS-POINT = point estimate clears but the CI does not (keep collecting).

## Era discipline & coverage

- cutover marker `nt_calibration_cutover`: **2026-10-02T00:00:00Z**
- stamps fetched: **1908** (bad keys 0, dupes 1880)
- stamps dropped as pre-cutover: **0**
- resolved sim rows fetched: **1451** (bad keys 0, key collisions 0)
- stamps still pending (no resolved sim row): **693**
- joined: **1215** · joined & resolved: **1215**
- stamp outcome already backfilled vs NULL: mismatched **0** (stamped ≠ resolved), backfillable **1215**
- recomputed null vs stamped p_w_null mismatches (>1e-4): **1215**

## Full-triple log-score (all resolved stamps incl. expired — the PRIMARY)

| scorer | n | mean log-score | clipped | notes |
|---|---:|---:|---:|---|
| **pbm** posterior triple (nt_pbm_shadow stamp) | 1215 | -0.7499 | 0 | log p(outcome) under the triple |
| **null** analytic barrier (recomputed from stamp geometry) | 1215 | -0.7223 | 0 | log p(outcome) under the triple |
| **table** live EV input (p_win_bracket + p_resolve) | 1181 | -1.0584 | 15 | log p(outcome) under the triple |

## Conditional calibration (decided rows: q = P(tp \| resolve))

Realized win rate on decided rows: **0.3012**

| scorer | n | mean q | Brier |
|---|---:|---:|---:|
| **pbm** posterior triple (nt_pbm_shadow stamp) | 1215 | 0.3351 | 0.2298 |
| **null** analytic barrier (recomputed from stamp geometry) | 1215 | 0.3207 | 0.2052 |
| **table** live EV input (p_win_bracket + p_resolve) | 1181 | 0.2998 | 0.2368 |
| **dir** raw directional p_win (overconfidence exhibit) | 1181 | 0.2518 | 0.2512 |

## Paired log-score differences (common scoreable rows)

| comparison | population | n paired | mean Δ | 95% CI |
|---|---|---:|---:|---|
| pbm − table | full triple | 1181 | +0.2996 | [+0.2197, +0.3855] |
| pbm − table | conditional | 1181 | +0.0356 | [+0.0245, +0.0457] |
| pbm − null | full triple | 1181 | -0.0296 | [-0.0380, -0.0205] |
| table − null | full triple | 1181 | -0.3291 | [-0.4076, -0.2503] |

## Reliability curves (decided rows, 0.1 bins)

### pbm

| q bin | n | mean predicted | realized frequency |
|---|---:|---:|---:|
| 0.0–0.1 | 68 | 0.047 | 0.015 |
| 0.1–0.2 | 135 | 0.146 | 0.444 |
| 0.2–0.3 | 169 | 0.260 | 0.243 |
| 0.3–0.4 | 492 | 0.361 | 0.398 |
| 0.4–0.5 | 252 | 0.439 | 0.226 |
| 0.5–0.6 | 91 | 0.519 | 0.121 |
| 0.6–0.7 | 8 | 0.608 | 0.000 |
| 0.7–0.8 | 0 | — | — |
| 0.8–0.9 | 0 | — | — |
| 0.9–1.0 | 0 | — | — |

### table

| q bin | n | mean predicted | realized frequency |
|---|---:|---:|---:|
| 0.0–0.1 | 205 | 0.024 | 0.244 |
| 0.1–0.2 | 204 | 0.162 | 0.245 |
| 0.2–0.3 | 189 | 0.258 | 0.376 |
| 0.3–0.4 | 204 | 0.347 | 0.348 |
| 0.4–0.5 | 135 | 0.443 | 0.259 |
| 0.5–0.6 | 194 | 0.537 | 0.418 |
| 0.6–0.7 | 46 | 0.636 | 0.130 |
| 0.7–0.8 | 2 | 0.700 | 0.000 |
| 0.8–0.9 | 0 | — | — |
| 0.9–1.0 | 2 | 1.000 | 0.500 |

### dir

| q bin | n | mean predicted | realized frequency |
|---|---:|---:|---:|
| 0.0–0.1 | 205 | 0.020 | 0.244 |
| 0.1–0.2 | 338 | 0.142 | 0.317 |
| 0.2–0.3 | 250 | 0.264 | 0.464 |
| 0.3–0.4 | 119 | 0.347 | 0.143 |
| 0.4–0.5 | 106 | 0.432 | 0.302 |
| 0.5–0.6 | 113 | 0.527 | 0.319 |
| 0.6–0.7 | 46 | 0.636 | 0.130 |
| 0.7–0.8 | 2 | 0.700 | 0.000 |
| 0.8–0.9 | 0 | — | — |
| 0.9–1.0 | 2 | 1.000 | 0.500 |

## Per-symbol Δ(pbm − table), full triple

| symbol | n resolved | n decided | mean Δ | 95% CI (n≥100) | realized WR | pbm mean q |
|---|---:|---:|---:|---|---:|---:|
| AAPL | 53 | 53 | -0.3282 | [—, —] | 0.019 | 0.335 |
| AUDUSD | 103 | 103 | +0.1429 | [+0.0757, +0.2066] | 0.485 | 0.411 |
| BTCUSD | 121 | 121 | -0.1166 | [-0.1623, -0.0648] | 0.190 | 0.160 |
| EURUSD | 85 | 85 | +0.1744 | [—, —] | 0.106 | 0.385 |
| GBPUSD | 87 | 87 | +0.4366 | [—, —] | 0.437 | 0.411 |
| US30USD | 59 | 59 | +0.4307 | [—, —] | 0.136 | 0.351 |
| US500 | 42 | 42 | -0.1335 | [—, —] | 0.024 | 0.028 |
| USDCAD | 108 | 108 | +0.6184 | [+0.5689, +0.6710] | 0.389 | 0.383 |
| USDCHF | 103 | 103 | -0.0201 | [-0.0613, +0.0251] | 0.408 | 0.427 |
| USDJPY | 104 | 104 | +2.5997 | [+1.8807, +3.2700] | 0.510 | 0.380 |
| XAGUSD | 123 | 123 | +0.1243 | [+0.0835, +0.1560] | 0.187 | 0.296 |
| XAUAUD | 102 | 102 | +0.0554 | [-0.1601, +0.4052] | 0.324 | 0.395 |
| XAUEUR | 66 | 66 | -0.6124 | [—, —] | 0.606 | 0.205 |
| XAUUSD | 59 | 59 | -0.4435 | [—, —] | 0.051 | 0.379 |

## Verdict (mechanically applied bar — no discretion)

- **PASS-FLIP** — mean Δ +0.2996 ≥ 0 AND CI lower bound +0.2197 ≥ 0 (n=1181, era-clean).

> The posterior's price is clearly ≥ the table lookup on era-clean resolved evidence. Next: flip `pbm_ev_mode: "off" → "shadow"`, observe the substitution logs, and re-run this audit on a fresh window before considering `"live"`. This harness never flips a flag.

## Notes / honesty

- Log-scores clip probabilities at 1e-6 (a wrong certainty costs −13.8, not −∞); clipped counts are reported per scorer.
- The table scorer mirrors the LIVE fail-open contract exactly: unscored `p_resolve` prices a binary triple `(p, 1−p, 0)` — the audit scores what production prices, not an idealized table.
- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar demands the CI lower bound clear 0 regardless — no episode dedupe, matching the platform's own measurement conventions.
- The verdict is POOLED (one number, one gate). Per-symbol rows are diagnostics; their CIs print only at n ≥ 100.
- Scoring is evaluation of FIXED stamps (nothing is fit): there is no train/test leakage to guard. The posterior was written at emit time and cannot see the outcome it is scored against.
- `dir` is scored to exhibit the raw model's calibration (the overconfidence the calibration-enforcement layer masks); it is not a Phase B candidate.

