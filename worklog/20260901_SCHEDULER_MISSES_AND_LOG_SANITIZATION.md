# 2026-09-01 — Two infra issues from the health-check thread: scheduler misses, log truncation/noise

Scope only, continuing from `20260831_INFRA_HEALTH_DISK_IO_CALIBRATION_TRIAGE.md`'s
health checks. Nothing built yet.

## Issue 1: fixed-time-of-day jobs silently skip across restarts

**Confirmed symptom**: `nt_macro_regime` stuck 3.5 days stale (last
successful run 2026-08-29 06:00 UTC); `nt_bracket_reachability`/
`nt_calibration_bias_by_bucket` stuck 6 days stale (last run 2026-08-26
07:00 UTC); `nt_macro_edge_by_symbol`/`nt_hmm_shadow_accuracy`/
`nt_entry_drift` (all chained onto that same weekly job) show zero rows
ever. Meanwhile the 00:32 UTC daily EOD lookback and the 4-hourly
intraday-correction jobs are firing reliably — confirmed via a direct
`nt_signal_sim`/`nt_calibration_records` join (1,373/1,373 matched for
today's run).

**Root cause**: APScheduler's job store here is in-memory. A misfire is
only caught if the *same running scheduler instance* was alive at the
trigger time but got delayed executing — `misfire_grace_time` cannot
help here, and tuning it further is not the fix, because the actual
failure mode is different: after a restart, `add_job()` on a fresh
in-memory scheduler computes "next fire time" strictly forward from
*now*, with zero memory that today's/this-week's fire time already
passed. The studio's restart timestamps cluster tightly around
06:2x-06:5x UTC daily — almost exactly straddling the 06:00 daily
trigger (and plausibly 07:00 Sunday too) — while 00:32 and the 4-hourly
marks fall in the gaps between restarts and keep firing normally. This
is downstream of the already-known ~4h restart cadence, not a separate
bug in either job's own logic.

### Recommended fix: catch-up check on `start()`, reading existing data — not a new config file

Considered and rejected a dedicated "last-run" config file: it would just
be a second, separately-mutated copy of information the database already
has (`computed_at` on both tables), and a tracked-but-separately-mutated
file is the exact failure mode `config/brick_params.yaml` already
documents as a known trap in this codebase.

**Also rejected: a persistent APScheduler job store** (e.g.
`SQLAlchemyJobStore`), the more "textbook" fix. Reason: it depends on
local state surviving whatever kind of restart this actually is — and
the same restarts are *already* truncating the local orchestrator log
(see Issue 2), which suggests local state doesn't reliably survive
these restarts. A check against Supabase (definitely survives — it's
remote) is more robust to that uncertainty than betting on a local
job-store file.

**Proposed**: right after registering each of the two affected jobs in
`start()`, a one-off staleness check against the table each job itself
writes to:
```python
last = await fetch_max_computed_at("nt_macro_regime")
if last is None or (now - last) > timedelta(hours=20):
    asyncio.create_task(self._run_macro_regime_refresh())
```
Same shape for the weekly job (`nt_bracket_reachability`, threshold
~6.5 days). Thresholds deliberately shorter than the full cycle so a
recent legitimate run doesn't get needlessly re-triggered by the next
routine restart. Small, contained: one new helper function (a `select
max(computed_at)` read, mirrors patterns already used everywhere this
session), two call sites in `start()`, no new schema, no new config.

**Level of effort**: small — comparable to the config-flag additions
earlier this session, not a rearchitecture. Rough size: one helper
function + ~10 lines at each of 2 call sites + tests for the staleness
threshold logic (pure function, easy to unit test).

## Issue 2: log truncation — root cause is size, not restart, and the real fix is volume

**Correcting my own earlier read**: I'd attributed log truncation to
"the restart wipes the log." That's not quite what's happening — the
user pointed me to the actual mechanism:
`noble-trader-fastapi-backend/lightningstudio/on_start.sh` (tracked in
this repo, copied to the studio's `.lightning_studio/` root on start).
It runs a `truncate_log()` pass on every startup that keeps only the
last `LOG_MAX_KB` (default 10 MB) of each service log — explicitly
size-triggered, not restart-triggered; the script's own comment says
logs are "appended to (not overwritten) so you can see history across
restarts."

**Why it looks the same in practice**: log volume is high enough that
`orchestrator.log` crosses the 10 MB threshold within a similar
timeframe to the ~4h restart cadence, so in practice truncation fires on
close to every restart anyway — but the actual lever is volume, not the
restart itself. Fixing verbosity fixes retention directly, without
touching the restart cadence or `on_start.sh` at all.

**Quantified where the volume actually goes** (87,225 lines in the
current file):
- **30,888 lines (35.4%)** are routine `[INFO] httpx: HTTP Request: ...`
  lines — one per Supabase/TradingView/Discord/TimesFM call, logged by
  the `httpx` library itself at INFO, not something this codebase's own
  loggers control today.
- A further meaningful chunk is the per-rejected-signal `Gate details`
  dump (~7-8 lines every time a signal fails qualification) — given the
  qualified rate is ~1-2%, this fires on nearly every signal-sim capture.
- At least one already-flagged-as-temporary diagnostic is still live at
  INFO: the "markov diag" line in `orchestrator.py` carries its own
  2026-08-22 comment saying "Remove once the mechanism is confirmed and
  a fix lands" — it has been confirmed; it's still logging on every
  tick.

**Proposed fix, cheapest first**:
1. `logging.getLogger("httpx").setLevel(logging.WARNING)` once at app
   startup — suppresses the 35% baseline without touching the app's own
   log lines at all. Lowest-risk, highest-ratio change available.
2. Demote (not delete) the per-rejection `Gate details` block to DEBUG —
   it's genuinely useful when actively debugging a specific symbol's
   qualification path, just not at the volume of "every single tick for
   13 of 14 symbols."
3. Remove the `_markov diag_` line now that Tier 1/2 shipped and the
   mechanism it was diagnosing is understood — it did its job.
4. Leave `apscheduler`/`regime.sweep.orch`'s own INFO-level milestone
   lines (job start/stop, qualified signals, resolver summaries) as-is —
   those are the actual signal this log exists to carry.

**Not proposing**: touching `on_start.sh`'s truncation mechanism itself
(10 MB is a reasonable cap; the problem is what fills it, not the cap),
or a full structured-logging rewrite — that's a bigger, separate
undertaking or this session over-scoping a good idea past what's needed.

**Level of effort**: very small — a handful of logger-level/line changes
in 1-2 files, no schema, no config, no test changes beyond confirming
existing log-dependent tests (if any capture on specific INFO lines)
still pass. Realistically an hour of work, most of it spent confirming
nothing downstream parses the demoted lines by scraping the log file
(unlikely — every actual diagnostic this session reads Supabase
directly, never the log).

## Not done

Both scoped only, per instruction. Neither touches anything already
shipped this session (the Tier 1/2 capture, macro filter, Phase 3a) —
these are two independent infra fixes discovered via the health-check
thread, not corrections to anything built.

## Build notes (2026-09-01, same day)

Built exactly as scoped, both issues.

### Issue 1 — startup catch-up

- **`supabase_client.fetch_max_computed_at(table)`** (new): single-row
  `select=computed_at&order=computed_at.desc&limit=1` read, best-effort
  (`None` on any failure/empty table).
- **`orchestrator._is_stale(last, threshold, now=None)`** (new, pure
  function): missing value is stale; strictly-greater-than-threshold age
  is stale; exactly-at-threshold is NOT stale (boundary chosen so a
  just-completed run right at the edge doesn't get needlessly re-fired).
- **`MACRO_REGIME_STALE_AFTER = timedelta(hours=20)`,
  `BRACKET_REACHABILITY_STALE_AFTER = timedelta(hours=6.5*24)`** — module
  constants, both intentionally shorter than their full cycle.
- **`SweepOrchestrator._catch_up_stale_jobs()`** (new): checks both
  tables via `fetch_max_computed_at`, fires the corresponding job
  immediately (`asyncio.create_task`) if stale. Called once from
  `start()`, right after `self._scheduler.start()`.
- **Tests** (`tests/test_scheduler_catchup.py`, 9 new): missing-value,
  recent-value, exactly-at-threshold, just-past-threshold, real-clock
  default, and threshold-sanity assertions (both constants shorter than
  their full cycle). All pure, no mocking needed.
- **Real-data verification**: ran `fetch_max_computed_at` +  `_is_stale`
  against live Supabase — correctly read `nt_macro_regime`'s
  2026-08-29 timestamp and `nt_bracket_reachability`'s 2026-08-26
  timestamp, correctly flagged both `stale=True`, exactly matching the
  health-check findings that motivated this fix. Confirms the catch-up
  will actually fire on the next restart, not just that the code
  compiles.

### Issue 2 — log sanitization

- **`logging.getLogger("httpx").setLevel(logging.WARNING)`** added to
  `__main__.py`'s `main()`, right after the existing `basicConfig` call
  — the actual entry point the live orchestrator process runs (`python
  -m regime_platform.sweep_orchestrator`), not any of the standalone
  diagnostic scripts' own `if __name__ == "__main__":` blocks (low
  volume, not worth touching for this).
- **REJECTED gate-details dump** split into two calls: a short
  `logger.info` summary (`REJECTED — N gate(s) failed: [...]`, kept at
  INFO so basic visibility survives) and the full per-gate breakdown
  moved to `logger.debug`.
- **"markov diag" line removed** (`orchestrator.py`, the block that
  logged `current_state`/`row`/`p_up`/`p_dn` on every tick) — its own
  2026-08-22 comment said to remove it once the mechanism was confirmed;
  Tier 1/2 confirmed it this session. The underlying `p_up`/`p_dn`
  computation itself was kept (still feeds `nt_sweep_result`'s
  backward-compat columns) — only the logging calls were removed.

**Verified**: full suite 668 passed, 21 skipped (pre-existing/unrelated),
0 failed (up from 659). Byte-compiled all four touched files
(`orchestrator.py`, `supabase_client.py`, `__main__.py`, plus the test
file).

**Not done**: no change to `on_start.sh`'s truncation mechanism itself
(out of scope, per the original scoping) and no measurement yet of the
actual log-volume reduction post-deploy — that needs a restart + a few
hours of real ticks to compare against the 35.4%-httpx baseline this
was scoped from.
