# 2026-08-25 — Redis deprecation Phase 0: unblock production

**Scope:** `regime_platform/sweep_orchestrator/orchestrator.py`,
`regime_platform/sweep_orchestrator/heavy_sweep.py`,
`regime_platform/sweep_orchestrator/discord_delivery.py`. Phase 0 of the
full Redis deprecation plan (`.claude/plans/goofy-rolling-swing.md`, Part
3) — Redis is being retired entirely ("no longer a service we will use").
This phase specifically unblocks production, which has been fully idle
since the Redis connection started failing (~05:07 UTC 2026-08-25).

**Status:** Built, tested, full suite unaffected. Not yet deployed.

---

## Problem

`SweepOrchestrator.start()` treated a failed Redis connection as fatal:
```python
self._redis = await redis_pool.get_client()
if not self._redis:
    logger.error("Redis not available — sweep orchestrator cannot start")
    return
```
The process stayed alive and `/health` reported OK, but the scheduler
never registered — no light sweep, no heavy sweep, no EOD correction, no
resolver. Confirmed live across two consecutive restarts.

Three parallel Explore agents mapped every Redis touchpoint across the
repo beforehand (`regime_platform/`, `renko/`, and the rest of the app) to
scope the full deprecation properly rather than patch around the
immediate symptom. Two decisions were confirmed with the user before
finalizing the plan: the proxy service is also being retired (so
`stream_splitter.py`'s per-plan signal fan-out needs no replacement), and
`renko/`'s own live tick path is dead (sweep orchestrator is the sole live
signal source).

## Fix

1. **Removed the hard gate** — `start()` no longer touches Redis at all.
2. **Replaced the per-symbol distributed lock with an in-process one.**
   The `lock:sweep:{symbol}` Redis SET NX/DELETE pattern
   ("Layer 1, same as edge functions") guarded against light sweep and
   heavy sweep hitting the same symbol concurrently on independent cron
   schedules — a genuine race (APScheduler's `max_instances=1` doesn't
   prevent it, since they're different jobs), just one that no longer
   needs to be *distributed* since the deployment is a single process.
   Replaced with `self._sweep_locks: dict[str, asyncio.Lock]`, created
   lazily per symbol.
3. **Deleted the `signal.raw.noble_trader` PUBLISH+XADD** in both
   `orchestrator.py` and `heavy_sweep.py`'s `_push_heavy_signal` — the
   proxy/StreamSplitter consumer is retired, so this has no replacement to
   build. Kept the content-hash dedup logic that lived alongside it (still
   valuable — prevents re-sending an unchanged signal after cooldown
   expires — independent of where the signal ends up).
4. **Deleted the already-commented-out DEPRECATED heartbeat/config HSET
   block** (`trading:config:{symbol}` / `trading:heartbeat:{symbol}`) —
   dead code preserved "for reference" that no longer needs preserving;
   git history is the reference.
5. **Deleted the TimesFM Redis cache** (`timesfm:cached:{symbol}`) in
   `heavy_sweep.py` entirely, including a real latent bug found during
   scoping: one of the two cache-write call sites had no `self._redis is
   not None` guard and no try/except, unlike the read and the other write
   site — an inconsistency, not just Redis cruft. Recomputing TimesFM was
   already the fallback on a cache miss, so this is a pure simplification.
6. **Simplified `discord_delivery.py`** — removed the `redis_delivery_active`
   parameter and the double-delivery guard built around it. Discord is now
   the only delivery path, so there's nothing left to guard against
   duplicating.
7. **Deleted the debug-channel publish** (`signal.debug.noble_trader`) and
   its consumer script (`scripts/debug_signal_subscriber.py`) — dev
   tooling for a channel that no longer exists. Also removed the now-dead
   `DEBUG_SIGNALS` flag and its log line.
8. Cleaned up stale comments throughout both files that described the
   removed Redis mechanisms as if still live (module docstrings, the
   `HEAVY_SWEEP_MAX_CONCURRENCY` rationale, qualification-gate comments).

## Verification

Full suite: 588 passed, 6 failed, 2 errors (pre-existing — `slowapi`/
missing-dependency import errors, confirmed identical on `main` via `git
stash` comparison), 12 skipped. Zero new regressions.

Test changes:
- `tests/test_discord_direct_delivery.py` rewritten — removed all
  `redis_delivery_active` scenarios (the mechanism no longer exists),
  kept every still-relevant case (enabled/disabled toggle, neutral-signal
  skip, exception safety, heavy-sweep dispatch).
- `tests/test_heavy_sweep_resilience.py` — removed
  `test_timesfm_cache_read_failure_degrades_to_miss` and
  `test_timesfm_cache_write_failure_is_best_effort`. Both had gone
  **vacuous**: they set up an `_ExplodingRedis` mock that the real code no
  longer calls at all, so they were passing because nothing touched the
  mock, not because error-handling worked. Deleting a test that passes for
  the wrong reason is not a loss of coverage — it was already testing
  nothing. Kept the 3 tests that still exercise live behavior
  (concurrency cap, group-level cap enforcement, no-combo failure trace).
- `tests/test_redis_publish_toggle.py` deleted outright — tested the
  Redis publish path directly, which no longer exists. The plan originally
  slated this for Phase 4, but since the underlying code was removed in
  this phase, deleting the test alongside it (not leaving it broken until
  later) matches this session's "delete cleanly, don't leave dormant"
  discipline.

Along the way, installed `pytest-asyncio`, `scipy`, and `hmmlearn` locally
(all declared dependencies in `pyproject.toml`, just not actually
installed in this Windows checkout) to validate tests directly rather
than trust file-pattern-matching — this also unblocked ~250 previously
import-erroring tests across the suite that had nothing to do with this
change, revealing the true local baseline.

## Deployment status

Not yet deployed. Live verification plan: confirm via SSH that the
scheduler actually registers its jobs (the "Scheduled: ..."/"Added job..."
log lines this session has used throughout as the standard startup-health
check) — this should now succeed even though Redis is still failing,
which is itself the acceptance test for this whole phase.

## Not done in this pass

Phases 1-4 of the full Redis deprecation (renko/'s Redis machinery,
main.py's Redis-dependent lifespan services — stream_splitter,
signal_aggregator, external_webhook_service, backtest_cache,
redis_persistence — operational services' Redis L1 layer, and the final
infra/dependency/docs cleanup) remain scoped in the plan but not started.
Explicit, known consequence flagged for Phase 2: deleting
`signal_aggregator.py`/`external_webhook_service.py` will drop Telegram
and generic-webhook signal forwarding (both were pub/sub consumers of the
now-deleted channel, so they're already inert, just not yet removed).
