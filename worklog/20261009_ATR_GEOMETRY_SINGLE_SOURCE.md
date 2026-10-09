# Worklog: ATR Geometry Single Source of Truth — one picker, one store, both sweeps read it

**Date:** 2026-10-09
**Scope:** Close orchestrator-chain gaps 1+2 — the `dynamic_atr` tier was a dead write, and the ATR picker never ran on the light path.
**Related:** `worklog/20261008_PARAM_SOURCE_OF_TRUTH.md` (the same single-source shape), `worklog/20261008_GEOMETRY_BASIS_DIVERGENCE.md` (Stage 5 gate on this same pick), `docs/diagrams/orchestrator_chain.html` (the chain map that surfaced the gaps)

---

## 1. The two gaps (from the chain triage)

| # | Gap | Evidence |
|---|---|---|
| **1** | The `dynamic_atr` tier is a **dead write** | `dynamic_tier_optimizer._compute_dynamic_atr` writes 17 per-symbol rows into `param_store` every 15 min. **No production code reads `TIER_DYNAMIC_ATR`** — only `tests/test_param_store.py`. Live confirmation: 17 rows in `config/param_store.sqlite`, `computed_at 04:05`. |
| **2** | The ATR picker **never runs on the light path** | `tiered_atr.dynamic_atr_pick` is called **only from `heavy_sweep.py`**. The light sweep (every 15 min, the live signal generator) reads geometry from `config/brick_params.yaml`, which only the heavy sweep rewrites — so the stop/target *shape* refreshes **daily**, not at the advertised 15-min cadence. |

Both gaps are the **same defect class already eliminated twice this week** (param store, geometry basis): **two implementations of one concept that never meet.**

---

## 2. The root cause is not "a missing read"

The obvious fix — "make the sweep read the `dynamic_atr` tier" — **does not work**, because the two sides compute *different things*:

| | `tiered_atr.dynamic_atr_pick()` | `dynamic_tier_optimizer._compute_dynamic_atr()` |
|---|---|---|
| **Input** | TradingView 1h bars + `nt_signal_sim` signals | `nt_signal_sim` resolved rows only |
| **Method** | full live-basis walk (`bracket_envelope` → `select_tp_sl`) over a TP/SL grid | bucket the *stored* brackets by `distance/ATR`, pick the best-expectancy bucket |
| **Output** | `DynamicAtrPick` — `tp_atr`, `sl_atr`, `exp_atr`, `exp_h1`, `exp_h2`, `expiry`, `n` | `{bucket: "2-4", expectancy_r: -0.31, n: 134}` |
| **Usable as geometry?** | **yes** — `to_price_levels()` gives entry ± tp_atr·ATR | **no** — a bucket label, not a bracket |

The prior worklog already noticed this and *deliberately left the tier as a diagnostic* (`20261008_PARAM_SOURCE_OF_TRUTH.md` §7 follow-up 1):

> the optimizer's `dynamic_atr` rows carry a *bucket label + expectancy* (a diagnostic), not the usable `tp_atr`/`sl_atr` geometry `dynamic_atr_pick()` produces — so they are NOT a drop-in for the geometry path and were deliberately left as diagnostics.

That was the right call *for a plumbing change*. It is the wrong end state: it leaves a 15-minute optimizer producing a value nothing consumes, while the actual geometry refreshes daily. **The single-source-of-truth resolution is to collapse the two computations into one.**

---

## 3. The rule

```
┌───────────────────────────────────────────────────────────────────────┐
│  tiered_atr.dynamic_atr_pick()   ←  THE ONE ATR COMPUTATION            │
│  live-basis walk → DynamicAtrPick (tp_atr, sl_atr, exp, n)            │
└───────────────────────────────────────────────────────────────────────┘
        │ write_pick_to_store()                    ▲ read_pick_from_store()
        ▼                                          │
┌───────────────────────────────────────────────────────────────────────┐
│  param_store tier "dynamic_atr"  key "latest"  ←  RUNTIME AUTHORITY    │
│  JSON: {tp_atr, sl_atr, exp_atr, exp_h1, exp_h2, expiry, n}           │
└───────────────────────────────────────────────────────────────────────┘
        │                    │                         │
        ▼                    ▼                         ▼
   heavy sweep          light sweep              optimizer
   (apply)              (apply)                  (refresh clock)
```

1. **One computation** — `dynamic_atr_pick`. The optimizer's parallel bucket computation is **retired**; it becomes the clock that *calls* the picker.
2. **One store** — the `dynamic_atr` tier now carries the **usable geometry** (JSON), not a bucket label. It is the runtime authority, read without a network round-trip.
3. **Both sweeps read it** — the light path gains the same apply block the heavy path has had.
4. **Fail-open everywhere** — a cold store or a `None` pick falls through to the existing `brick_params.yaml` geometry. No new failure mode on the hot path.

---

## 4. What was built

### 4.1 `tiered_atr.py` — the pick becomes store-backed

| Function | Role |
|---|---|
| `DynamicAtrPick.to_json()` / `from_json(d)` | Serialise the pick to/from the tier's JSON value |
| `read_pick_from_store(symbol, *, max_age_hours)` | Local param_store read, freshness-bounded by `computed_at`; `None` when absent/stale |
| `write_pick_to_store(symbol, pick, *, source)` | Write the usable geometry to tier `dynamic_atr`, key `latest` |

`dynamic_atr_pick()` resolution order is now **in-process cache → store (fresh) → compute**, and it **writes the store** after computing. The store read gives cross-restart persistence; the in-process cache keeps the hot path free of disk I/O.

### 4.2 `dynamic_tier_optimizer.py` — the divergent computation is retired

`_compute_dynamic_atr(resolved_rows)` (the bucket/expectancy diagnostic) is **removed**. In its place, `_refresh_dynamic_atr(peers)` calls the real picker per symbol — the optimizer is now the 15-minute clock that keeps the store fresh, which is exactly its documented purpose ("pre-sweep refresh of dynamic ATR"). The `dynamic_atr` rows are no longer assembled by the optimizer; the picker writes them.

### 4.3 `orchestrator.py` — the light path gains the apply block

`_process_symbol` reads the pick from the store and, when the light-apply flag is `live`, overrides the brick geometry with exact ATR price levels — **mirroring `heavy_sweep.py`'s existing block** (`to_price_levels`), so both sweeps settle on the same bracket from the same value.

### 4.4 New flag: `dynamic_atr_light_mode` (off | shadow | **shadow**)

The heavy apply is gated by the existing `dynamic_atr_mode` (already `live`). The light apply is **new behaviour on the live signal path**, so it ships behind its own flag at **`shadow`** — it logs what it *would* apply without changing a published signal, per the standing shadow-first convention. Flipping to `live` is one config edit, no restart.

---

## 5. Tests

`tests/test_atr_single_source.py` (new) — wiring tests driving the real entry points:

- `DynamicAtrPick` JSON round-trip (every field survives).
- `read_pick_from_store` returns `None` when cold, a pick when fresh, `None` again once stale.
- `write_pick_to_store` → `read_pick_from_store` round-trip through the real param_store.
- The **optimizer refreshes the store by calling the picker** (picker stubbed; asserts the tier is populated with usable geometry, not a bucket label).
- The light path **applies** the store pick when `live` and **does not** when `shadow` (the published bracket is byte-identical to the pre-change geometry in shadow).
- Fail-open: a cold store leaves the brick geometry untouched.

Full suite: **1843 passed, 21 skipped, 1 pre-existing failure** (`test_yaml_ships_shadow_qs` — yaml ships `calibration_evidence_pool_mode: live`, the test still asserts `shadow`; unrelated to this change).

---

## 6. What this does NOT change

- **Heavy-sweep behaviour is unchanged.** It already called the picker; the picker now additionally persists, which does not alter the pick.
- **Live light behaviour is unchanged today.** `dynamic_atr_light_mode: shadow` — the light path computes the would-apply bracket and logs it, but publishes the same geometry as before.
- **`brick_params.yaml` remains the fallback geometry.** A cold store (fresh Studio, empty SQLite) falls through to exactly the previous path.
- **No migration.** The tier table (`nt_dynamic_tier_state`, migration 203) already exists; `dynamic_atr` was already in `_MIRROR_TIER_TYPES`.

---

## 7. Files changed

| File | Change |
|---|---|
| `regime_platform/sweep_orchestrator/tiered_atr.py` | `to_json`/`from_json`; `read_pick_from_store`; `write_pick_to_store`; picker store-backed |
| `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py` | `_compute_dynamic_atr` retired → `_refresh_dynamic_atr` calls the picker |
| `regime_platform/sweep_orchestrator/orchestrator.py` | light-path apply block (store read → `to_price_levels`), gated by `dynamic_atr_light_mode` |
| `regime_platform/core/feature_flags.py` | `get_dynamic_atr_light_mode` + default `shadow` |
| `config/feature_flags.yaml` | `dynamic_atr_light_mode: "shadow"` |
| `tests/test_atr_single_source.py` | **new** — store round-trip + light apply + optimizer refresh |
| `tests/test_param_store.py` | updated for the retired `_compute_dynamic_atr` |

---

## 8. Live-deploy verification (2026-10-09 05:20 UTC)

Pulled `751bf30` on the Studio and restarted the orchestrator (PID 28650, health 200).
The 05:20 pre-sweep proved the wiring end-to-end:

```
05:20:06 [dynamic:atr] scope=XAGUSD ... grid tp=[2.21,2.46,2.71] sl=[2.3,2.42,2.54]
05:20:06 [dynamic:atr] scope=XAGUSD basis=intrabar policy=sl_first n=232
... (all 14 symbols walked) ...
05:20:16 [dynamic-tier] done: atr=0 deadline=56 edge=157 incubation=14 errors=0
```

The optimizer now **calls the picker for all 14 symbols** — the retired
`_compute_dynamic_atr` is gone and `atr_picks` counts real picker results.

### Finding: the picker returns `None` for every symbol — and that is correct

`atr=0` is **not** a wiring failure. Dumped the full candidate grid for XAGUSD
on the live basis:

| tp | sl | exp (mean) | exp_h1 | exp_h2 | qualifies |
|---|---|---|---|---|---|
| 2.21 | 2.30 | +0.519 | **−0.123** | +1.160 | ✗ |
| 2.46 | 2.42 | +0.525 | **−0.148** | +1.198 | ✗ |
| 2.71 | 2.54 | +0.560 | **−0.141** | +1.262 | ✗ |

Every candidate is **negative in the first half** and strongly positive in the
second. `select_tp_sl` requires positive expectancy in *both* halves, so it
correctly declines. This is the **same verdict the heavy sweep has always
gotten** (`[dynamic:atr] XAGUSD mode=live no qualifying TP/SL — heavy kept`),
and it is the direct, expected consequence of the 2026-10-08 geometry-basis
harmonization: once the picker stops seeing a wick-free close-walk, the tight
FX/commodity bands no longer clear the both-halves bar.

**So the wiring is live and the value is legitimately absent.** Two
consequences worth stating plainly:

1. The light path's `dynamic_atr_light_mode: shadow` therefore logs nothing
   today (there is no pick to would-apply) — it is a correct no-op until a
   bracket qualifies. That is the intended fail-open behaviour, not a gap.
2. The `dynamic_atr` tier will stay empty until a symbol clears the both-halves
   bar. The **`brick_params.yaml` geometry remains the operative source** for
   both sweeps meanwhile, exactly as before this change.

### Cleanup: legacy per-bucket rows pruned

The old code wrote one `dynamic_atr` row **per distance-bucket** (keys `4+`,
`2-4`, `1-2`, `0.5-1`); the new code writes exactly one row (key `latest`).
18 legacy rows existed on the Studio. `param_store.delete_keys()` (new) prunes
them: `write_pick_to_store` prunes on a successful write, and
`_refresh_dynamic_atr` prunes for every symbol regardless of pick outcome — so
a symbol with no qualifying bracket does not keep unreachable rows alive as
permanent parity drift (the same accumulate-stale-rows trap fixed for the
mirror on 2026-10-08).

Tests: `tests/test_atr_single_source.py` is now **13 tests**.

