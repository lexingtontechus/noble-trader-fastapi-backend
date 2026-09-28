# P1.3 — TimesFM quantile capture (research stamps)

**Date:** 2026-09-28  
**Parent:** `20260927_P0_P4_PROGRAMME.md` §3 (P1 TimesFM) · assessment 2026-09-28  
**Status:** Capture landed (local). Migration **186**. Research-only — no gates/Kelly/EV.

---

## 1. Why

Univariate TimesFM `/forecast` already returns `quantile_forecast`
(`[series][horizon][q]`, continuous quantile head). The orchestrator used only
`point_forecast[0][0]` → `sigmoid(k·rel_change)` → `p_timesfm` and **discarded**
the band.

That band is the cheapest unused **decision-aligned** output in the stack:
uncertainty, bracket geometry, and rank features without a second model call.

## 2. Assessment recap (binds this change)

| Finding | Implication |
|---|---|
| `p_win` level is overconfident; IC≈0 as ranker | Do not add more level knobs |
| `rank_score` ranks within cell (AUC 0.67 / 0.785) | Keep R-Layer as product priority |
| `timesfm_gap` within-cell IC +0.037 (both halves) | Disagreement features can rank |
| `cell_p` best Brier (0.167 vs p_win 0.259) | Level stays `cell_p` / EV blend |
| G2.2 **NO PASS** (Δm₂₄ −0.32) | Do not promote gates on this |
| Quantiles not in DB (P1.2 note) | **This worklog closes that gap** |

Promotion bar unchanged: both-half sign on within-cell IC **or** ΔBrier ≤ −0.02
vs `p_timesfm`. Until then stamps are measurement only (same as
`kairos_rank_score`).

## 3. What we model

At horizon step 0 (next bar; same window as `p_timesfm`):

| Column | Definition |
|---|---|
| `timesfm_q_lo` | p10 price |
| `timesfm_q_mid` | p50 price |
| `timesfm_q_hi` | p90 price |
| `timesfm_q_spread` | `(q90 − q10) / last_price` — uncertainty |
| `timesfm_q_pos` | `(last − q10) / (q90 − q10)` — where spot sits in the band |

Extraction (`kairos_rank.extract_q10_q50_q90`):
- 10-vector → indices 1, 5, 9 (p10/p50/p90)
- 9-vector → indices 0, 4, 8
- shorter → min / mid / max (usable band)

`timesfm_rank_features` derives `q_spread` / `q_pos` and stamps them **even if
`p_timesfm` is missing** (point map can fail while quantiles exist).

## 4. Data flow

```text
server.py /forecast
  point_forecast  ──► p_timesfm (unchanged path)
  quantile_forecast ──► extract_q10_q50_q90 ──► _TIMESFM_Q_CACHE
                                              ──► timesfm_rank_features
                                              ──► nt_signal_sim (186)
```

## 5. Implementation

| Piece | File |
|---|---|
| `extract_q10_q50_q90` | `regime_platform/sweep_orchestrator/kairos_rank.py` |
| Parse + `_TIMESFM_Q_CACHE` + `get_timesfm_quantiles` | `orchestrator.py` `_fetch_timesfm_cached` |
| Feature stamp at capture | `orchestrator.py` `_process_symbol` |
| Payload fields | `_build_signal_sim_payload` |
| Column whitelist | `supabase_client.NT_SIGNAL_SIM_COLUMNS` |
| Schema | `supabase/migrations/186_nt_signal_sim_timesfm_quantiles.sql` |
| Tests | `tests/test_timesfm_rank_features.py` |

## 6. Explicit non-goals

- No EV weight / `p_map` change (interval-hit `p_map` is a later promote)
- No R-Layer weight change (`timesfm_gap` already optional / did not beat base)
- No TimesFM 3.0 / XReg covariates
- No live gate or Kelly mult from quantiles

## 7. Next (P1.3 eval)

After ~1 week of light-sweep rows with non-null `timesfm_q_*`:

1. IC of `q_spread`, `q_pos` vs `y_acc` (within-cell + half-split) — same harness as `scripts/kairos_p12_timesfm_ic.py`
2. Optional interval-hit `p_map` shadow vs `p_timesfm` Brier
3. Promote only on the standing bar; else keep as measurement

## 8. Decision log

| Date | Decision |
|---|---|
| 2026-09-28 | Assessment: capture quantiles as research stamps (P1.3). |
| 2026-09-28 | Implement parse → cache → `nt_signal_sim` (186). No gate/Kelly change. |
