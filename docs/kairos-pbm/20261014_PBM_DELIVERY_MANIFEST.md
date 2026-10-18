# PBM Delivery Manifest

**Package:** `kairos-pbm-build-20261014.zip`
**Date:** 2026-10-14
**Repo:** `lexingtontechus/noble-trader-fastapi-backend` — HEAD `4953e96` (13 commits past the review baseline `aa9fff1`)
**State:** the complete Posterior Barrier Model build (Phases A–E) as an **uncommitted working tree** on top of HEAD — review the diff, then commit when satisfied.

---

## 1. What this package is

The full implementation of the PBM redesign proposed in `review-worklogs/20261013_POSTERIOR_PRICING_METHODOLOGY_PROPOSAL.md` and mapped in `review-worklogs/20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md`:

> one probability, one price, one gate — closed-form double-barrier null (μ = 0, σ = ATR), decay-weighted Dirichlet posterior over `(symbol, side, vol_tier, bucket, deadline_class, era)` fed by **all outcomes including expired**, 3-outcome EV, and a single qualification inequality: **the 5% credible lower bound of posterior EV > 0**.

**Nothing live has changed except two P0 bug fixes.** `pbm_mode: shadow` stamps measurements; Phases B/C/D ship as complete, tested, flag-gated code paths at their no-op defaults; Phase E ships as the measurement harness that decides.

## 2. Contents

| Path | What |
|---|---|
| `noble-trader-fastapi-backend/` | the full repo working tree (incl. `.git`, shallow @ `4953e96`) with the build |
| `noble-trader-fastapi-backend/worklog/20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md` | the build log — files, verification, honest boundaries, flip checklist |
| `noble-trader-fastapi-backend/worklog/20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md` | the integration map — 5 attachment points, phases, deletion ledger (~1,700 LOC out / ~350 in) |
| `review-worklogs/` | the 6 review/design docs: triage, geometry mechanism review, PBM proposal, integration map, build log, this manifest |
| `shared-worklog.md` | the full session worklog (review → proposal → map → build → delivery) |
| `pbm_build.patch` | `git diff` of the 7 edited tracked files (645 insertions / 18 deletions); the 8 new files are untracked — full content in the tree |

## 3. What was built (by phase)

| Phase | Landing | Default flag state |
|---|---|---|
| **A — measurement** | `barrier.py` (378 LOC — closed-form double-barrier first-passage null, method-of-images), `posterior.py` (979 LOC — decay-weighted Dirichlet anchored on the null, exact EV moments + lower bound, conservative Kelly, bucket tilt), migrations `210_nt_cell_posterior` + `211_nt_pbm_shadow`, resolver hook for **all three outcome classes incl. expired** (survivorship closed, side finally consumed — migration 159 gets its consumer), per-signal shadow stamps (light + heavy), weekly `rebuild_posteriors`, **P0: heavy-path clobber guard + P0b: p_expire parity** | `pbm_mode: "shadow"` — measurement on, zero publish change |
| **B — EV input swap** | the posterior triple through the ONE `apply_bracket_to_ev` call site, both paths | `pbm_ev_mode: "off"` |
| **C — continuous geometry** | posterior-EV argmax over the band domain (grid / band-centre / gates bypassed in-mode) + evidence-triggered reopt | `pbm_geometry_mode: "grid"` |
| **D — sizing** | conservative Kelly on the EV lower bound | `pbm_sizing_mode: "legacy"` |
| **E — drift harness** | `scripts/measure_pbm_drift_auc.py` — walk-forward barrier-orientation AUC with mechanical PROMOTE / STAY-OFF verdicts | run-on-demand |

## 4. Verify (as delivered)

```bash
cd noble-trader-fastapi-backend
python -m pytest tests/test_barrier_null.py tests/test_cell_posterior.py tests/test_pbm_heavy_guard.py -q
# → 39 passed  (re-confirmed at packaging time)
git diff --stat          # 645 insertions / 18 deletions across 7 files
git status --short       # 8 new paths: 2 modules, 2 migrations, 3 test files, 1 script (+ 2 worklogs)
```

Full-suite context (build session): **2055 passed / 19 failed / 11 skipped, zero regressions** — the 19 failures are pre-existing sandbox-env issues (API-key/db mocking), identical at the stashed baseline; the 4-test delta vs baseline was the new guard file failing correctly against the pre-fix code.

## 5. Before `pbm_mode: shadow` can write

Apply migrations **210/211** (Supabase). Until the tables exist, PostgREST 404s are swallowed best-effort — the stamps simply don't land.

## 6. Flip checklist (operator)

| Flip | Edit | Bar to clear first |
|---|---|---|
| B | `pbm_ev_mode: "shadow"` → observe logs → `"live"` | Phase A ledger log-score ≥ table lookup, era-clean |
| C | `pbm_geometry_mode: "posterior"` | replay book EV ≥ grid; band-centre 68/77 → 0 |
| D | `pbm_sizing_mode: "conservative"` | C accepted (size honestly only once price is honest) |
| E | none (harness) | promote drift only if WF AUC bootstrap CI > 0.5 — expected honest outcome: stays off |
| retire | delete `calibration_enforcement.py` + gate block | D accepted |

## 7. Rollback

`pbm_mode: "off"` stops stamps/writes. `pbm_ev_mode` / `pbm_geometry_mode` / `pbm_sizing_mode` back to defaults restores HEAD behaviour exactly. The only behavioural deltas vs HEAD are the two P0 fixes — which the repo's own worklogs already deemed correct for the light path; the heavy twin now matches.

## 8. Known pre-existing issues (flagged, NOT touched)

- `heavy_sweep.py:1171` references `trade_signal` where ruff proves it undefined (F821, at baseline, inside a try-block observation path)
- `orchestrator.py` double-imports `get_min_p_win_bracket` (F811, at baseline)
- 3 shipped bands measured INVALID on the platform's own solve (XAUEUR −0.235, XAUUSD −0.582, US30USD −1.592 band EV) — Phase C's business
- Full true-range ATR re-denomination of the sweep deliberately NOT done blind — the PBM layer consumes `pbm_sigma_atr` (default 1.0 ≡ σ in ATR units) and the walk-agreement test keeps the effective-σ calibration measurable
