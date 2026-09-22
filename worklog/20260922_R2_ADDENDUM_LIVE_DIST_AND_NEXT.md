# R2/R5/R7 addendum — live p_win_bracket distribution + remaining L1 items

> **Date:** 2026-09-22 · **Continues:** `20260922_R2_BUILD_P_WIN_BRACKET_EV_KELLY.md`  
> **Also:** nt_roadmap rows for `p_win_in_ev_mode` + `p_win_bracket_mode` (pending, review_at 2026-10-06)

---

## 1. Live `p_win_bracket` distribution (Studio, 284 scored rows)

| stat | value |
|---|---:|
| n with p_win_bracket | 284 |
| min / p10 / p25 | 0.002 / 0.003 / 0.004 |
| **p50** | **0.076** |
| p75 / p90 / **max** | 0.188 / 0.308 / **0.310** |

Pass rate at candidate `min_p_win_bracket`:

| thr | n pass | % |
|---:|---:|---:|
| 0.10 | 123 | 43.3% |
| 0.15 | 102 | 35.9% |
| **0.20** | **59** | **20.8%** |
| 0.25 | 39 | 13.7% |
| 0.30 | 39 | 13.7% |
| 0.35–0.45 | **0** | **0%** |

**Critical finding:** `min_p_win_bracket: 0.45` is on the **wrong scale**. `p_win_bracket = p_resolve × p_win|resolve` maxes at **0.31** live — the current gate would reject **100%** of scored rows (`replay --live` reported `n=0` accepted).

**Recommendation:** set `min_p_win_bracket: 0.20` (~21% pass) as first live candidate after a clean shadow week.

---

## 2. Flip assessment — UPDATED

| Mode | Flip? | Reason |
|---|---|---|
| `p_win_bracket_mode` | **HOLD shadow** | 0.45 rejects all; 0.20 untested in shadow |
| `p_win_in_ev_mode` | **HOLD shadow** | Same scale issue; no expectancy table yet |

**Sequence:** (1) `min_p_win_bracket: 0.20`, both modes shadow ≥7d → (2) live replay exp↓ expectancy↑/flat → (3) `p_win_in_ev_mode: live` → (4) +7d `p_win_bracket_mode: live` if bias_raw improves.

---

## 3. Built this pass

| Item | What shipped |
|---|---|
| **R5 unit-aligned baseline** | `161_aligned_baseline_and_evidence_floor.sql` → `v_paper_vs_equalwt_aligned` |
| **R7 evidence floor** | `thin_evidence` on `v_calibration_confidence_board` (n_ep < 10) |
| **nt_roadmap tracking** | `p_win_in_ev_mode` + `p_win_bracket_mode` rows (pending, review_at 2026-10-06) |
| **Replay distribution** | quantiles + pass-rate table in `replay_p_win_bracket_gate.py` |

**Still not built:** R3 cell-level enforcement · `min_p_win_bracket` 0.45→0.20 · heavy-sweep bracket · nobletradingapp aligned-baseline UI.

---

## 4. Next priority (on completion)

1. **P0** — Set `min_p_win_bracket: 0.20`, deploy, ≥7d shadow logs (unblocks every flip).
2. **P0** — Apply migration 161 on Supabase.
3. **P1** — R3 cell-level enforcement (symbol×side×vol_tier×bucket, single correction owner).
4. **P1** — nobletradingapp: `v_paper_vs_equalwt_aligned` + `thin_evidence` on L1 board.
5. **P2** — Live expectancy table → `p_win_in_ev_mode: live`.
6. **P2** — Heavy-sweep bracket parity (after light live).
