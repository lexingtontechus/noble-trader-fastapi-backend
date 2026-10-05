# geometry_band_urgency — Defect Record & Build Log

**Date:** 2026-10-05
**Status:** live (cooldown fix verified in production); one design gap open (§4)
**Deployed:** `40a4353` on Studio, orchestrator restarted 03:42 UTC
**Prior art:** `worklog/20261004_GEOMETRY_BAND_URGENCY_AND_EPISODE_DEDUP.md` (the
module's first build), `worklog/20261004_GEOMETRY_BAND_CALIBRATION_URGENCY_SCOPE.md`

---

## 1. Why this document exists

`geometry_band_urgency.py` was shipped live and took **six** defects to become
correct. Four were introduced during the build and survived a green test suite.
This records each defect, its detection, and the test that would have caught it —
because the pass count was not evidence of correctness.

The module is a sweep-triggered job that re-computes a geometry bucket's
reachability statistics when its calibration bias has drifted. It is a
*re-optimization* job (recompute from recent resolved signals), not a threshold
lookup.

## 2. Defect record

| # | Defect | How it was found | Fix |
|---|---|---|---|
| 1 | Upsert payload used columns that do not exist (`p_resolve`, `p_win_resolve`, `expectancy_r`); the real table has `pct_hit_tp`/`pct_hit_sl`/`pct_expired`/`median_hours_to_resolve`/`p75_hours`/`p90_hours`/`atr_source` | Code read against migration 120's DDL | Rewrote payload to the real columns |
| 2 | `geometry_band_urgency` was absent from the `nt_eod_actions.action` CHECK — every insert would fail 23514 | Read the constraint history (migrations 116/118/158) | Migration 195 |
| 3 | `lookback_days: 7` in YAML was silently dropped: `get_geometry_band_urgency_config()` copies only keys already in `_DEFAULTS` | Smoke test after deploy printed the resolved config | Added `lookback_days` to `_DEFAULTS` |
| 4 | Cooldown state was read from an `nt_roadmap` row that has never existed; `write_job_heartbeat()` only PATCHes an existing row, so nothing persisted | Queried `nt_roadmap` for the mechanism — empty | Source the state from `nt_eod_actions` itself |
| 5 | Cooldown gate passed epoch floats through `_ts_or_none()` (an ISO-string parser) → always `None` → cooldown never applied | Live log showed 53 cells re-firing every sweep | Compare the float directly |
| 6 | **Open — see §4.** The job writes fresh `nt_bracket_reachability` stats but never refreshes `nt_calibration_bias_by_cell`, the table it reads to detect drift | Cadence read of the refresh path | not fixed |

### 2.1 Defects 4 and 5 were mine, and the tests passed through both

- **#4** was a wrong assumption about a helper's contract (`write_job_heartbeat`
  updates, it does not upsert).
- **#5** was a type mismatch introduced by the #4 fix, in the *wiring* between
  the loader and the gate.

The reason both survived: `_load_last_fired()` was unit-tested **in isolation**
and was correct. Nothing exercised `check_geometry_band_urgency()` end-to-end.
The tests asserted the pieces that were thought about, never the contract the
system depends on.

## 3. Detection evidence (live)

| Time (UTC) | Observation | Meaning |
|---|---|---|
| 10-05 00:47 | `checked=14 triggered=59 recomputed=59` | first run, all drifted cells fire |
| 10-05 01:01 | `checked=14 triggered=59 recomputed=59` | **cooldown broken** (#5) — 14 min apart |
| 10-05 02:46 → 03:16 | 53 cells re-fire on **four** consecutive sweeps | same bug, after #4's fix deployed |
| 10-05 03:46 | `checked=14 triggered=0 recomputed=0` | **fixed** — all cells inside cooldown |

The 03:46 line is the first end-to-end proof in this workstream that a fix
actually took effect in production.

## 4. Open design gap — the drift loop does not close

**What the job does:** reads per-cell bias from `nt_calibration_bias_by_cell` →
if `|bias_shrunk| ≥ threshold`, recomputes that bucket's reachability stats from
the last 7 days of resolved signals → upserts `nt_bracket_reachability` → logs
`nt_eod_actions`.

**What is missing:** nothing recomputes `nt_calibration_bias_by_cell` from the
refreshed reachability. The bias table is rebuilt by
`build_calibration_bias_by_bucket`, which runs inside `_run_bracket_reachability`
— scheduled **daily at 01:17 UTC** (the `eod-lookback` job) and weekly (Sun 07:00),
plus a startup-catchup when the reachability table is stale.

Verified live: `nt_calibration_bias_by_cell.computed_at = 2026-10-05T01:18:58Z`,
`nt_bracket_reachability.computed_at = 2026-10-05T01:18:44Z` — both from the
01:17 pass.

**Consequence:** a drift detected at 02:00 has its reachability refreshed, but the
bias reading that triggered it stays stale until 01:17 the next day. The trigger
condition therefore **cannot clear on its own** — the same cell re-fires every
cooldown window (4h → up to 6×/day/cell) for the rest of the day, each time
recomputing from the same 7-day window and writing the same values.

The cooldown bounds the churn; it does not make the loop converge. This is the
substantive remaining defect, and it is a design issue rather than a bug.

**Options (not yet decided):**

1. Have the job recompute the affected cells' bias after writing reachability,
   so the trigger clears immediately. Requires the cell-bias builder to be
   callable per-cell (today it rebuilds all 231 rows for 14 symbols).
2. Widen the cooldown to ≥24h so at most one re-fire per cell per day, matching
   the bias refresh cadence. Cheap, honest, but concedes the loop stays open.
3. Drop the bias table as the trigger and detect drift directly from the
   reachability row's own `n`/`pct_*` history.

## 5. Test strategy — what changes going forward

The rule for anything touching the sweep, adopted after this build:

- **Drive the real entry point** (`check_geometry_band_urgency()`,
  `_sweep_group()`, `_clef_flash_stamp()`) with the fake at the **transport
  boundary** (`httpx.AsyncClient`), never by patching the module's own helpers.
  Patching `_load_last_fired` is precisely how #5 hid — it tested the assumption,
  not the integration.
- **Assert the observable contract**: the request bodies the fake captures (i.e.
  what would land in `nt_bracket_reachability` / `nt_eod_actions`) and the
  returned summary. Not internal call counts.
- **One test per failure mode, each verified to fail on the pre-fix code.**
  `TestCooldownGate` was checked by reverting the gate line and watching it fail
  (`recomputed=1`, expected `0`), then restoring.
- **Fixture-integrity guard** so a test cannot silently pass on a degenerate
  fixture. `test_calibration_bias_bucket`'s failure in this cycle was accidental
  — three rows identical in every keyed field happened to expose the episode
  dedup, not a designed assertion.

Implemented: `tests/test_geometry_band_urgency.py` — 18 tests, of which
`TestCooldownGate` (3) is the end-to-end wiring check and `TestLastFiredCooldown`
(4) covers the ledger-sourced state.

## 6. Artefacts

| Artefact | Path |
|---|---|
| Module | `regime_platform/sweep_orchestrator/geometry_band_urgency.py` |
| Sweep hook | `orchestrator.py::_run_geometry_band_urgency` (called at the end of `_sweep_group`) |
| Config | `config/feature_flags.yaml` → `geometry_band_urgency` (global + `by_symbol`, FOREX shared key) |
| Accessor | `feature_flags.py::get_geometry_band_urgency_config()` |
| Migration | `supabase/migrations/195_nt_eod_actions_geometry_band_urgency.sql` |
| Tests | `tests/test_geometry_band_urgency.py` |
| Episode dedup (shared) | `bracket_reachability._dedupe_episodes` |

## 7. Verified state

- Cooldown holds: `recomputed=0` on a re-run inside the 4h window (03:46 UTC).
- 59 actions written on the first run, with the episode dedup visible in the
  payload (e.g. US500 buy/`1-2`: `old_n 324 → new_n 62`).
- Full suite: 1806 passed, 21 skipped.
- Studio `40a4353`, no errors/tracebacks since restart.
