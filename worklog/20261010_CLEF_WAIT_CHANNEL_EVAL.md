# Clef-Flash decision layer — direction is chance, `p_clef_wait` is a real suppress signal

**Date:** 2026-10-10
**Scope:** Roadmap #89 `kairos_clef_flash_decision_layer` D1 evaluation (bar: ≥200 decided rows). The direction channel was previously found at chance (AUC 0.506); this evaluates the **wait** channel (`p_clef_wait`) on the now-decision-grade population (1,458 episode-deduped decided rows) and tests whether it earns a role.
**Status:** Direction channel = retire. Wait channel = **repurpose as a suppress gate (shadow-first)**.

---

## 1. Direction channel — confirmed dead

`p_clef_dir` vs `tp_hit`, episode-deduped (n=1,458):

| p_clef_dir bucket | n | WR |
|---|---|---|
| ~0.14 | 495 | 10.1% |
| ~0.28 | 460 | 12.8% |
| ~0.51 | 300 | 15.7% |
| ~0.69 | 183 | 16.9% |
| ~0.82 | 20 | 15.0% |

AUC(p_clef_dir → tp_hit) = **0.582**. Monotonic-ish but the top buckets are thin (n=20) and the lift from bottom to top is only ~6pp. Consistent with the prior 0.506 read: **not usable as a direction gate.** Do not build it.

## 2. Wait channel — real and monotonic

`p_clef_wait` vs outcome, episode-deduped (n=1,458):

| p_clef_wait bucket | n | WR_tp | SL_rate |
|---|---|---|---|
| ~0.16 | 44 | 18.6% | 81.4% |
| ~0.32 | 236 | 18.2% | 81.8% |
| ~0.50 | 307 | 14.0% | 86.0% |
| ~0.70 | 683 | 12.3% | 87.7% |
| ~0.83 | 188 | **6.4%** | **93.6%** |

- **AUC(p_clef_wait → sl_hit) = 0.608** — the mirror of the direction channel's 0.58, but it *predicts losses* rather than wins, which is the more useful direction for a suppress gate.
- **Clean monotonic**: win rate falls 18.2% → 6.4% as wait rises; SL rate rises 81.8% → 93.6%. The extreme bucket (wait≈0.83) has a **93.6% stop rate** on n=188 — the model is confidently saying "this is not a good entry," and it's right.

## 3. Suppress-gate impact (backtest on the same population)

Threshold: suppress `p_clef_wait ≥ 0.70`:

| metric | all | kept (<0.70) |
|---|---|---|
| n closed | 1,458 | 906 |
| WR | 13.0% | **15.2%** |
| expectancy_R | −0.586 | **−0.538** |
| % book cut | — | **37.9%** |

Cutting the top ~38% of wait scores lifts WR +2.2pp and expectancy +0.05R. Real, directionally correct improvement — **but the surviving book is still −0.54R.** A cleaner bucket does not rescue an unprofitable base. This is the same wall every gate candidate hits this week (bracket, reachability, regime_edge, context): no per-bucket filter currently separates winners at scale because the *book itself* is negative-expectancy. The calibration/era-clean work is the load-bearing fix; gates only trim.

## 4. Recommendation

1. **Retire the direction channel** (`p_clef_dir`) — AUC 0.58, confirmed chance. Do not promote.
2. **Repurpose `p_clef_wait` as a shadow suppress gate**: stamp `clef_wait_suppress_shadow` when `p_clef_wait ≥ 0.70` and other gates would pass; log the would-suppress cohort. Promote to a live suppress only if the shadow cohort (a) holds its WR gap over a fresh window and (b) the base book expectancy turns non-negative — otherwise suppressing a third of an already-bad book just makes a smaller bad book.
3. **Sequence it after, not before, the calibration fix.** Gates trim; calibration moves the mean. Run the era-clean reopt / calibration work first, then revisit whether the wait gate adds anything on top of a healthier book.

## 5. Data lineage

- Population: `nt_signal_sim`, `source='sweep-light'`, `outcome in ('tp_hit','sl_hit')`, `p_clef_wait is not null`, episode-deduped via `distinct on (symbol, signal, date_trunc('hour', ts))`.
- n = 1,458 decided episodes (clears the ≥200 D1 bar by 7×).
- Roadmap `kairos_clef_flash_decision_layer` updated with this finding; review 2026-10-17. `clef_macro_coherence` (#92) superseded by the wait-channel decision.
