# PBM complete build — Phases A-E landed at HEAD (shadow-defaulted)

**Date:** 2026-10-14
**Type:** Implementation (the full five-phase build of the Posterior Barrier Model).
**Implements:** `20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md` (the integration map + migration plan written earlier the same day; itself grounded in `20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md`).
**Base:** HEAD `4953e96` (working tree; uncommitted).
**Contract:** one probability, one price, one gate — now in code.

---

## 0. What shipped

Every phase of the map's §5, as code, **with all live-behaviour flips at their
no-op defaults**. Phase A is live (stamps + posterior writes + the P0 fixes);
Phases B/C/D ship as complete, tested, flag-gated code paths that stay off
until their era-clean replay acceptance bars pass; Phase E ships as the
measurement harness that decides. This is the repo's own shadow-first
discipline applied to its own redesign — nothing goes live on a calendar.

| Phase | What landed | Default flag state |
|---|---|---|
| **A** | `barrier.py` + `posterior.py` + migrations 210/211 + resolver hook (ALL outcomes incl. expired) + per-signal prediction-ledger stamps (light+heavy) + weekly rebuild wiring + **P0 heavy-clobber guard + P0b p_expire fix** | `pbm_mode: "shadow"` — measurement on, zero publish change |
| **B** | EV input swap to the posterior triple through the ONE substitution function, both paths | `pbm_ev_mode: "off"` |
| **C** | Continuous in-band argmax of the posterior EV (grid/band-centre/gates bypassed in-mode) + evidence-triggered reopt | `pbm_geometry_mode: "grid"` |
| **D** | Conservative Kelly on the EV lower bound | `pbm_sizing_mode: "legacy"` |
| **E** | `scripts/measure_pbm_drift_auc.py` — walk-forward barrier-orientation AUC harness, promotion bar encoded mechanically | run-on-demand; expected outcome: everything stays off |

## 1. Files

**New:**
- `regime_platform/sweep_orchestrator/barrier.py` (~380 LOC) — the analytic null: closed-form double-barrier first-passage (method-of-images series, alternating families re-indexed into one), price-space wrapper, EV + lower-bound quantile, and the continuous solver `solve_geometry_pbm`. Pure functions, numpy/math only. Distinct from `first_passage.py` by design (that module is an empirical offline overlay — "LIVE WIRING: none" in its own header — and cannot be the live prior by construction).
- `regime_platform/sweep_orchestrator/posterior.py` (~970 LOC) — Layer 2+3: `CellPosterior` (decay-weighted Dirichlet anchored on the null, covariate-aware re-anchoring), `ev_moments`/`ev_lower_bound` (exact Dirichlet moments of the linear EV — no simulation), `conservative_kelly`, `bucket_tilt` (logit win-tilt + resolve-rate tilt, k0-shrunk, `is None` checks so a MEASURED 0.0 dead bucket is never flattened to "no evidence"), `read_cell`/`update_cell`/`record_pbm_shadow`/`cells_for_symbol`/`rebuild_posteriors` (fetch-then-upsert, best-effort, never raises into the tick or resolver).
- `supabase/migrations/210_nt_cell_posterior.sql` — the one table: PK `(symbol, side, vol_tier, distance_atr_bucket, deadline_class, era)`; sufficient statistics (`s_w/s_l/s_e` decayed, `n_*` raw), derived means/gap/`m_mtm`, stored alphas. **Finally consumes migration 159's side columns.**
- `supabase/migrations/211_nt_pbm_shadow.sql` — the Phase A prediction ledger, one row per emitted signal (posterior triple, null, gap, EV + bound + pass flag).
- `tests/test_barrier_null.py` (17), `tests/test_cell_posterior.py` (15), `tests/test_pbm_heavy_guard.py` (7).
- `scripts/measure_pbm_drift_auc.py` (Phase E harness, built+verified by subagent).

**Edited (645 insertions / 18 deletions):**
- `eod_resolver.py` — the survivorship fix: `_update_cell_posterior` called from BOTH resolve funnels for ALL THREE outcome classes (expired marked-to-market at its exit price). Era-clean by construction (pre-cutover rows skipped inside `update_cell`). Lazy import — the module-level edge created the cycle `tiered_atr → geometry_risk → heavy_sweep → eod_resolver → posterior → barrier → tiered_atr` (pinned by an import-order test).
- `heavy_sweep.py` — **P0**: the enforcement block now branches on `_bracket_applied_to_ev` before any p_win mutation (the light path's bracket-EV-terminal guard, applied to the heavy twin); **P0b**: the directional branch's recompute passes `p_expire` (the old call silently converted the heavy EV to a binary price). Plus the heavy twins of the PBM read/Phase-B swap/Phase-D sizing/stamp.
- `orchestrator.py` — the PBM read block (one `read_cell` GET per tick, same cost class as `lookup_bracket_probs`); Phase B input selection at the single `apply_bracket_to_ev` call site; Phase D conservative sizing after enforcement; Phase A stamp fire-and-forget at the persist site; weekly `rebuild_posteriors` riding `_run_bracket_reachability`.
- `tiered_atr.py` — Phase C branch in `dynamic_atr_pick` BEFORE the min_signals floor (no sample floor is the point): posterior tilt + continuous solve, store write, cache; grid path untouched below it.
- `atr_reopt.py` — evidence trigger in `check_atr_reopt_urgency` (posterior outcome-count deltas vs stored state, `pbm_reopt_min_new_outcomes`), counts persisted into the urgency state.
- `feature_flags.py` + `feature_flags.yaml` — 10 flags with full comment blocks (the flip conditions are written next to the flags).

## 2. Verification

**New tests: 39/39 green.**
- `test_barrier_null.py` — gambler's ruin (both series and fallback branches), symmetry, exact normalization, scale invariance (`a/(σ√T)`), deadline monotonicity with saturation, **sub-stepped continuous-monitoring MC agreement** (the bar-end variant carries the classic far-barrier discretization bias — +0.022 on π_W for (4.5,2,48) — documented in the test), **walk agreement with `walk_envelopes` on true OHLC from sub-stepped BM** (the standing acceptance test), EV convention parity with the picker's `net = pnl − cost_atr` rows, the XAUAUD-class arithmetic block, price-wrapper direction/log-distance exactness, solver-vs-fine-grid agreement, min_rr domain honesty, tilt-response.
- `test_cell_posterior.py` — conjugacy/normalization, **cold cell prices exactly at the null** (the no-cliff property), monotone evidence, gap signs, decay half-life, **bound tightens with evidence while never exceeding its own mean** (the property that retires `min_signals`/`min_fold_n`), EV moments vs direct expectation, conservative Kelly monotonicity/caps, deadline classes, bucket tilt (cold = null; **dead bucket bites** — regression-pins the `or`-vs-`is None` distinction; normalization; per-bucket selectivity; legacy-era exclusion), and **the platform's measured adverse timing fails the one gate** under honest evidence.
- `test_pbm_heavy_guard.py` — structural pins (guard precedes mutation; terminal branch never touches p_win; `p_expire` in the heavy recompute; both funnels hook all outcomes; both paths share one substitution contract) + behavioural pins (`apply_bracket_to_ev` terminal 3-outcome semantics, fail-open; expire never charged as loss) + **import-order cycle pin in all three orders**. These four structural pins were observed failing against the stashed (pre-fix) baseline — they are real regression tests, not tautologies.

**Full suite: 2055 passed / 19 failed / 11 skipped — ZERO regressions** (baseline at stashed HEAD: 23 failed; the 4-test delta is my guard file failing correctly against unfixed code; the 19 remaining failures are pre-existing sandbox-env issues — API-key/db mocking, `calibration_bias_window` etc. — identical at baseline).

**Ruff:** all new files clean (0 errors, E/F/I/W, line-length 120). The 6 edited legacy files carry 2 net I001-class notes in my inserted blocks, consistent with those files' pre-existing in-function import style (46 such notes at baseline).

**Known pre-existing bugs found but NOT touched** (out of scope, flagged for the operator): `heavy_sweep.py:1171` references `trade_signal` where ruff proves it undefined (F821, present at baseline, inside a try-block observation path); `orchestrator.py` double-imports `get_min_p_win_bracket` (F811, pre-existing).

## 3. The honest boundaries (unchanged, now enforced by code)

- **Nothing is live that wasn't live before.** `pbm_mode: shadow` writes stamps and cells; publish behaviour is byte-identical to HEAD except the two P0 bug fixes (which the repo's own worklogs already deemed correct for the light path — the heavy twin now matches).
- **The flips are earned, not scheduled**: B after posterior-vs-table reliability on the resolved book; C after replay book-EV ≥ grid-book and band-centre rate → 0; D after C; E promotes only above null with CI lower bound > 0.5 — expected outcome: μ stays pinned at 0 (the stable resting state).
- **Migration 210/211 must be applied** before `pbm_mode: shadow` can write (PostgREST 404s are swallowed best-effort today; the stamps simply won't land until the tables exist).
- **TR-ATR scoped honestly:** the full true-range ATR migration of the sweep's ATR stamping would re-denominate every band/bucket overnight and is NOT done blind in this build; the PBM layer consumes `pbm_sigma_atr` (default 1.0 ≡ the proposal's `σ = ATR_true_range/price` in ATR units) and the walk-agreement test keeps the effective-σ calibration measurable.

## 4. Flip checklist (operator)

| Flip | Edit | Bar to clear first |
|---|---|---|
| B | `pbm_ev_mode: "shadow"` → observe logs → `"live"` | Phase A ledger log-score ≥ table lookup, era-clean |
| C | `pbm_geometry_mode: "posterior"` | replay book EV ≥ grid; band-centre 68/77 → 0 |
| D | `pbm_sizing_mode: "conservative"` | C accepted (size honest only once price is) |
| E | none (harness) | promote drift only if WF AUC CI > 0.5 — else stays off |
| retire | delete `calibration_enforcement.py` + gate block | D accepted |
