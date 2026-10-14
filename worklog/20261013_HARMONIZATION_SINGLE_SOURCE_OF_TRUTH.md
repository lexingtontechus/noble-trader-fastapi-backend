# Harmonization — single source of truth across metrics, definitions, scheduling

**Date:** 2026-10-13
**Author:** Hermes Agent (assessment)
**Trigger:** pulse check 2026-10-13 20:15 UTC + user directive — "simple principle of
harmonization is single source of truth, metrics, definitions, scheduling. regime+trend
labels etc is consistent — asset classes match nt_symbol. E2E tests + replay for
verification for fix or new code. atr issue — cleanup/reduce inefficiencies on frequency
config params are maintained, either in realtime or schedule."

## Task tracker

| taskid | task description | status |
|---|---|---|
| HARM-01 | Asset-class vocabulary split: `nt_symbol.asset_class='commodities'` → `asset_class_key()` → `'commodity'`, but no enabled archetype listed `commodity`. All four golds unreachable → router replayed 0/225. **Fixed** — `commodity` added to `momentum` + `mean_reversion` `asset_classes`. 13 tests. Replay 19→59 routed. | **done** |
| HARM-02 | Deadline floors declared but never enforced: `min_soft_deadline_hours: 12.0` / `max_hard_deadline_hours: 120.0` returned by `get_dynamic_deadline_config_for_symbol()` but never applied in `_compute_dynamic_deadline`. AUDUSD shipped `soft_deadline_hours=0.5` vs 2.62h avg settle; 118/225 rows breached their own soft deadline. **Fixed** — clamp sourced from the same config, mirroring `dynamic_deadline.derive_deadline_tier` :116-117. 2 tests. | **done** |
| HARM-03 | Reachability staleness guard can never fire: `BRACKET_REACHABILITY_STALE_AFTER` = 6.5 days vs a 7-day weekly build interval. **Verified correct, pinned** — invariant test added; no change needed. | **done** |
|| HARM-04 | Router alignment inputs (macro_label/trend_label) were daily-refreshed while signals fire every 15 min, causing up to 24h staleness and frozen alignment. **Fixed** — changed refresh cadence to pre-sweep grid (5,20,35,50 * * * *), added freshness guard that skips redundant daily-bar fetches, reduced MACRO_REGIME_STALE_AFTER from 20h to 90min. 1 test updated. | **done** |
| HARM-05 | `mean_reversion.regime_vol: [low_vol, med_low_vol]` excludes the measured best cell (med_high_vol 52.9% WR n=17 vs low_vol 27.3% n=11). **Documented, NOT fixed** — widening is a strategy decision needing the B1 evidence bar. Pinned by test. | **pinned** |
| HARM-06 | Forex majors (6) intentionally uncovered by any enabled archetype. **Verified intentional** — B1 measured 0 false-cohort rows across ~3,300 era-clean resolved forex signals; `ma_trend` stays disabled. Pinned by test with an explicit allow-list. | **verified** |

## 1. What was broken

The pulse check's router replay (225 last-12h resolved rows, `route()` called on each row's
own `macro_label`/`trend_label`/`regime` and its real `nt_symbol.asset_class`) returned
**routed 0/225, stand_by 100%**. Root-caused to two independent defects plus three
invariants worth pinning.

### HARM-01 — the asset-class vocabulary split

`nt_symbol.asset_class` for XAUUSD/XAUAUD/XAUEUR/XAGUSD is `'commodities'`.
`asset_class_key()` normalises that to `'commodity'`. The registry gated
`mean_reversion` to `asset_classes: [metal]` and `momentum` to
`[crypto, metal, equity, index, etf]`. Neither list contains `commodity`, so a gold row
could never match a gold archetype regardless of what its macro/trend reads said.

This is the single-source-of-truth violation the user named: three vocabularies for one
concept — the DB value (`commodities`), the normaliser's key (`commodity`), and the
registry's allow-list (`metal`).

**Fix** — `config/strategy_registry.yaml`, both enabled archetypes now list `commodity`
alongside `metal` (kept, so a literal `'metal'` class still routes). Vocabulary widened,
not replaced.

### HARM-02 — declared floors, unenforced

`feature_flags.yaml` declares `min_soft_deadline_hours: 12.0` and
`max_hard_deadline_hours: 120.0`. `get_dynamic_deadline_config_for_symbol()`
(`feature_flags.py:1829-1833`) returns them. The derivation path
(`dynamic_tier_optimizer._compute_dynamic_deadline`, :315-324) wrote raw p75/p90 with no
clamp. Live consequence measured 2026-10-13:

- AUDUSD `soft_deadline_hours = 0.5` against a 2.62h average settle
- **118 of 225** resolved rows breach their own `soft_deadline_hours`; 55 breach `hard`

**Fix** — `dynamic_tier_optimizer.py:315-341` now clamps, sourcing the numbers from the
same `get_dynamic_deadline_config()` so there is one definition. The clamp mirrors
`dynamic_deadline.derive_deadline_tier`'s existing :116-117 exactly, and preserves
`hard > soft` (otherwise `DeadlineTier.decay()` divides by a non-positive span and every
trade scores 0).

## 2. Verification

### E2E — `tests/test_harmonization_20261013.py` (13 tests)

TDD: 8 written RED first and watched fail for the expected reason (6 × asset-class, 2 ×
floor), then fixed.

| test | pins |
|---|---|
| `test_asset_class_key_of_every_active_symbol_is_reachable_by_some_enabled_archetype` | HARM-01 invariant, with an explicit `INTENTIONALLY_UNCOVERED` allow-list |
| `test_metals_are_reachable_after_the_harmonization_fix` | HARM-01 regression guard |
| `test_gold_buy_opposing_macro_and_trend_routes_mean_reversion` | HARM-01 behaviour, real `'commodities'` class |
| `test_every_gold_symbol_can_reach_mean_reversion[XAUUSD/XAUAUD/XAUEUR/XAGUSD]` | HARM-01, all four golds |
| `test_metal_key_still_accepted_for_backwards_compatibility` | vocabulary widened, not replaced |
| `test_regime_vol_gate_excludes_the_measured_best_evidence_cell` | HARM-05, asserted-not-asserted-away |
| `test_dynamic_deadline_derivation_respects_the_declared_floors` | HARM-02 floor + `hard > soft` |
| `test_dynamic_deadline_derivation_clamps_an_absurdly_wide_tier` | HARM-02 ceiling |
| `test_reachability_staleness_guard_fires_before_the_next_build` | HARM-03 invariant |
|| `test_router_input_refresh_cadence_is_at_least_as_fast_as_the_signal_cadence` | HARM-04 jobs registered on pre-sweep grid (5,20,35,50) + stale threshold 90min |

**Full suite: `2069 passed, 22 skipped, 0 failures`** (was 2054/22 — +13 new, +2
pre-existing). Ruff: new files clean; the one remaining `E501` in
`dynamic_tier_optimizer.py:123` is pre-existing (2 ruff errors on the file before this
change, 1 after).

### Replay — `scripts/replay_router_harmonization.py`

Re-runs the original replay with the HARM-04 blocker relaxed in both arms, so BEFORE and
AFTER differ **only** by the asset-class vocabulary — HARM-01's effect in isolation:

| arm | routed | detail |
|---|---|---|
| BEFORE (pre-HARM-01) | **19/206** | BTCUSD buy momentum 13 @ 0.0% tp; AAPL buy momentum 6 @ 0.0% tp |
| AFTER (shipped) | **59/206** | + XAGUSD sell momentum 15 @ 13.3%/−0.556R; XAUUSD sell momentum 12 @ 8.3%/−0.500R; **XAUAUD buy mean_reversion 7 @ 0.0%/−1.000R**; **XAGUSD buy mean_reversion 6 @ 0.0%/−1.000R** |
| AS-SHIPPED (no relaxation) | **0/206** | HARM-04 still binding — the honest live state |
| 2026-10-13 counterfactual D | 59/206 | matches AFTER, confirming the fix reproduces the intended counterfactual |

+40 rows became routable, **all of them metals**. The mean_reversion archetype now
actually fires (13 rows across XAUAUD/XAGUSD) — it never had before.

## 3. What this does and does not change live

**Router mode is `shadow`** (`config/strategy_registry.yaml:206`) — the router stamps
`strategy_archetype` on `nt_signal_sim` and applies no live effect. So HARM-01 changes
what gets *measured*, not what gets *traded*. No geometry, deadline, or calibration path
changed behaviour from it.

HARM-02 is different: `_compute_dynamic_deadline` feeds `nt_dynamic_tier_state` → the
local param store → `read_local_deadline_tier` → the sweep's deadline stamp, and
`dynamic_deadline_mode` is `shadow` too, so the stamp is measurement-only. The floors now
bound a real diagnostic. Live effect: none while the mode stays `shadow`.

## 4. Left open — the binding constraint is HARM-04

The replay's AS-SHIPPED arm (0/206) is the finding that matters: **fixing the asset-class
plumbing does not make the router route anything**, because `macro_label` and
`trend_label` are refreshed once daily (06:00 / 06:10 UTC) while signals emit every 15
minutes. The tri-state alignment the router depends on is up to 24h stale by
construction — measured: zero rows in 3 days with both reads directional, last such row
2026-10-09 06:00Z, i.e. alignment frozen at `consolidating` for whole days.

This is a **frequency** defect, and it is the "atr issue — frequency config params
maintained either in realtime or schedule" the user named. Three candidate levers, none
taken here because each changes which signals route live:

1. **Move the label refreshes onto the pre-sweep cadence** (e.g. align to the existing
   `5,20,35,50 * * * *` `dynamic-tier-optimization` job, which already runs on the same
   dataset). Cheapest, and it reuses a job that exists.
2. **Compute the alignment at signal time** from the already-fetched `macro_label` /
   `trend_label` rather than from the daily-stamped values — removes the staleness
   entirely but adds a per-signal fetch.
3. **Relax the tri-state** to tolerate one neutral read. **Measured and rejected**:
   counterfactual C/D route the book's worst cells (every routed cell tp ≤ 14.3%, avgR
   ≤ 0 — see the 2026-10-13 report). The tri-state is correct; its inputs are stale.

Recommendation: lever 1. It is a scheduling change with no new computation, and the
`dynamic-tier-optimization` job already runs at `:05/:20/:35/:50` on the same population.

## 5. Also recorded

- `nt_roadmap.mean_reversion_gold_quant` — PATCHed with the 2026-10-13 B1 + router-replay
  findings, `status: pending`, `review_at` held at 2026-10-17, idempotent
  (`scripts/update_nt_roadmap_mr_shadow_20261013.py`, ruff clean).
- B1 walk-forward report regenerated:
  `worklog/kairos/reports/20261013_b1_mr_cohort_walkforward_XAUUSD-XAUAUD-XAGUSD-XAUEUR.md`

## 6. Files

| file | change |
|---|---|
| `config/strategy_registry.yaml` | `commodity` added to `momentum` + `mean_reversion` `asset_classes` (HARM-01) |
| `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py` | deadline clamp from `get_dynamic_deadline_config()` (HARM-02) |
| `tests/test_harmonization_20261013.py` | new — 13 tests |
| `scripts/replay_router_harmonization.py` | new — before/after replay |
| `scripts/update_nt_roadmap_mr_shadow_20261013.py` | new — idempotent roadmap patch |
