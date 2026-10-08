# Worklog: Single Source of Truth — Local Param Store vs Supabase Mirror

**Date:** 2026-10-08
**Scope:** Enforce a single source of truth between `config/*` (YAML + SQLite) and Supabase tables; stop optimization processes retrieving their own parameters from a DB to run.
**Related:** `worklog/20261008_PULSE_CHECK_AND_PIPELINE_EVALUATION.md` (the finding), `worklog/20261007_TABLE_FRESHNESS_AND_DYNAMIC_TIER_DESIGN.md` (the design this corrects), `worklog/PARAMETER_TUNING_PLAYBOOK.md` (the precedent pattern)

---

## 1. The Problem

`dynamic_tier_optimizer.py` computed four families of parameters before every sweep (dynamic ATR, dynamic deadline, regime edge, geometry incubation), wrote them to the `nt_dynamic_tier_state` **Supabase table**, and the sweep was supposed to read them back from that table.

Measured on 2026-10-08 (see the pulse-check worklog):

- **The sweep never read the table.** `_process_symbol` loaded brick geometry from `config/brick_params.yaml` and derived deadlines from a **per-signal Supabase fetch** of `nt_bracket_reachability`. The optimizer's output was write-only.
- **The table itself had no migration file** — it existed in production, created out-of-band, so the repo was not reproducible.
- The pre-sweep optimizer's whole purpose — "give the sweep fresh parameters without waiting for the nightly pass" — was not achieved, because nothing consumed the fresh parameters.

The deeper issue is architectural: **an optimization process was retrieving its own freshly-computed parameters back out of a database to run.** Parameters are local runtime state, not remote data. The process that computes them and the process that consumes them run in the **same Studio process, on the same persistent volume** — a network round-trip through Postgres to pass a value between two functions in one process is pure latency and a failure mode, not a design.

---

## 2. The Rule (single source of truth)

```
┌──────────────────────────────────────────────────────────────────┐
│  config/*  (YAML + SQLite)   ←  RUNTIME AUTHORITY                 │
│  Hot paths read local files/SQLite. Never Supabase to run.        │
└──────────────────────────────────────────────────────────────────┘
                    │  write_params()          ▲  read_tier() / read_value()
                    ▼                          │
┌──────────────────────────────────────────────────────────────────┐
│  nt_dynamic_tier_state (Supabase)  ←  DURABLE AUDIT MIRROR        │
│  Cross-instance fallback + parity check. Never the read path      │
│  for an optimization process.                                     │
└──────────────────────────────────────────────────────────────────┘
```

1. **Local store is the authority.** The optimizer writes parameters to `config/param_store.sqlite`; the sweep reads them from there on the hot path — no DB round-trip.
2. **Supabase is a mirror.** `nt_dynamic_tier_state` is the durable audit trail and cross-instance fallback (cold boot / fresh Studio). It is written best-effort and never blocks a sweep.
3. **Parity is enforced, not asserted.** `parity_report()` compares local vs mirror and reports divergence, so "single source of truth" is a checkable property rather than a comment.
4. **Fail-open everywhere.** A cold local store falls back to the previous derivation path (Supabase reachability fetch, or the module's own computation). A Supabase outage never blocks a sweep.

This is the **same shape already relied on** in this codebase — `config/signal_stream_cache.sqlite` (Supabase `nt_signal_sim` is the durable source, the SQLite file is the fast local working set), `config/tuning_registry.yaml` (file primary, `nt_tuning_params` audit + fallback), `config/brick_params.yaml`. This worklog generalizes that established pattern to the dynamic-tier family instead of inventing a new one.

---

## 3. What Was Built

### 3.1 `regime_platform/core/param_store.py` (new)

The local authority. Stdlib `sqlite3` (no new dependency), `asyncio.to_thread` for the blocking I/O (mirrors `signal_stream_cache.py`), best-effort throughout.

| Function | Role |
|---|---|
| `write_params(rows)` | Write parameter rows to the LOCAL store — the runtime authority |
| `read_all()` / `read_tier(symbol, type)` / `read_value(...)` / `read_float(...)` / `read_dict(...)` | Local-only reads; `None` when absent (fail-open) |
| `stats()` | Row counts by tier type + newest `computed_at` (diagnostics) |
| `sync_to_supabase()` | Best-effort mirror into `nt_dynamic_tier_state` (audit) |
| `sync_from_supabase(symbols)` | Cold-boot self-heal from the mirror |
| `parity_report(symbols)` | Detect divergence: `missing_in_mirror` / `missing_local` / `value_mismatch` |

Storage: `config/param_store.sqlite`, gitignored (live-mutated runtime state), same persistence argument as every other `config/*` local-state file — the repo checkout lives on Studio's persistent volume.

### 3.2 `dynamic_tier_optimizer.py` (wired)

The write path now goes **local-first, mirror-second**:

```python
written = await param_store.write_params(state_rows)   # authority
summary["state_rows_written"] = written
mirror = await param_store.sync_to_supabase()          # audit (best-effort)
summary["mirrored_to_supabase"] = mirror.get("mirrored", 0)
```

`regime_edge` and `dynamic_deadline` rows now store a **JSON object** as `tier_value` so the local store carries every field consumers need (`regime_edge`: `win_rate` + `expectancy_r`; `dynamic_deadline`: soft/hard/median/confidence) — one row, all fields, no second source.

### 3.3 Consumer read paths (local-first, fail-open)

| Consumer | Before | After |
|---|---|---|
| `regime_edge.get_regime_edge()` | Supabase `nt_regime_edge` fetch per lookup | Local store first → Supabase fallback |
| `dynamic_deadline.read_local_deadline_tier()` (new) | — | Local store; `None` → caller derives |
| `orchestrator._process_symbol` deadline block | Supabase `nt_bracket_reachability` fetch **per signal** | Local tier first → reachability fetch only when the store is cold |

The per-signal Supabase round-trip in the deadline block was the clearest instance of the anti-pattern: a hot path (every qualified signal) fetching reachability data to re-derive a tier the pre-sweep optimizer had already computed. It is now a local SQLite read in the common case.

### 3.4 `supabase/migrations/203_param_store_mirror.sql` (new)

The `nt_dynamic_tier_state` table had **no migration file**. This migration makes the repo reproducible and documents the table's role as a mirror. It is **idempotent** (`CREATE TABLE IF NOT EXISTS`) so it is safe to apply against the existing production table without data loss. Includes RLS + service policy + anon SELECT grant + `NOTIFY pgrst, 'reload schema'` per repo convention.

### 3.5 `.gitignore`

Added `config/param_store.sqlite{,-wal,-shm}` with the same explanatory comment shape as the other local-state files.

---

## 4. Bug Found & Fixed Along The Way

`orchestrator.py`'s deadline block referenced `_vol_tier(regime_label)` — but `_vol_tier` is defined in `bracket_reachability.py` and **was never imported into orchestrator.py**. The call raised `NameError`, which the surrounding `try/except` swallowed, so the entire deadline-tier derivation silently never ran in production (it always fell through to `_deadline_tier = None`).

Fixed by importing it explicitly as `_vol_tier_of` in both the local-read and the fallback branches. This is the same class of silent-failure bug the single-source-of-truth change is meant to eliminate: a swallowed exception made a mechanism look wired when it never executed.

---

## 5. Tests

`tests/test_param_store.py` — 13 tests, all passing. They are **wiring tests** (drive the real public entry points, assert the observable contract), not unit tests of internals:

- Store round-trip, upsert-overwrite, JSON-object values, stats
- Mirror is best-effort (a Supabase failure does not raise; local survives)
- Mirror normalises JSON-object values to a JSON string before the text-column upsert
- **The optimizer persists to the LOCAL store** (drives `run_dynamic_tier_optimization()` end-to-end)
- `regime_edge.get_regime_edge` reads local first and does **not** touch Supabase when the local store has the key (httpx client is replaced with an exploding stub, so a regression is loud)
- `dynamic_deadline.read_local_deadline_tier` returns a usable `DeadlineTier` (decay contract asserted)
- `parity_report` detects divergence and is clean when in sync

**Teeth proven:** with the local read disabled at runtime, `get_regime_edge` returns no local value and `read_local_deadline_tier` returns `None` — i.e. the tests fail on the pre-fix code. A regression test never seen to fail is not evidence.

Full suite: **1813 passed, 21 skipped** (the one remaining failure, `test_calibration_evidence_pool.py::test_yaml_ships_shadow_qs`, is **pre-existing** — HEAD already ships `calibration_evidence_pool_mode: live` while the test still asserts `shadow`; it is unrelated to this change and to `config/feature_flags.yaml`, which this work does not touch).

---

## 6. What This Does NOT Change

- **Live behavior is unchanged today.** The consumer read paths are fail-open: on the current Studio the local store is empty, so every consumer falls back to exactly its previous path. The change takes effect only once the pre-sweep optimizer next runs (`:05/:20/:35/:50`) and populates the local store.
- **No flag flips.** `dynamic_atr_mode`, `dynamic_deadline_mode`, `regime_edge_mode` are untouched — this is a plumbing change, not a behavior change.
- **The ATR-anchored brick geometry path** (`config/brick_params.yaml`, heavy sweep) is unchanged. `dynamic_atr` picks are stored locally too, but the brick-geometry apply path still runs through `tiered_atr.dynamic_atr_pick` / `heavy_sweep` as before; wiring the light-path geometry read to the store is a follow-up (see below).

---

## 7. Follow-ups

All four follow-ups below are **DONE** (2026-10-08, same session).

1. ✅ **Light-path geometry read wired to `param_store`.** The real per-signal Supabase round-trip on the light path was `geometry_risk.fetch_brick_state()` (reads `nt_brick_params_state` for `atr_at_write`, live via `geometry_intraday_scale_mode: live`). It now reads the local store first (new tier `brick_state`, key `latest`); the heavy sweep writes there via `geometry_risk.persist_brick_state_local()` alongside its Supabase mirror. Supabase stays the fallback for a cold store.
   - Note: the optimizer's `dynamic_atr` rows carry a *bucket label + expectancy* (a diagnostic), not the usable `tp_atr`/`sl_atr` geometry `dynamic_atr_pick()` produces — so they are NOT a drop-in for the geometry path and were deliberately left as diagnostics. The brick-state tier above is the actual hot-path read that needed the fix.
2. ✅ **`parity_report()` scheduled on the nightly EOD pass.** New `param_store.run_parity_check()` runs as Part IX of `_run_eod_lookback` and writes a `parameter_decay_check` audit row to `nt_eod_actions` (sentinel symbol `__param_store__`, whole report in `detail`) when local and mirror diverge. No row when in sync.
3. ✅ **Deploy order:** migration 203 applied to production (version `20261008215739`); code deploy + Studio pull/restart follow this worklog.
4. ✅ **Dead `upsert_dynamic_tier_state` import removed** from `dynamic_tier_optimizer.py` — it no longer calls it directly (goes through `param_store.sync_to_supabase()`); `supabase_client.upsert_dynamic_tier_state` now has exactly one caller (the store's mirror).

Tests: `tests/test_param_store.py` is now **17 tests** (4 added for FU1/FU2). Full suite: **1828 passed, 21 skipped, 1 pre-existing failure** (`test_yaml_ships_shadow_qs`, unrelated — HEAD ships `calibration_evidence_pool_mode: live`).

---

## 8. Files Changed

| File | Change |
|---|---|
| `regime_platform/core/param_store.py` | **new** — local authority + mirror + parity + `run_parity_check` |
| `regime_platform/sweep_orchestrator/dynamic_tier_optimizer.py` | write local-first, mirror-second; JSON-object tier values; dead import removed |
| `regime_platform/sweep_orchestrator/regime_edge.py` | local-first read in `get_regime_edge` |
| `regime_platform/sweep_orchestrator/dynamic_deadline.py` | **new** `read_local_deadline_tier` |
| `regime_platform/sweep_orchestrator/geometry_risk.py` | `fetch_brick_state` local-first; **new** `persist_brick_state_local` |
| `regime_platform/sweep_orchestrator/heavy_sweep.py` | persist brick state to local store alongside the Supabase mirror |
| `regime_platform/sweep_orchestrator/orchestrator.py` | deadline block reads local first; fixed missing `_vol_tier` import; EOD Part IX parity check |
| `supabase/migrations/203_param_store_mirror.sql` | **new** — idempotent table definition + RLS (applied: `20261008215739`) |
| `tests/test_param_store.py` | **new** — 17 wiring tests |
| `.gitignore` | `config/param_store.sqlite*` |
