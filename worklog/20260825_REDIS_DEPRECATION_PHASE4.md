# 2026-08-25 — Redis deprecation Phase 4: infra + dependency + docs cleanup (final phase)

**Scope:** `regime_platform/services/redis_pool.py` (deleted),
`regime_platform/core/settings.py`, `requirements.txt`, `pyproject.toml`,
`uv.lock`, `render.yaml`, `README.md`, `AGENTS.md`, `.env.example`,
`docs/Setup/deployment.md`, both `docs/openapi.yaml` copies, several
stale docstrings/comments across `renko/router.py`,
`regime_platform/services/execution_deprecation.py`,
`regime_platform/core/tenant.py`, `regime_platform/services/registry.py`,
`regime_platform/middleware/rate_limit.py`,
`regime_platform/sweep_orchestrator/__init__.py` and `__main__.py`,
`supabase_client.py`, plus deletion of a dozen orphaned dev
scripts/entrypoints. Final phase of the full Redis deprecation plan
(`.claude/plans/goofy-rolling-swing.md`, Part 3) — follows Phase 0
(orchestrator), Phase 1 (renko/), Phase 2 (main.py's lifespan services),
Phase 3 (operational services), all already live.

**Status:** Built, tested (611 passed, 0 failed — including a run with
the `redis` package fully uninstalled locally), committed (`96c2f46`),
pushed to `origin/main`. Awaiting Studio restart + live verification.

---

## Problem

After Phases 0-3, every real Redis *consumer* was gone, but the
infrastructure, dependency declarations, orphaned dev tooling, and
documentation hadn't caught up:

- `regime_platform/services/redis_pool.py` — the shared connection-pool
  wrapper every prior phase's `_get_client()` methods called — had zero
  remaining consumers except one leftover shutdown-side
  `redis_pool.close()` call in `main.py`.
- `redis>=5.0.0` was still declared in `requirements.txt`/`pyproject.toml`
  (and locked in `uv.lock`) despite nothing importing it anymore.
- A dozen root-level and `scripts/` dev tools from the Redis-signal-flow
  era (manual test scripts, simulators, a Node.js listener) were
  confirmed-dead weight, plus `main_v3.py`/`main_v4.py` (unused
  alternate entrypoints already flagged as dead in earlier phases'
  worklogs).
- Settings still declared `REDIS_URL`, `UPSTASH_REDIS_REST_URL`,
  `UPSTASH_REDIS_REST_TOKEN`, `redis_url_effective`, and
  `DISCORD_WEBHOOK_DEBUG` (the last one's only mechanism — `DEBUG_SIGNALS`
  + the `signal.debug.noble_trader` channel — was already removed in
  Phase 0, leaving the setting silently orphaned until this pass's grep
  caught it).
- `regime_platform/sweep_orchestrator/__main__.py` still ran a standalone
  Redis PING diagnostic at startup — this was the exact source of the
  two-traceback noise this session observed in live log tails after
  Phase 1's Studio restart (flagged then as "non-fatal, candidate for
  Phase 4 cleanup").
- README.md, AGENTS.md, `.env.example`, `docs/Setup/deployment.md`, and
  both `openapi.yaml` copies all still described the fully-dead
  Redis Stream → proxy → agent signal-fan-out architecture, a
  `redis_l1_cache` health-check field and Redis L1 dual-write snapshot
  behavior that Phase 1 had already removed from the actual code, and a
  `REDIS_URL` config var with nothing left to read it.

## Fix

1. **Deleted `redis_pool.py`** and its one remaining consumer (`main.py`'s
   shutdown-side `redis_pool.close()` block) — confirmed via grep this
   was the only reference left anywhere in the codebase.
2. **Removed dead settings**: `REDIS_URL`, `UPSTASH_REDIS_REST_URL`,
   `UPSTASH_REDIS_REST_TOKEN`, `redis_url_effective` (all orphaned once
   `redis_pool.py` was gone), and `DISCORD_WEBHOOK_DEBUG` (orphaned since
   Phase 0, caught here).
3. **Removed the `redis` package dependency** from `requirements.txt` and
   `pyproject.toml`, regenerated `uv.lock` via `uv lock` (also dropped
   `async-timeout`, `redis`'s sole transitive dependency), removed
   `REDIS_URL` provisioning from `render.yaml`.
4. **Deleted orphaned dev scripts/entrypoints** confirmed to have zero
   real callers (checked via grep for imports before deleting each):
   `test_redis.py`, `test_redis_signal_generation.py`,
   `redis_signal_generation_results.json`, `stream_spy_ticks.py`,
   `test_signal_flow_comparison.py`, `test_discord_delivery.py`,
   `scripts/qualification_simulator.py`, `scripts/test_signal_stream.py`,
   `scripts/timesfm_gate_simulator.py`, `scripts/agent_listener.mjs`,
   `main_v3.py`, `main_v4.py`.
5. **Removed the standalone Redis PING diagnostic** from
   `sweep_orchestrator/__main__.py` — this eliminates the traceback noise
   observed in this session's own Phase 1 live-verification log tail; it
   was never load-bearing (non-fatal, orchestrator proceeded immediately
   after either way).
6. **Docs**: deleted `docs/Setup/REDIS_ACL_SETUP.md` and
   `REDIS_USAGE_OPTIMIZATION_ANALYSIS.md`. Rewrote README.md's
   "Signal pipeline (system topology)" section (was a detailed, wrong
   diagram of the Redis Stream → StreamSplitter → proxy → agent
   fan-out), its "Redis Persistence"/"Redis Signal Delivery — Push Model"
   sections (replaced with a short "Signal Delivery" section describing
   direct Discord delivery + Supabase-backed operational state), the
   dependency table, and several directory-tree entries referencing
   already-deleted files (`redis_persistence.py`, `tools/`, `test_v21.py`).
   Fixed AGENTS.md's signal-pipeline diagram and the proxy retirement
   note (referenced a setting, `NOBLE_TRADER_REDIS_PUBLISH_ENABLED`,
   removed back in Phase 2). Fixed `.env.example` (removed the dead
   `REDIS_URL`/`UPSTASH_*` block and the `DEBUG_SIGNALS` block, fixed a
   dangling reference to the now-deleted `scripts/timesfm_gate_simulator.py`).
   Fixed both `docs/openapi.yaml` copies (byte-identical — confirmed via
   `diff` before and after): removed the `redis` and `redis_l1_cache`
   response-schema fields from `/health` and `/renko/health`, and
   rewrote the `/renko/warmup`, `/renko/snapshot/save`,
   `/renko/snapshot/restore` descriptions to describe the actual
   Supabase-only implementation instead of a Redis L1 + Supabase L2
   dual-write that Phase 1 had already removed from the real code.
   Fixed `docs/Setup/deployment.md` (removed a dead `REDIS_URL` env-var
   table row and an "Extending" section suggesting Redis-backed session
   persistence as a future improvement — directly contradicts the
   platform's Redis retirement decision).
7. **Fixed several stale docstrings/comments** caught by a full
   case-insensitive repo grep, all describing Redis-era behavior that
   earlier phases had already removed from the actual code:
   `renko/router.py`'s `_save_snapshot`/`_save_pipeline_snapshot*`/
   `_get_pipeline_async` docstrings (still said "Redis L1 + Supabase L2"
   after Phase 1 deleted the Redis L1 write entirely — the function
   bodies were already Supabase-only, only the docstrings lagged),
   `execution_deprecation.py` and `settings.py`'s "computes signals,
   publish to Redis" claims (Phase 0 moved this to direct Discord
   delivery), `tenant.py`/`registry.py`/`rate_limit.py`'s Redis-specific
   language for mechanisms that are now generic or Supabase-backed, and
   `sweep_orchestrator/__init__.py`/`supabase_client.py`'s stale
   architecture-comparison bullets.

**Not touched, left as intentional historical record** (matching this
session's "git history is the audit trail" convention, applied here to
doc prose): worklogs, `docs/HMM/`, `docs/TimeFM/`, planning docs
(`docs/mse-implementation-plan.md`, `docs/SIGNAL_GEN_FLOW_COMPARISON.md`,
`docs/DISCORD_DELIVERY_FIX.md`), `docs/marketing-overview.md`, and
`index.html` — all describe past states/decisions rather than current
operator-facing instructions, same category as this repo's existing
worklog convention. `renko/router.py`'s one remaining "Redis usage
optimization" comment and `_pipeline_heartbeat`'s "the Redis heartbeat
store is gone" note are accurate historical context, kept as-is.

## Verification

Full suite: **611 passed, 21 skipped, 0 failed** — unchanged from Phase 3
(no test files needed changes this phase; the removed code had no
direct test coverage of its own).

**Strongest verification of this whole four-phase effort**: ran
`pip uninstall redis async-timeout` locally, then re-ran
`python -c "import main"` (succeeded cleanly) and the full test suite
again (still 611 passed, 0 failed) with the package physically absent.
This is direct proof — not an assumption from grep — that nothing in the
codebase still depends on `redis` at import time or at runtime, closing
out the entire Redis retirement.

## Deployment status

Committed as `96c2f46`, pushed to `origin/main`. Not yet verified live —
next step is the user restarting the Studio. Expected: a clean boot with
no `[sweep-orch] Redis PING: FAILED` traceback in `orchestrator.log` (the
diagnostic that produced it is now deleted), and no residual
`Redis pool: Failed to initialize` warning in `main.log` (the only
remaining consumer of `redis_pool` was removed this phase).

## Summary — full Redis retirement (Phases 0-4)

| Phase | Scope | Commit(s) |
|---|---|---|
| 0 | Unblock production — orchestrator hard-gate, distributed lock → in-process, signal publish, TimesFM cache | `f84205e` |
| 1 | renko/'s own Redis machinery (L1 cache, heartbeat, config-cache read, publish) | `191cc55`, `d60a486` |
| 2 | main.py's Redis-dependent lifespan services (StreamSplitter, SignalAggregator, ExternalWebhookService, BacktestCache, RedisPersistence) | `58dc92c`, `166c609` |
| 3 | Operational services → Supabase (mode_manager, kill_switch, reconciler) + a real bug fix (broken mode-switch confirmation) | `d410f6b`, `3e2878c` |
| 4 | Infra, dependency, orphaned scripts, docs | `96c2f46` |

Redis is no longer a dependency of this codebase in any form — no
package, no settings, no live code path, no accurate documentation
claiming otherwise.
