# 2026-08-25 — Redis deprecation Phase 2: remove main.py's Redis-dependent lifespan services

**Scope:** `main.py`, delete `regime_platform/services/stream_splitter.py`,
`signal_aggregator.py`, `external_webhook_service.py`, `backtest_cache.py`,
`redis_persistence.py`. Also touches `regime_platform/core/settings.py`,
`regime_platform/models/schemas.py`, `regime_platform/routers/backtest.py`,
`regime_platform/services/stream_session.py`, `renko/router.py` (a stale
comment), and deletes `test_signal_notifications.py` +
`tests/test_backtest_cache.py`. Phase 2 of the full Redis deprecation plan
(`.claude/plans/goofy-rolling-swing.md`, Part 3) — follows Phase 0
(orchestrator) and Phase 1 (renko/), both already live.

**Status:** Built, tested (611 passed, 0 failed), committed (`58dc92c`),
pushed to `origin/main`. Awaiting Studio restart + live verification.

---

> **Priority**: P3 · **Task**: Redis deprecation Phase 2: remove main.py's
> Redis-dependent lifespan services (StreamSplitter, SignalAggregator,
> ExternalWebhookService, BacktestCache, RedisPersistence) ·
> **Status**: resolved (confirmed "already live" by Phase 3/4 worklogs) ·
> **Date**: 2026-08-25

## Problem

Five services wired into `main.py`'s FastAPI lifespan all depended on
Redis, and all five were either fully dead (their only consumer already
removed in Phase 0) or safely droppable (pure performance caches with
zero data-loss risk on removal):

- **`stream_splitter.py`** — republished `signal.raw.noble_trader` to
  per-plan streams (`signals:signal_scout`, `signals:precision_pro`) for
  the proxy. The proxy is retired (confirmed earlier this session), so
  this had no consumer left.
- **`signal_aggregator.py`** / **`external_webhook_service.py`** — both
  were pub/sub consumers of `signal.raw.noble_trader`, which Phase 0
  already deleted the sole producer of. Already fully inert before this
  phase; this just removes the dead code.
- **`backtest_cache.py`** — a pure Redis-backed 1h-TTL cache for
  `/backtest/run`. No data-loss risk: a cache miss was already the
  fallback behavior (recompute), this just makes every call a "miss."
- **`redis_persistence.py`** — gave `StreamSession` (the `/stream/ws/*`
  and `/stream/sse/*` WebSocket/SSE engine) restart-survival for its
  price buffer via Redis Streams. `StreamSession` itself is fully
  in-memory otherwise.

## Fix

1. **Deleted `stream_splitter.py`** and its lifespan startup block in
   `main.py` (the `NOBLE_TRADER_STREAM_SPLIT_ENABLED`-gated
   `get_stream_splitter()` call). No shutdown wiring existed for it
   despite the class having a `stop()` method — moot now, the whole file
   is gone.
2. **Deleted `signal_aggregator.py` and `external_webhook_service.py`**
   and both their startup and shutdown wiring in `main.py`. **Explicit,
   known consequence**: this drops Telegram and generic-webhook signal
   forwarding. Both were pub/sub consumers of the now-dead
   `signal.raw.noble_trader` channel, so they were already producing zero
   real notifications — this removal has no *behavioral* effect beyond
   removing log noise (`"Signal Aggregator start failed: ..."` /
   `"External Webhook Service start failed: ..."` on every boot, visible
   in this session's own log tails). Discord is unaffected —
   `discord_delivery.py` (Phase 0) is already a direct-send path
   independent of this.
3. **Deleted `backtest_cache.py`** and its three call sites in
   `regime_platform/routers/backtest.py`'s `/backtest/run` (cache-check
   before compute, cache-store after compute, and the import). Also
   deleted `tests/test_backtest_cache.py` outright — it tested the
   `BacktestCache` service's Redis-mocked behavior directly, which no
   longer exists. One sub-class in that file (`TestCachedField`) only
   checked that the `cached` Pydantic field defaults to `False` and
   accepts `True` — trivial schema behavior, not worth preserving in a
   new file. Kept the `cached: bool` field on `BacktestResponse` itself
   (for API stability — always `False` now) and updated its description
   accordingly. Also updated a stale comment in `renko/router.py`
   referencing `test_backtest_cache.py::TestCachedField` in the
   DEPRECATED-aliases block.
4. **Deleted `redis_persistence.py`** and its two integration points in
   `regime_platform/services/stream_session.py` (`persistence.append_price()`
   calls in `seed()` and `tick()`, plus the import) and `main.py`'s
   startup restore-loop (hydrate `StreamSession`s from Redis on boot) and
   shutdown `persistence.close()` call. **Checked before deleting**: grepped
   the live Studio's `main.log` for `/ws/`, `/sse/`, `/stream/` traffic —
   zero matches — confirming the WebSocket/SSE routes this persistence
   layer served have no observed live traffic, consistent with the plan's
   assumption that this was pure unused restart-survival plumbing.
5. **Settings cleanup** (`regime_platform/core/settings.py`): removed
   `NOBLE_TRADER_STREAM_SPLIT_ENABLED` (only reader was the deleted
   `main.py` block), `EXTERNAL_SIGNAL_WEBHOOK_URL` (only reader was the
   deleted `external_webhook_service.py`), `NOBLE_TRADER_REDIS_PUBLISH_ENABLED`
   (found to already be fully orphaned — its actual gate-check code was
   removed in Phase 0 but the setting itself was left behind; caught by
   grep during this phase, not a Phase 0 regression since nothing reads
   it), and the `signal_aggregator_configured`/`external_webhook_configured`
   properties (both unused outside their own definitions). Removed the
   two related `model_post_init` startup warnings about Redis/webhook
   configuration that referenced now-deleted services. Kept
   `NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID`/`NOBLE_TRADER_PLAN_PRECISION_PRO_ID`
   — confirmed still actively used by `notifications/discord.py` and
   `realtime_publisher.py` for an independent Discord plan-routing
   mechanism, unrelated to StreamSplitter.
6. **Deleted `test_signal_notifications.py`** (repo root) — a standalone
   manual dev script (not under `tests/`, not collected by pytest per
   `pyproject.toml`'s `testpaths = ["tests"]`) that directly imported
   `get_signal_aggregator`/`get_external_webhook_service`. Left broken by
   this phase's deletions; removed alongside the code it exercised rather
   than left dangling.

**Not touched (explicitly out of scope, already known dead)**: `main_v3.py`
and `main_v4.py` still import `redis_persistence`/`auth_router` — both are
confirmed-unused entrypoints already slated for wholesale deletion in
Phase 4. Editing them now would be wasted work.

## Verification

Full suite: **611 passed, 21 skipped, 0 failed** (down from Phase 1's 637
passed — the 26-test delta is `tests/test_backtest_cache.py`'s full
removal, accounted for exactly). `python -c "import main"` smoke-tested
locally — app constructs cleanly, only the expected pre-existing
auth-config warnings appear (Clerk/JWT/API_KEYS unconfigured — unrelated
to this phase).

## Deployment status

Committed as `58dc92c`, pushed to `origin/main`. Not yet verified live —
next step is the user restarting the Studio and confirming via SSH that
startup logs no longer show `"Signal Aggregator start failed"` /
`"External Webhook Service start failed"` / `"StreamSplitter start failed"`
(all three should simply be absent now, not silently succeeding), and
that `/backtest/run` and a light sweep cycle both still complete cleanly.

## Not done in this pass

Phase 3 (operational services — `kill_switch.py`, `mode_manager.py`,
`reconciler.py` — migrate their Redis L1 layer to Supabase-only; this also
fixes a real pre-existing bug where `/mode/request`/`/mode/confirm` raise
a hard `RuntimeError` with no fallback when Redis is absent) and Phase 4
(delete `redis_pool.py`, remove remaining Redis settings, remove the
`redis` package dependency, delete `main_v3.py`/`main_v4.py` and other
orphaned dev scripts, doc cleanup) remain scoped in the plan but not
started.
