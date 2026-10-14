# Merge / Test / Migrate Worklog — PBM Build Delivery

**Date:** 2026-10-14
**Type:** Merge, test, migrate, deploy
**Base:** HEAD `4953e96` + uncommitted PBM build (Phases A–E)
**Author:** Hermes Agent (astro profile)

---

## 0. What this worklog covers

The complete delivery pipeline for the PBM (Posterior Barrier Model) build:
review authentication → merge → E2E test → migrate SQL 210/211 → commit/push →
deploy to Studio → stop/restart Studio.

All steps are executed in order, with evidence at each gate.

---

## 1. Review Authentication (COMPLETE)

Two parallel subagents verified the build against the worklog claims.

### Subagent 1 — Code claim verification (15/15 CONFIRMED)

| Claim | Status | Evidence |
|---|---|---|
| 1. barrier.py closed-form double-barrier first-passage | CONFIRMED | `barrier.py:98-123` `_image_sum()`, `:162-203` `barrier_null()` |
| 2. posterior.py CellPosterior decay-weighted Dirichlet | CONFIRMED | `posterior.py:325-395` `CellPosterior`, `:221-225` `decay_lambda()` |
| 3. posterior.py ev_moments/ev_lower_bound/conservative_kelly/bucket_tilt | CONFIRMED | `posterior.py:247-277`, `:280-301`, `:303-318`, `:750-849` |
| 4. Migration 210 PK = (symbol,side,vol_tier,bucket,deadline_class,era) | CONFIRMED | `210_nt_cell_posterior.sql:52` |
| 5. Migration 211 = nt_pbm_shadow prediction ledger | CONFIRMED | `211_nt_pbm_shadow.sql:16-44` |
| 6. eod_resolver hooks _update_cell_posterior for ALL outcomes | CONFIRMED | Two hooks after calibration gate; `posterior.py:543` accepts all 3 |
| 7. heavy_sweep.py _bracket_applied_to_ev guard (P0) | CONFIRMED | Guard before `ev_result.p_win = calib_adj["p_win"]` |
| 8. heavy_sweep.py p_expire in recompute (P0b) | CONFIRMED | `p_expire=max(0.0, min(1.0, 1.0 - float(_p_resolve)))` |
| 9. orchestrator.py PBM read block (read_cell per tick) | CONFIRMED | Lazy import, read_cell gated on pbm_mode |
| 10. orchestrator.py Phase B input at apply_bracket_to_ev | CONFIRMED | `_ev_p_win_src, _ev_p_resolve_src` swap when pbm_ev_mode=live |
| 11. tiered_atr.py Phase C posterior branch before min_signals | CONFIRMED | `if _pbm_geo == "posterior":` before grid path |
| 12. atr_reopt.py evidence trigger | CONFIRMED | `cells_for_symbol()` count delta vs threshold |
| 13. 10 pbm_* flags in _DEFAULTS + getters | CONFIRMED | 5 dedicated getters + 5 via posterior._flag() |
| 14. All pbm_* flags default to no-op | CONFIRMED | shadow/off/grid/legacy |
| 15. Resolver hook uses lazy imports | CONFIRMED | Import inside function body |

**Test files verified:** test_barrier_null.py (17 tests), test_cell_posterior.py (15 tests),
test_pbm_heavy_guard.py (7 tests) — all test what their names claim.

**Script verified:** scripts/measure_pbm_drift_auc.py (838 LOC) — py_compile passes, ruff clean.

**No bugs found.** No security issues. No missing error handling.

### Subagent 2 — Integration verification (ALL WIRED)

| Integration Point | Status |
|---|---|
| orchestrator.py PBM read block (L4261-4383) | Wired |
| orchestrator.py Phase B swap (L4631-4658) | Wired |
| orchestrator.py Phase D sizing (L4776-4804) | Wired |
| orchestrator.py Phase A stamp (L4357-4372) | Wired |
| heavy_sweep.py P0 guard (L1647-1669) | Wired |
| heavy_sweep.py P0b p_expire (L1673-1691) | Wired |
| heavy_sweep.py PBM read block (L1451-1554) | Wired |
| heavy_sweep.py Phase B swap (L1583-1598) | Wired |
| heavy_sweep.py Phase D sizing (L1697-1719) | Wired |
| eod_resolver.py lazy import (L953-960) | Correct |
| eod_resolver.py call sites (L960, L1029) | Wired |
| tiered_atr.py Phase C branch (L888-931) | Wired |
| atr_reopt.py evidence trigger (L737-773) | Wired |
| feature_flags.py 10 pbm_* getters/_DEFAULTS | All present |
| config/feature_flags.yaml 10 pbm_* entries | All match _DEFAULTS |

**No integration bugs found.**

---

## 2. Merge

The PBM build is an uncommitted working tree on top of HEAD `4953e96`. The merge
consists of:

**New files (8):**
- `regime_platform/sweep_orchestrator/barrier.py`
- `regime_platform/sweep_orchestrator/posterior.py`
- `supabase/migrations/210_nt_cell_posterior.sql`
- `supabase/migrations/211_nt_pbm_shadow.sql`
- `tests/test_barrier_null.py`
- `tests/test_cell_posterior.py`
- `tests/test_pbm_heavy_guard.py`
- `scripts/measure_pbm_drift_auc.py`

**Edited files (7 tracked, 645 insertions / 18 deletions):**
- `eod_resolver.py` — survivorship fix (all outcomes incl. expired)
- `heavy_sweep.py` — P0 guard + P0b p_expire + PBM twins
- `orchestrator.py` — PBM read/Phase B/Phase D/Phase A stamp
- `tiered_atr.py` — Phase C posterior branch
- `atr_reopt.py` — evidence trigger
- `feature_flags.py` — 10 pbm_* flags
- `config/feature_flags.yaml` — 10 pbm_* flag entries

All changes are additive or flag-gated. The only behavioural deltas vs HEAD are
the two P0 fixes (heavy-path clobber guard + p_expire parity), which the repo's
own worklogs already deemed correct for the light path.

---

## 3. E2E Test Plan

Run the full test suite plus targeted PBM tests:

```bash
# PBM-specific tests (39 tests)
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/test_barrier_null.py tests/test_cell_posterior.py tests/test_pbm_heavy_guard.py -q

# Full suite (expected: ~2055 passed / 19 pre-existing failures / 11 skipped)
PYTHONPATH="$PWD/src" ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:cacheprovider
```

Subagent verification:
- One subagent runs the PBM-specific tests
- One subagent runs the full suite and compares against the known baseline

---

## 4. Migration SQL 210/211

Before `pbm_mode: shadow` can write, the tables must exist:

```sql
-- 210: nt_cell_posterior
create table if not exists public.nt_cell_posterior (
    symbol text not null,
    side text not null check (side in ('buy','sell')),
    vol_tier text not null default 'unknown',
    distance_atr_bucket text not null,
    deadline_class text not null default 'standard',
    era text not null default 'live',
    s_w double precision not null default 0,
    s_l double precision not null default 0,
    s_e double precision not null default 0,
    n_w bigint not null default 0,
    n_l bigint not null default 0,
    n_e bigint not null default 0,
    n_eff double precision not null default 0,
    p_w double precision,
    p_l double precision,
    p_e double precision,
    p_w_null double precision,
    gap double precision,
    m_mtm double precision,
    alpha_w double precision,
    alpha_l double precision,
    alpha_e double precision,
    updated_at timestamptz,
    primary key (symbol, side, vol_tier, distance_atr_bucket, deadline_class, era)
);

-- 211: nt_pbm_shadow
create table if not exists public.nt_pbm_shadow (
    id uuid primary key default gen_random_uuid(),
    symbol text not null,
    ts timestamptz not null,
    signal text not null check (signal in ('buy','sell')),
    source text,
    tp_atr double precision,
    sl_atr double precision,
    t_bars integer,
    sigma double precision,
    p_w double precision,
    p_l double precision,
    p_e double precision,
    p_w_null double precision,
    gap double precision,
    n_eff double precision,
    ev_atr double precision,
    ev_lb_atr double precision,
    ev_lb_pass boolean,
    alpha_w double precision,
    alpha_l double precision,
    alpha_e double precision,
    bracket_bucket text,
    vol_tier text,
    deadline_class text,
    era text,
    outcome text check (outcome in ('tp_hit','sl_hit','expired')),
    created_at timestamptz not null default now()
);
```

Applied via Supabase SQL editor or `psql` connection.

---

## 5. Commit / Push

After tests pass and migrations are applied:

```bash
git add regime_platform/sweep_orchestrator/barrier.py
git add regime_platform/sweep_orchestrator/posterior.py
git add supabase/migrations/210_nt_cell_posterior.sql
git add supabase/migrations/211_nt_pbm_shadow.sql
git add tests/test_barrier_null.py tests/test_cell_posterior.py tests/test_pbm_heavy_guard.py
git add scripts/measure_pbm_drift_auc.py
git add regime_platform/sweep_orchestrator/eod_resolver.py
git add regime_platform/sweep_orchestrator/heavy_sweep.py
git add regime_platform/sweep_orchestrator/orchestrator.py
git add regime_platform/sweep_orchestrator/tiered_atr.py
git add regime_platform/sweep_orchestrator/atr_reopt.py
git add regime_platform/core/feature_flags.py
git add config/feature_flags.yaml
git add worklog/20261014_BARRIER_MODEL_INTEGRATION_MAP_AND_PHASES.md
git add worklog/20261014_PBM_COMPLETE_BUILD_PHASES_A_E.md
git add worklog/20261014_MERGE_TEST_MIGRATE_WORKLOG.md
git commit -m "feat(pbm): Posterior Barrier Model — Phases A-E

- barrier.py: closed-form double-barrier first-passage analytic null
- posterior.py: decay-weighted Dirichlet + EV moments + conservative Kelly
- migrations 210/211: nt_cell_posterior + nt_pbm_shadow
- P0 fixes: heavy-path clobber guard + p_expire parity
- survivorship closed: expired outcomes enter calibration
- Phase B/C/D: flag-gated, default no-op
- Phase E: walk-forward AUC measurement harness
- 39 new tests, all green"
git push origin main
```

---

## 6. Deploy to Studio

After push, deploy to LightningAI Studio (`noble-trader-unified`):

1. SSH to Studio
2. `git fetch && git reset --hard origin/main`
3. Verify the new files are present
4. Apply migrations 210/211 via Supabase
5. Verify Studio is running

---

## 7. Stop / Restart Studio

After deploy and migration:

1. Stop the Studio instance
2. Restart it
3. Verify the restarted instance is healthy (curl health endpoint)
4. Confirm PBM shadow stamps are flowing (check logs)

---

## 8. Operator Flip Checklist (post-deploy)

| Flip | Edit | Bar to clear first |
|---|---|---|
| B | `pbm_ev_mode: "shadow"` → observe → `"live"` | Phase A ledger log-score ≥ table lookup, era-clean |
| C | `pbm_geometry_mode: "posterior"` | replay book EV ≥ grid; band-centre 68/77 → 0 |
| D | `pbm_sizing_mode: "conservative"` | C accepted |
| E | none (harness) | promote drift only if WF AUC CI > 0.5 |
| retire | delete `calibration_enforcement.py` + gate block | D accepted |

---

## 9. Known Pre-existing Issues (NOT touched)

- `heavy_sweep.py:1171` references `trade_signal` where ruff proves it undefined (F821, at baseline)
- `orchestrator.py` double-imports `get_min_p_win_bracket` (F811, at baseline)
- 3 shipped bands measured INVALID on the platform's own solve (XAUEUR −0.235, XAUUSD −0.582, US30USD −1.592)
- Full true-range ATR re-denomination deliberately NOT done blind
- 19 pre-existing test failures (sandbox-env: API-key/db mocking), identical at baseline
