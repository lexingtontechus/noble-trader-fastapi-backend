## [9.3.0] - 2026-08-12

> **Priority**: P0 · **Task**: Fix nt_signal_sim/EOD resolver health (window anchoring, throughput, retention, capture trim) ·
> **Status**: resolved · **Date**: 2026-08-12

### Fixed — nt_signal_sim DB health: resolver window anchoring, throughput, retention, capture trim

**Incident (2026-08-11/12 live verification):** `nt_eod_actions` was EMPTY
(0 rows) even though the EOD job runs nightly at 00:30 UTC and the resolver
successfully resolved 200 signals per run. Root-cause chain verified live:

1. `run_eod_correction()` only writes actions when `fetch_calibration_bias()`
   returns a bias, and that function returns `None` when a symbol+venue has
   **fewer than 20 calibration records** (`supabase_client.py:456`). Current
   per-symbol counts — every symbol sits at 18–19, just under the bar — so
   `bias=None` for all symbols → 0 actions → `nt_eod_actions` stays empty.
2. `nt_calibration_records` was starved because the EOD resolver's calibration
   inserts failed PG 23502 (missing `id` + `aggression`) on Aug 8/9/11 —
   fixed in commit 30a8c78 and confirmed live-deployed by the Aug 12 00:30
   run (182 inserts OK).
3. **Backlog:** `nt_signal_sim` had 17,992 rows, 16,792 pending (93%),
   capture ≈ 3,200/day vs resolver 200/night → net +3,000/day, oldest pending
   from Aug 6 (5+ day resolution latency).
4. **Window-anchoring bug:** `resolve_signal_outcome` walks `closes[:48]` —
   the FIRST 48 bars of the fetched 200-bar window. With a backlog, those
   bars are from ~8 days ago (pre-entry) → the resolver scored Aug 6 signals
   against Aug 3–5 price action, yet wrote those outcomes + calibration
   records as truth. Resolution must anchor to the signal's entry position.

This change implements the 4 required actions from the 2026-08-12 assessment:

**Action 1 — Fix resolver window anchoring (root cause).**
`_fetch_pending` now filters `ts >= now() - LIGHT_CANDLE_BARS hours` so the
resolver only picks signals whose entry falls inside the candle window it
fetches. New pure helper `anchor_post_entry_closes(closes, signal_ts,
fetch_ts)` computes the entry offset (bars from the newest close) and slices
`closes` to the post-entry window (the fetched array is ascending, so
`closes[-(offset):]` starts at the entry bar). Signals older than the window
are skipped and counted in the summary (`windowed_out`). The close-walk then
resolves the correct post-entry price action.

#### regime_platform/sweep_orchestrator/eod_resolver.py - MODIFIED
- `_fetch_pending`: optional `min_ts` filter param (ISO string → PostgREST
  `ts=gte.<iso>`); `resolve_pending_signals` passes `now() - LIGHT_CANDLE_BARS hours`.
- `anchor_post_entry_closes(closes, signal_ts, fetch_ts)` — NEW pure helper:
  returns the post-entry close slice for a signal, or `None` when the entry
  predates the fetched window (can't resolve). Ascending-order aware.
- `resolve_pending_signals`: per-symbol loop now slices closes via the anchor
  helper before `_resolve_one`; unresolvable (windowed-out) signals are
  skipped with a summary counter instead of being resolved against stale bars.

**Action 2 — Raise throughput + one-time drain.**
The nightly cap is now configurable (`EOD_RESOLVE_LIMIT`, default 1000) instead
of a hardcoded 200. A new `scripts/drain_signal_backlog.py` CLI loops
`resolve_pending_signals` with a high limit until the pending backlog is
exhausted (or a max-iterations safety cap), for the one-time 16k drain.

#### regime_platform/core/settings.py - MODIFIED
- `EOD_RESOLVE_LIMIT: int = 1000` — nightly EOD resolver cap (was hardcoded 200).
- `EOD_RETENTION_RESOLVED_DAYS: int = 30` — delete resolved rows older than N days.
- `EOD_RETENTION_STALE_DAYS: int = 7` — mark unresolved rows older than N days `expired`.
- `NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY: bool = False` — Action 4 toggle.

#### scripts/drain_signal_backlog.py - NEW
- One-time backlog drain: `resolve_pending_signals(limit=2000)` in a loop
  until pending == 0 (max 20 iterations), prints per-pass summary. Read-only
  except for the resolver's own PATCH/inserts. Never runs the sweep engine.

**Action 3 — Retention/prune.**
New `regime_platform/sweep_orchestrator/eod_retention.py` — nightly
best-effort pass (wired into `_run_eod_lookback` Part IV) that:
1. Deletes resolved (`outcome IS NOT NULL`) rows older than
   `EOD_RETENTION_RESOLVED_DAYS` (30) via PostgREST DELETE + Content-Range
   count (handles the 1000-row cap with pagination).
2. Marks unresolved rows older than `EOD_RETENTION_STALE_DAYS` (7) as
   `expired` via PATCH — they are neither wins nor losses, so calibration
   records are deliberately NOT written for them (same convention as the
   resolver's 48-bar expiry).

#### regime_platform/sweep_orchestrator/eod_retention.py - NEW
- `run_eod_retention() -> dict` — deletes old resolved rows, expires stale
  pending rows; never raises (every failure logged + counted). Returns
  `{deleted_resolved, expired_stale, errors}`.

#### regime_platform/sweep_orchestrator/orchestrator.py - MODIFIED
- `_run_eod_lookback`: Part IV calls `run_eod_retention()` after the
  emergency heavy-sweep batch; summary logged at INFO.

**Action 4 — Optional capture trim (default OFF).**
`NOBLE_TRADER_SIM_CAPTURE_QUALIFIED_ONLY` (default `False` preserves current
behavior): when `True`, only QUALIFIED non-neutral signals are persisted to
`nt_signal_sim` (cuts ~93% of the noise rows; changes what the sim measures,
so opt-in).

#### regime_platform/sweep_orchestrator/orchestrator.py - MODIFIED
- `_process_symbol` §11a: skips `log_signal_sim` when the toggle is on and
  the signal is not qualified.

#### regime_platform/sweep_orchestrator/heavy_sweep.py - MODIFIED
- §10a: same qualified-only gate for the heavy-sweep capture.

#### tests/test_eod_resolver.py - MODIFIED
- `TestAnchorPostEntryCloses` — anchoring pure-logic tests: ascending closes,
  entry at mid-window, entry at newest, entry older than window → None,
  fractional offset rounding.

#### tests/test_eod_retention.py - NEW
- `TestEodRetention` — settings defaults; DELETE/PATCH URL construction;
  pagination-aware count; best-effort never-raises on HTTP errors.

#### tests/test_eod_lookback_wiring.py - NEW
- `TestEodLookbackWiring` — `_run_eod_lookback` calls retention Part IV;
  capture-site toggle gates `log_signal_sim` in both sweep paths.

### Verified
- Live DB (2026-08-12): `nt_signal_sim` total 17,992 / pending 16,792 /
  resolved 1,200; per-day capture deltas 3,005/2,639/3,295/3,723/3,264.
- Live insert probe: `nt_eod_actions` POST with the exact `_write_eod_action`
  payload → HTTP 201 (action_id auto-generated), DELETE 204 cleanup. Inserts
  cannot fail on schema (PK default `gen_random_uuid()`, CHECK enum covers all
  4 written actions, JSONB detail accepted).
- Full backend suite: PASS (see test run output below).
