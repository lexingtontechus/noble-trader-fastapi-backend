# PBM calibration audit — Phase B flip bar (measured or nothing)

Generated 2026-10-16 21:44 UTC · since 2026-10-14T00:00:00Z · bootstrap 200 resamples (seed 13)
Population: nt_pbm_shadow stamps joined to resolved nt_signal_sim rows on (symbol, ts, signal) — migration 211's designated acceptance replay.

## The flip bar (pre-registered)

`pbm_ev_mode` may flip `off → shadow → live` only when the **Phase A ledger log-score ≥ table lookup, era-clean** (worklog 20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md §4). Proper scoring rules measure what AUC cannot: AUC is monotone-invariant, structurally blind to overconfidence. PASS-FLIP requires mean Δ(pbm − table) full-triple ≥ 0 AND bootstrap 95% CI lower bound ≥ 0 on ≥ 500 joined-resolved and ≥ 500 paired rows; PASS-POINT = point estimate clears but the CI does not (keep collecting).

## Era discipline & coverage

- cutover marker `nt_calibration_cutover`: **2026-10-02T00:00:00Z**
- stamps fetched: **1908** (bad keys 0, dupes 0)
- stamps dropped as pre-cutover: **0**
- resolved sim rows fetched: **1451** (bad keys 0, key collisions 0)
- stamps still pending (no resolved sim row): **1906**
- joined: **2** · joined & resolved: **2**
- stamp outcome already backfilled vs NULL: mismatched **0** (stamped ≠ resolved), backfillable **2**
- recomputed null vs stamped p_w_null mismatches (>1e-4): **2**

## Full-triple log-score (all resolved stamps incl. expired — the PRIMARY)

| scorer | n | mean log-score | clipped | notes |
|---|---:|---:|---:|---|
| **pbm** posterior triple (nt_pbm_shadow stamp) | 2 | -0.4790 | 0 | log p(outcome) under the triple |
| **null** analytic barrier (recomputed from stamp geometry) | 2 | -0.3943 | 0 | log p(outcome) under the triple |
| **table** live EV input (p_win_bracket + p_resolve) | 2 | -0.3662 | 0 | log p(outcome) under the triple |

## Conditional calibration (decided rows: q = P(tp \| resolve))

Realized win rate on decided rows: **0.0000**

| scorer | n | mean q | Brier |
|---|---:|---:|---:|
| **pbm** posterior triple (nt_pbm_shadow stamp) | 2 | 0.2423 | 0.1089 |
| **null** analytic barrier (recomputed from stamp geometry) | 2 | 0.1541 | 0.0406 |
| **table** live EV input (p_win_bracket + p_resolve) | 2 | 0.2024 | 0.0819 |
| **dir** raw directional p_win (overconfidence exhibit) | 2 | 0.1635 | 0.0534 |

## Paired log-score differences (common scoreable rows)

| comparison | population | n paired | mean Δ | 95% CI |
|---|---|---:|---:|---|
| pbm − table | full triple | 2 | -0.1129 | [-0.2998, +0.0741] |
| pbm − table | conditional | 2 | +0.0400 | [+0.0184, +0.0616] |
| pbm − null | full triple | 2 | -0.0847 | [-0.2650, +0.0956] |
| table − null | full triple | 2 | +0.0282 | [-0.3391, +0.3954] |

## Reliability curves (decided rows, 0.1 bins)

### pbm

| q bin | n | mean predicted | realized frequency |
|---|---:|---:|---:|
| 0.0–0.1 | 1 | 0.018 | 0.000 |
| 0.1–0.2 | 0 | — | — |
| 0.2–0.3 | 0 | — | — |
| 0.3–0.4 | 0 | — | — |
| 0.4–0.5 | 1 | 0.466 | 0.000 |
| 0.5–0.6 | 0 | — | — |
| 0.6–0.7 | 0 | — | — |
| 0.7–0.8 | 0 | — | — |
| 0.8–0.9 | 0 | — | — |
| 0.9–1.0 | 0 | — | — |

### table

| q bin | n | mean predicted | realized frequency |
|---|---:|---:|---:|
| 0.0–0.1 | 1 | 0.000 | 0.000 |
| 0.1–0.2 | 0 | — | — |
| 0.2–0.3 | 0 | — | — |
| 0.3–0.4 | 0 | — | — |
| 0.4–0.5 | 1 | 0.405 | 0.000 |
| 0.5–0.6 | 0 | — | — |
| 0.6–0.7 | 0 | — | — |
| 0.7–0.8 | 0 | — | — |
| 0.8–0.9 | 0 | — | — |
| 0.9–1.0 | 0 | — | — |

### dir

| q bin | n | mean predicted | realized frequency |
|---|---:|---:|---:|
| 0.0–0.1 | 1 | 0.000 | 0.000 |
| 0.1–0.2 | 0 | — | — |
| 0.2–0.3 | 0 | — | — |
| 0.3–0.4 | 1 | 0.327 | 0.000 |
| 0.4–0.5 | 0 | — | — |
| 0.5–0.6 | 0 | — | — |
| 0.6–0.7 | 0 | — | — |
| 0.7–0.8 | 0 | — | — |
| 0.8–0.9 | 0 | — | — |
| 0.9–1.0 | 0 | — | — |

## Per-symbol Δ(pbm − table), full triple

| symbol | n resolved | n decided | mean Δ | 95% CI (n≥100) | realized WR | pbm mean q |
|---|---:|---:|---:|---|---:|---:|
| US500 | 1 | 1 | -0.2998 | [—, —] | 0.000 | 0.018 |
| XAGUSD | 1 | 1 | +0.0741 | [—, —] | 0.000 | 0.466 |

## Verdict (mechanically applied bar — no discretion)

- **NO-GO** — ledger depth 2 < 500 joined resolved stamps; paired depth 2 < 500 (pbm ∧ table scoreable) — keep collecting, discovery-grade only.

> Coverage gates unmet — the ledger is younger than the bar. This is the expected state for the first few days after `pbm_mode: shadow`; re-run weekly alongside the drift AUC harness.

## Notes / honesty

- Log-scores clip probabilities at 1e-6 (a wrong certainty costs −13.8, not −∞); clipped counts are reported per scorer.
- The table scorer mirrors the LIVE fail-open contract exactly: unscored `p_resolve` prices a binary triple `(p, 1−p, 0)` — the audit scores what production prices, not an idealized table.
- The bootstrap is ROW-LEVEL: overlapping 15-min same-symbol signals correlate rows, so CIs are optimistic (n inflated). The bar demands the CI lower bound clear 0 regardless — no episode dedupe, matching the platform's own measurement conventions.
- The verdict is POOLED (one number, one gate). Per-symbol rows are diagnostics; their CIs print only at n ≥ 100.
- Scoring is evaluation of FIXED stamps (nothing is fit): there is no train/test leakage to guard. The posterior was written at emit time and cannot see the outcome it is scored against.
- `dir` is scored to exhibit the raw model's calibration (the overconfidence the calibration-enforcement layer masks); it is not a Phase B candidate.

