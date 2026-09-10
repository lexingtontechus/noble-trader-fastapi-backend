# 2026-08-25 — Redis deprecation Phase 1: remove renko/'s Redis machinery

**Scope:** `renko/redis_cache.py` (deleted), `renko/trading_heartbeat.py`
(deleted), `renko/pipeline.py`, `renko/router.py`, `main.py`,
`renko/README.md`. Also fixes a dangling import in
`regime_platform/sweep_orchestrator/orchestrator.py` caught by the test
suite. Phase 1 of the full Redis deprecation plan
(`.claude/plans/goofy-rolling-swing.md`, Part 3) — follows Phase 0, which
unblocked the sweep orchestrator's own Redis dependency
(`worklog/20260825_REDIS_DEPRECATION_PHASE0.md`).

**Status:** Built, tested (full suite green), committed
(`191cc55`), pushed to `origin/main`. Awaiting Studio restart + live
verification.

---

> **Priority**: P3 · **Task**: Redis deprecation Phase 1: remove renko/'s
> own dead Redis machinery (L1 cache, heartbeat, config-cache, publish) ·
> **Status**: resolved (confirmed "already live" by Phase 2/3/4 worklogs) ·
> **Date**: 2026-08-25

## Problem

`renko/`'s live tick path (`/renko/tick`, `/renko/tick/batch`) was
confirmed dead earlier in this session — the sweep orchestrator is now the
sole live signal source, and `renko/` is backtest/grid-search machinery
only (used by `heavy_sweep.py`'s grid search). That left `renko/`'s own
separate Redis publish/heartbeat/config-cache machinery as dead weight,
not a Redis call that needed swapping for something else:

- `renko/redis_cache.py` — an L1 snapshot cache duplicating
  `nt_renko_snapshot` (Supabase), which was already the durable store.
- `renko/trading_heartbeat.py` — wrote `trading:heartbeat:{symbol}` /
  `trading:config:{symbol}`; both write call sites were already
  commented out with dated "v8.0 REMOVED" comments before this session
  started, so the read paths were already permanent no-ops falling
  through to Supabase-backed config.
- `renko/pipeline.py`'s `_publish_qualified_signal()` — published to the
  `signal.raw.noble_trader` channel, which Phase 0 already deleted the
  sole producer/consumer chain for on the orchestrator side.
- `renko/pipeline.py`'s `_fetch_prediction_data_async()` — a best-effort
  read of `regime:symbol:{symbol}:config`, feeding a `prediction_data`
  param that defaults to `None` anyway.

Also, `renko/pipeline.py:28` had an unguarded `import redis.asyncio as
redis` — a packaging landmine that would break `renko/__init__`,
`router.py`, `main.py`, and both renko test files at import time the
moment the `redis` package is removed in Phase 4, regardless of
`REDIS_URL` being set.

## Fix

1. **Deleted `renko/redis_cache.py` outright.** Removed call sites: the
   save trigger and dual-write in `_save_pipeline_snapshot_confirmed`,
   the Tier-1 restore branch in `_restore_pipeline_snapshot` (now goes
   straight to Supabase), the "Test Redis L1 cache" health-check block,
   and the shutdown close in `main.py`.
2. **Deleted `renko/trading_heartbeat.py` outright.** Removed the
   `trading:config:{symbol}` cache-read fallback in
   `_get_pipeline_async()` (now calls `RenkoConfig.from_runtime_config()`
   unconditionally), the `_on_config_changed()` invalidation call, and 5
   live `trading_heartbeat.update(...)` call sites in the warmup
   endpoint. **Not a straight deletion for the two heartbeat endpoints** —
   `/renko/heartbeat` and `/renko/heartbeat/all` had no other data source,
   so they'd have started returning empty data the moment the module was
   gone. Rebuilt both from the in-memory `_pipelines` dict via a new
   `_pipeline_heartbeat()` helper that derives a heartbeat-shaped response
   from a pipeline's existing `readiness` property — same shape the
   Redis-backed version returned, no consumer-facing change.
3. **`renko/pipeline.py`**: removed the Redis client construction, the
   unguarded `import redis.asyncio as redis`, `_publish_qualified_signal()`
   and its call site, `_fetch_prediction_data()` (sync, dead — always
   returned `None`) and `_fetch_prediction_data_async()`, and the now-dead
   `close()` method (confirmed zero callers anywhere in the repo). Dropped
   the `prediction_data` computation at the `TradeJournal.create_record()`
   call site (the param already defaults to `None`). Removed now-unused
   `settings`, `asyncio`, and `json` imports that only existed to support
   the deleted code.
4. **Caught by the test suite, not by inspection**: `orchestrator.py:37`
   had `from renko.trading_heartbeat import CONFIG_TTL` — an import of a
   constant that was never actually referenced anywhere else in the file.
   Running the full suite after this phase's changes turned this into a
   hard `ModuleNotFoundError`, which is exactly the kind of cross-module
   dangling reference that static grepping inside `renko/` alone wouldn't
   have caught. Deleted the import.
5. **`renko/README.md`**: rewrote the Persistence/Data-Flow sections to
   describe Supabase as the only store (also fixed a stale table-name typo,
   `ta_renko_snapshot` → `nt_renko_snapshot`, discovered while editing).
   The "True Signal Gateway (v8.0)" section described an entire Redis
   pub/sub architecture (`agent:{symbol}`, `signal.raw.noble_trader`,
   heartbeat/config keys) that depended on the orchestrator-side Redis
   writes Phase 0 already removed — marked the whole section historical
   rather than rewriting it line-by-line, since none of its mechanism
   description is accurate anymore; kept the "What Changed" changelogs
   since they're a historical record of past architecture evolution, not
   a claim about current behavior.

## Verification

Ran the renko-specific tests first (`tests/test_renko_backtest.py`,
`tests/test_renko_snapshot_capture.py`, `tests/test_backtest.py`) — 64
passed, 11 skipped. Then the full suite: **637 passed, 23 skipped, 0
failed** (up from Phase 0's 588 passed / 6 failed / 2 errors — those 6
failures/2 errors were pre-existing missing-dependency import errors,
`slowapi` among them, installed locally this pass to get a clean
baseline rather than carry them forward unexamined).

No test files needed rewriting this phase (unlike Phase 0) — the deleted
Redis machinery in `renko/` had no direct test coverage exercising the
Redis calls themselves; `renko/tests.py`/`test_renko_backtest.py` only
exercised `RenkoPipeline` behavior, which the import-chain fix (`redis`
package no longer required) left unaffected.

## Deployment status

Committed as `191cc55`, pushed to `origin/main`. Not yet verified live —
next step is the user restarting the Studio and confirming via SSH that
`/renko/heartbeat` and `/renko/heartbeat/all` still return sensible data
from the in-memory pipeline path, and that a `/renko` backtest/grid-search
call (used by `heavy_sweep.py`) still completes cleanly.

## Not done in this pass

Phases 2-4 of the full Redis deprecation (main.py's Redis-dependent
lifespan services — `stream_splitter.py`, `signal_aggregator.py`,
`external_webhook_service.py`, `backtest_cache.py`,
`redis_persistence.py` — operational services' Redis L1 layer, and the
final infra/dependency/docs cleanup) remain scoped in the plan but not
started. `tests/test_backtest_cache.py`'s Redis-mock tests are untouched
by this phase — that file is explicitly Phase 2 scope.
