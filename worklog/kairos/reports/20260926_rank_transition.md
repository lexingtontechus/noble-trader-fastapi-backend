# Ranking IC + regime-transition event study

Generated 2026-09-26 21:44 UTC · read-only · `kairos_rank`
Rows 77768 · decided 36357 · transitions logged 10143

## 1. Event time (T1: fresh vs deep)

| window | n decided | mean y_acc | mean y_bin | mean y_acc |
|---|---:|---:|---:|---:|
| pre | 0 | — | — | — |
| fresh | 24768 | -0.037 | 0.293 | -0.037 |
| warm | 2807 | -0.052 | 0.273 | -0.052 |
| stale | 2397 | -0.085 | 0.233 | -0.085 |
| deep | 5188 | -0.154 | 0.168 | -0.154 |

**T1 contrast (fresh − deep) y_acc = +0.117 [+0.105, +0.129]** · n_fresh=24768 n_deep=5188
→ T1 **supported** (fresh beats deep).

## 2. Transition type (H3)

| type | n decided | mean y_acc | mean y_bin |
|---|---:|---:|---:|
| both | 829 | -0.014 | 0.316 |
| dir_flip | 1571 | 0.007 | 0.339 |
| vol_contract | 1445 | -0.047 | 0.284 |
| vol_expand | 1473 | -0.052 | 0.278 |

## 3. Surprise (H4) — Spearman(surprise, y_acc) on decided with surprise

n=5318 · rank IC = 0.004

## 4. Ranking inventory vs y_acc / y_bin (within-cell)

| feature | n | Spearman(y_acc) pooled | mean within-cell IC | cells n≥30 | AUC(y_bin) pooled |
|---|---:|---:|---:|---:|---:|
| p_win | 36357 | -0.076 | -0.042 | 180 | 0.494 |
| p_win_dir | 4218 | 0.078 | -0.087 | 35 | 0.516 |
| p_win_bracket | 4172 | 0.045 | -0.071 | 35 | 0.438 |
| predicted_p_win_raw | 36357 | -0.064 | -0.033 | 180 | 0.499 |
| p_resolve | 4172 | 0.12 | -0.055 | 35 | 0.517 |
| regime_conf | 36357 | -0.059 | 0.019 | 180 | 0.447 |
| raw_p_regime | 24047 | -0.048 | -0.020 | 174 | 0.461 |
| efficiency_24h | 295 | -0.445 | -0.835 | 1 | 0.342 |
| clv_1h | 295 | 0.063 | -0.459 | 1 | 0.593 |
| rel_volume_1h | 295 | -0.512 | -0.855 | 1 | 0.157 |
| atr_ratio_run | 295 | 0.106 | -0.589 | 1 | 0.483 |
| geometry_risk_hat | 3390 | -0.136 | 0.033 | 23 | 0.477 |

Pooled AUC mixes symbols; treat as descriptive only (triage: withdrawn as mix artefact). Within-cell IC is the ranking bar.

## 5. Cell-shrink baseline vs p_win (walk-forward-ish)

Scored decisions (prior n_cell≥5): **35243**
- cell-shrink Brier 0.214 · p_win Brier 0.214 · Δ(p_win − shrink) +0.000
- hit-rate@0.5: shrink 0.751 · p_win 0.547

## 6. Era mix (triage D4)

| era | decided |
|---|---:|
| pre_debounce | 34668 |
| debounce_to_atr | 1546 |
| atr_to_resolver | 123 |
| post_resolver | 20 |

Event-time and ranking results are **mixed-era** until post-resolver volume grows; treat as discovery (R1), not promote evidence.

