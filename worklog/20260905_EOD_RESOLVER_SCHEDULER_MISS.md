# 2026-09-05 — EOD resolver silently missed its daily fire, ~45h of zero resolutions

> **Priority**: P1 · **Task**: Add scheduler catch-up check for the daily
> EOD resolver job that silently missed its fire for ~45 hours ·
> **Status**: resolved · **Date**: 2026-09-05 · **Roadmap refs**: RM-10

## Trigger

Pulse check found `nt_signal_sim` had resolved **zero** rows since the
`2026-09-04 00:00` hour (4,993 resolved that hour, then nothing) —
spanning ~45 hours while new signals kept inserting normally.

## Root cause (confirmed via log timeline, not guessed)

`eod-lookback` (`orchestrator.py:1027-1034`) is a daily cron job
(`32 0 * * *` UTC) — the only thing that calls
`eod_resolver.resolve_pending_signals`. It has `misfire_grace_time=3600,
coalesce=True`, same as every other cron job here, but that only covers
delays *within* one continuously-running scheduler instance — it does
nothing across a restart, since `AsyncIOScheduler`'s in-memory job store
has no persistence: `add_job()` on a fresh instance computes the next
fire strictly forward from "now" (this exact failure mode was already
diagnosed once for `nt_macro_regime`/`nt_bracket_reachability`,
`worklog/20260901_SCHEDULER_MISSES_AND_LOG_SANITIZATION.md` — but
`eod-lookback` was never added to that fix's scope).

Reconstructed the exact miss from the log timeline:
- An instance starting `2026-09-04 20:30:08` ran clean light-sweep
  ticks straight through `2026-09-05 00:26:19` (the `00:25` tick).
- The next light-sweep log line jumps to `2026-09-05 02:31:35` — the
  process died somewhere in that ~2-hour gap, which swallows the
  `00:32` `eod-lookback` fire.
- The next instance started `2026-09-05 02:27:31`. A fresh scheduler
  registering `32 0 * * *` at `02:27` computes the next occurrence as
  `00:32` **the following day** (09-06) — silently skipping 09-05
  entirely. Confirmed zero log lines anywhere (`[eod:lookback] resolver
  done`/`resolver failed`/any exception) show it ever firing in the
  whole retained window.

Not a new bug pattern — the exact same class already fixed once,
just for a job that was out of that fix's scope, and arguably the
most consequential one to miss (it's the only thing that resolves
outcomes, which every downstream evidence table — calibration bias,
Tier 1/2 accuracy, macro edge — ultimately depends on).

## Fix

**`supabase_client.py`**: generalize `fetch_max_computed_at(table)` to
`fetch_max_computed_at(table, column="computed_at")` — same function,
same behavior for the two existing callers (default unchanged), but
now reusable for `nt_signal_sim.resolved_at` instead of needing a third
near-duplicate read function.

**`orchestrator.py`**:
- New `EOD_LOOKBACK_STALE_AFTER = timedelta(hours=20)` — same "just
  under the daily cycle" reasoning as `MACRO_REGIME_STALE_AFTER`
  (catches a miss the same day rather than waiting for a second full
  cycle). Accepted tradeoff, same as that existing constant: on a
  perfectly healthy day, any restart occurring in the last ~4 hours
  before the next scheduled `00:32` fire will ALSO see this as "stale"
  and re-trigger `_run_eod_lookback` redundantly — harmless, since
  `resolve_pending_signals` only acts on `outcome IS NULL` rows
  (idempotent, safe to over-run), and this is the exact same behavior
  already accepted for the macro-regime catch-up.
- `_catch_up_stale_jobs()` gains a third check: read
  `fetch_max_computed_at("nt_signal_sim", column="resolved_at")`,
  compare via `_is_stale`, fire `asyncio.create_task(self._run_eod_lookback())`
  if stale. Not gated behind a new settings kill-switch — the base
  `eod-lookback` job itself isn't gated by one either (unlike
  `MACRO_REGIME_ENABLED`/`BRACKET_REACHABILITY_ENABLED`, which gate
  jobs that DO have one), so the catch-up matches that existing
  posture rather than introducing new config surface for a job that's
  always-on.

## Explicitly not touched

- `auto-tuner-daily` (01:30 UTC) has the exact same theoretical
  exposure (fixed daily cron, no catch-up) — not in scope for this
  pass since it wasn't the reported symptom; flagged as a candidate
  for the same treatment if it's ever found stale.
- `eod_resolver.py`/`eod_correction.py`'s own resolution logic —
  untouched, the bug is purely "did the job ever get invoked," not
  "did it do the right thing once invoked."
- No change to `misfire_grace_time`/`coalesce` on the existing job —
  those already do what they're supposed to do within one running
  instance; the gap is specifically cross-restart memory, which is
  what the catch-up check adds.

## Build notes (2026-09-05, same day)

Built as scoped. 2 new tests
(`test_eod_lookback_stale_after_matches_documented_20_hours`,
`test_eod_lookback_threshold_shorter_than_full_daily_cycle`) in
`tests/test_scheduler_catchup.py`, mirroring the existing pattern for
the other two constants. Full suite: 696 passed (up from 694), 21
skipped, 0 failed.

**Second bug found and fixed while verifying, before shipping**: the
generalized `fetch_max_computed_at(table, column=...)` call for
`nt_signal_sim.resolved_at` initially returned `None` against real
Supabase data, even though resolved rows with real timestamps
definitely exist. Root cause: most `nt_signal_sim` rows are still-
pending (`resolved_at IS NULL`), and Postgres/PostgREST's default NULL
ordering puts NULLs FIRST on a `DESC` sort — so `order=resolved_at.desc`
returned a pending row's `NULL` as "the latest," not the actual most
recent resolution. The original two callers' columns
(`nt_macro_regime.computed_at`/`nt_bracket_reachability.computed_at`)
are never NULL, so this was latent and never surfaced until a nullable
column was passed. Fixed by changing the order clause to
`{column}.desc.nullslast` — verified against live Supabase afterward:
correctly returns `2026-09-04 00:49:44` (matching the confirmed freeze
window), while the two original callers are unaffected
(`nt_macro_regime` -> `2026-09-05 06:00:04`, `nt_bracket_reachability`
-> `2026-09-02 03:09:22`, both unchanged from before this fix).

Given the real `resolved_at` max is ~44 hours old against a 20-hour
threshold, `_catch_up_stale_jobs` will fire `_run_eod_lookback()`
immediately on the next studio restart/deploy.
