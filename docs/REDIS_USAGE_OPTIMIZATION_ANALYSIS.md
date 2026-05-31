# Redis Usage Optimization Analysis

**noble-trader v7.2.0 | Upstash Redis Infrastructure Audit**

Prepared: May 26, 2026 | Platform: FastAPI + Next.js + Upstash Redis + Supabase

---

## Executive Summary

Analysis of Upstash Redis command usage reveals approximately 60K commands/day for 2-3 active users, which is disproportionately high. The primary cause is the Kill Switch service polling Redis every 5 seconds, generating an estimated 69,120 commands/day on its own. Combined with per-tick XADD writes from the Regime price stream, the platform is spending 80-90% of its Redis budget on operations that could be eliminated or significantly reduced through architectural changes. This report identifies 7 specific optimization opportunities that would collectively reduce Redis command volume by 85-95%, from ~60K/day to ~3-9K/day, while improving system reliability and reducing infrastructure costs.

---

## 1. Current Redis Usage Overview

The noble-trader platform uses Upstash Redis as a multi-purpose infrastructure layer across three sub-projects: the FastAPI backend, the Next.js frontend BFF (Backend-For-Frontend), and the legacy agent backend. Redis serves six distinct functional roles: L1 caching for Renko pipeline snapshots, price stream persistence via Redis Streams, backtest result caching, kill switch state management with real-time polling, trading mode state persistence, and reconciliation timestamp tracking. The Upstash dashboard reports approximately 30,000 to 60,000 Redis commands per day across the us-west-1 region, which is strikingly high for a platform serving only 2-3 concurrent users.

A comprehensive audit identified 6 separate Redis client connections in the FastAPI backend alone, 19 unique key patterns, 12 distinct Redis commands in use, and critically, one background polling loop that runs every 5 seconds. The architecture follows a dual-write L1(cache)/L2(persistent) pattern for Renko snapshots, where data is written to Redis first and then to Supabase. While this pattern is architecturally sound for hot-path reads, the implementation has significant inefficiencies that inflate command counts well beyond what the workload requires.

### Key Metrics at a Glance

| Metric | Value | Context |
|--------|-------|---------|
| Daily Commands (observed) | 30K - 60K | 2-3 users |
| Kill Switch Polling (estimated) | 69,120/day | Every 5s cycle |
| Price Stream XADD (estimated) | 10K - 50K/day | Per-tick per symbol |
| Redis Client Connections | 6 separate | Should be 1 shared |
| Keys Without TTL | 7 patterns | Memory leak risk |
| KEYS Command in Production | 3 locations | O(N) blocking |
| Target After Optimization | 3K - 9K/day | 85-95% reduction |

*Table 1: Key Redis usage metrics for noble-trader v7.2.0*

---

## 2. Root Cause Analysis: Where Commands Are Spent

The command volume breakdown reveals that two services account for the vast majority of Redis operations. Understanding their mechanics is essential to designing effective optimizations.

### 2.1 Kill Switch 5-Second Polling Loop (Primary Offender)

The Kill Switch service implements a three-tier emergency halt system (global, user-level, symbol-level) that stores halt state in Redis for cross-process persistence. To provide sub-millisecond hot-path checks, it maintains an in-memory cache that is refreshed from Redis every 5 seconds via a background asyncio task. Each refresh cycle executes the following sequence of Redis commands: a PING to verify connectivity, a GET for the global halt key, a SCAN iteration over user halt keys with individual GET calls for each key found, and another SCAN iteration over symbol halt keys with individual GET calls for each.

At minimum (with zero active halts), each 5-second cycle consumes 4 Redis commands: PING, GET, SCAN (returns empty), and SCAN (returns empty). Over 24 hours, this produces 17,280 polling cycles at 4 commands each, totaling approximately 69,120 commands per day. When active halts exist, the cost increases further because each SCAN iteration returns keys that must each be retrieved with an additional GET command. This single service accounts for more than 100% of the observed daily command volume on some days, meaning other services effectively add to the overflow.

| Command | Per Cycle | Cycles/Day | Commands/Day | % of Total |
|---------|-----------|------------|--------------|------------|
| PING (health check) | 1 | 17,280 | 17,280 | 24.9% |
| GET (global halt) | 1 | 17,280 | 17,280 | 24.9% |
| SCAN (user halts) | 1 | 17,280 | 17,280 | 24.9% |
| SCAN (symbol halts) | 1 | 17,280 | 17,280 | 24.9% |
| GET per active halt key | varies | varies | 0 - 17,280+ | 0-25% |

*Table 2: Kill Switch polling command breakdown (minimum baseline, no active halts)*

### 2.2 Redis Persistence XADD per Tick (Secondary Offender)

The Redis Persistence service appends every incoming price tick to a Redis Stream using XADD. Each symbol gets its own stream key (`regime:prices:{symbol}`) with a MAXLEN of approximately 5,000 entries. When the Alpaca or Yahoo feed is actively streaming data, this generates one XADD command per tick per symbol. For a typical configuration with 2-3 active symbols receiving ticks at approximately 1 tick per second during market hours, this produces an estimated 2-3 XADD commands per second, accumulating to roughly 17,000-26,000 commands during a 6.5-hour trading session. During extended backtesting or warmup runs that process historical data in bulk, the rate can spike significantly higher, as the batch processing endpoint calls `append_price` for each tick in the dataset.

While XADD is an appropriate choice for time-series data storage in Redis Streams, the per-tick granularity is excessive for the persistence use case. The data is only ever consumed during session restoration (via XREVRANGE on startup), and the in-memory price buffer already holds the full window. Writing every individual tick to Redis is unnecessary when a batched approach would achieve the same persistence guarantee with dramatically fewer commands.

### 2.3 Other Redis Consumers (Minor)

| Service | Trigger | Cmds/Call | Est. Daily Impact |
|---------|---------|-----------|-------------------|
| Renko Snapshot Cache | Per save (fire-and-forget + confirmed) | 3-6 | ~5K-10K/day (Medium) |
| Backtest Result Cache | GET + SET per backtest request | 2 | ~2K-5K/day (Low) |
| Mode Manager | GET on cache miss, SET on change | 1-2 | ~100-500/day (Very Low) |
| Reconciler | 1 SET last_run every 15 minutes | 1 | ~96/day (Negligible) |
| Frontend BFF (Upstash) | GET/SET snapshot, price, regime | 2-4 | ~1K-3K/day (Low) |

*Table 3: Minor Redis consumers and their estimated command volumes*

---

## 3. Architectural Issues Identified

### 3.1 Six Separate Redis Connections

Each Redis-using service in the FastAPI backend creates its own independent Redis client connection via `redis.asyncio.from_url()`. This results in 6 separate TCP connections to the same Upstash Redis instance: RenkoRedisCache, RedisPersistence, BacktestCache, KillSwitchService, ModeManager, and ReconciliationService. Each connection consumes resources on both the application server and the Redis instance. In a containerized deployment on Render with limited resources, these connections compete for file descriptors and event loop capacity. A shared connection pool pattern would reduce this to a single multiplexed connection while maintaining the same throughput.

### 3.2 KEYS Command in Production Code

Three locations use the KEYS command, which is O(N) and blocks the Redis event loop while scanning all keys in the database. The `RenkoRedisCache.list_symbols()` method uses `KEYS renko:snapshot:*`, `RedisPersistence.list_persisted_symbols()` uses `KEYS regime:prices:*`, and `BacktestCache.get_stats()` uses `KEYS bt:renko:*` and `KEYS bt:hmm:*`. While these are called infrequently (health checks, startup, admin endpoints), the KEYS command is explicitly discouraged in production by Redis documentation because it degrades performance for all other clients while running. The SCAN command should be used instead, or a Redis SET of known keys should be maintained for O(1) lookups.

### 3.3 Keys Without TTL (Memory Leak Risk)

Seven key patterns have no TTL set, meaning they persist indefinitely in Redis memory until explicitly deleted. The most concerning is `regime:meta:{symbol}`, which stores session metadata for every symbol that has ever been tracked. If sessions are created and destroyed frequently (e.g., during backtesting), stale metadata accumulates without cleanup. The halt keys (`noble:halt:global`, `noble:halt:user:*`, `noble:halt:symbol:*`) are intentionally persistent, but lack any audit or cleanup mechanism for orphaned entries when users are deactivated or symbols are delisted. The `noble:trading:mode` key is also intentionally persistent, while `noble:reconciliation:last_run` is a small timestamp string with negligible memory impact. Adding TTLs to `regime:meta` keys (24 hours) and implementing a periodic cleanup job for stale halt keys would prevent unbounded memory growth.

### 3.4 Duplicate Renko Snapshot Writes

The Renko router contains two separate code paths that save pipeline snapshots to Redis: `_save_pipeline_snapshot()` which is a fire-and-forget background write, and `_save_pipeline_snapshot_confirmed()` which is an awaited confirmed write. Both call `renko_cache.save()` with the same snapshot data. When a code path executes both functions in sequence (which can happen during batch tick processing), the same gzip-compressed snapshot is written to Redis twice, doubling the command cost for that operation. The dual-write pattern should be consolidated into a single save operation that can be called either synchronously or as a background task depending on the criticality of the write.

---

## 4. Optimization Recommendations

The following recommendations are ordered by impact (highest command reduction first) and implementation complexity (simplest first within each tier).

### 4.1 Tier 1: High Impact (85-90% Command Reduction)

**Recommendation 1: Replace Kill Switch Polling with Event-Driven Architecture**

The single most impactful change is replacing the 5-second polling loop in KillSwitchService with a Redis pub/sub pattern. Instead of reading all halt state from Redis every 5 seconds, the service should subscribe to a Redis channel (e.g., `noble:halt:updates`) and receive push notifications when halt state changes. When `activate_halt()` or `deactivate_halt()` is called, the method would PUBLISH a message to the channel, and all subscribed processes would instantly update their in-memory cache. A conservative fallback polling interval of 60 seconds should be retained as a health check to detect missed pub/sub messages due to network partitions.

| Scenario | Commands/Day | % of Current |
|----------|-------------|--------------|
| Current: 5s polling | 69,120 | 100% |
| After: pub/sub + 60s fallback | 1,440 | 2% |
| Reduction | 67,680 | 98% |

*Table 4: Kill Switch optimization impact analysis*

Implementation approach: Modify `KillSwitchService._refresh_loop()` to subscribe to a Redis pub/sub channel instead of polling. Add a PUBLISH call in `activate_halt()` and `deactivate_halt()`. Keep a 60-second polling fallback for resilience. The in-memory cache remains the same, only the refresh mechanism changes. Estimated effort: 2-3 hours of development plus testing.

**Recommendation 2: Batch XADD Price Writes**

Instead of calling XADD on every individual tick, buffer price data in memory and flush to Redis in batches. A reasonable approach would be to accumulate ticks for 100 milliseconds or 10 ticks (whichever comes first), then write them all in a single Redis pipeline transaction. This reduces the per-tick command overhead from 1 XADD per tick to approximately 1 pipeline call per 10 ticks, achieving a 90% reduction in command count for the price persistence path.

| Scenario | Commands/Day | % of Current |
|----------|-------------|--------------|
| Current: per-tick XADD (2 symbols) | 17,280 - 25,920 | 100% |
| After: 10-tick batches | 1,728 - 2,592 | 10% |
| Reduction | 15,552 - 23,328 | 90% |

*Table 5: Price stream batching impact analysis*

### 4.2 Tier 2: Medium Impact (5-8% Additional Reduction)

**Recommendation 3: Consolidate Redis Connections**

Create a shared `RedisConnectionManager` singleton that all services use instead of creating their own `redis.asyncio.from_url()` connections. The manager would create a single connection pool with appropriate `max_connections` setting (e.g., 10), and each service would acquire clients from the pool. This reduces TCP connection overhead, simplifies connection lifecycle management, and ensures consistent timeout and retry configuration across all services. While this does not directly reduce command count, it reduces connection overhead on Upstash (which bills per connection in some plans) and improves the efficiency of remaining commands through connection reuse.

**Recommendation 4: Eliminate Duplicate Snapshot Saves**

Consolidate the two snapshot save paths in the Renko router into a single `save_snapshot()` method that accepts a parameter for whether the save should be awaited or fire-and-forget. Remove the separate `_save_pipeline_snapshot()` and `_save_pipeline_snapshot_confirmed()` functions and replace all call sites with the unified method. This eliminates approximately 3-6 redundant Redis commands per snapshot save operation, reducing Renko snapshot command volume by roughly 50%.

**Recommendation 5: Replace KEYS with SCAN or SET**

Replace the three KEYS command usages with either SCAN-based iteration (for `list_symbols` and `list_persisted_symbols`) or a Redis SET of known keys (for `get_stats`). The SET approach is particularly efficient for the backtest cache stats: maintain a SET `bt:known_keys` and SADD new keys when caching, SREM when deleting, and use SMEMBERS for stats. This provides O(1) membership checks and O(N) listing without blocking the Redis event loop.

### 4.3 Tier 3: Shift to Supabase (Cost Optimization)

Several categories of data currently stored in Redis are better suited to Supabase because they are read infrequently, have no sub-millisecond latency requirement, and would benefit from the durability and queryability of a relational database. Since the platform already has a Supabase instance with a `system_config` table containing 148 keys, extending it for these use cases is straightforward.

| Current Redis Key | Access Pattern | Recommended Migration Storage | Priority |
|-------------------|---------------|-------------------------------|----------|
| `noble:trading:mode` | Read on startup + cache in memory | Supabase `system_config` | High |
| `noble:reconciliation:last_run` | Write after reconciliation | Supabase `system_config` | High |
| `regime:meta:{symbol}` | Read on session creation | Supabase `session_config` | Medium |
| `noble:halt:*` | Write-through for audit trail | Keep in Redis + Supabase audit | Low* |
| `bt:{prefix}:{sha256}` | Already optimal for cache | Keep in Redis (1h TTL) | None |

*Table 6: Data migration recommendations from Redis to Supabase*

*\* Kill switch halt keys should remain in Redis for sub-millisecond reads in the hot path, but every activate/deactivate event should also write to a Supabase audit table for compliance and debugging. This is a write-through pattern, not a migration.*

### 4.4 Add TTLs to Orphaned Keys

Add TTLs to all keys that currently lack expiration. The `regime:meta:{symbol}` keys should receive a 24-hour TTL, which is generous compared to the 4-hour TTL already set on the corresponding snapshot data. The `noble:reconciliation:results` key (declared but never written) should be removed entirely. Halt keys are intentionally persistent but should have a cleanup cron that removes entries older than 30 days for user-level halts and 90 days for symbol-level halts. This prevents unbounded memory growth while preserving the persistent nature of these safety-critical keys.

| Key Pattern | Current TTL | Recommended TTL | Rationale |
|-------------|------------|-----------------|-----------|
| `regime:meta:{symbol}` | None | 24 hours | Prevents stale metadata accumulation |
| `noble:halt:user:{user_id}` | None | None (cleanup cron) | 30-day cleanup for orphaned entries |
| `noble:halt:symbol:{symbol}` | None | None (cleanup cron) | 90-day cleanup for delisted symbols |
| `noble:reconciliation:results` | None | Delete key entirely | Never written to; dead code |

*Table 7: TTL recommendations for orphaned Redis keys*

---

## 5. Projected Impact Summary

Combining all recommendations, the projected reduction in daily Redis commands is substantial. The table below shows the cumulative effect of implementing each tier of optimizations sequentially.

| Optimization Stage | Est. Commands/Day | % of Current | Key Change |
|--------------------|-------------------|--------------|------------|
| Current State | 60,000 | 100% | Baseline |
| Tier 1: Kill Switch pub/sub | 5,000 - 8,000 | 8-13% | 98% reduction in polling |
| Tier 1: + XADD batching | 3,000 - 6,000 | 5-10% | 90% reduction in stream writes |
| Tier 2: + Dedup + SCAN fix | 2,500 - 5,000 | 4-8% | 50% reduction in snapshot writes |
| Tier 3: + Supabase migration | 2,000 - 4,000 | 3-7% | Eliminates mode/recon keys |

*Table 8: Projected Redis command reduction by optimization stage*

The full optimization would reduce daily Redis commands from approximately 60,000 to 2,000-4,000, representing a 93-97% reduction. This would bring the platform well within the free tier of Upstash Redis (10K commands/day) or significantly reduce costs on paid plans. The two Tier 1 optimizations alone account for over 90% of the reduction and should be prioritized for immediate implementation.

---

## 6. Implementation Priority and Effort Estimates

| Priority | Recommendation | Tier | Impact | Effort | Expected Result |
|----------|---------------|------|--------|--------|-----------------|
| 1 | Kill Switch pub/sub | Tier 1 | High | 2-3 hours | 98% kill switch reduction |
| 2 | XADD batching | Tier 1 | High | 3-4 hours | 90% stream write reduction |
| 3 | Consolidate connections | Tier 2 | Medium | 4-6 hours | Resource optimization |
| 4 | Dedup snapshot saves | Tier 2 | Medium | 1-2 hours | 50% snapshot reduction |
| 5 | Replace KEYS with SCAN/SET | Tier 2 | Low | 2-3 hours | Remove blocking commands |
| 6 | Migrate mode/recon to Supabase | Tier 3 | Medium | 3-4 hours | Eliminate persistent keys |
| 7 | Add TTLs + cleanup cron | Tier 3 | Low | 1-2 hours | Prevent memory leaks |

*Table 9: Implementation priority and effort estimates*

---

## 7. Appendix: Complete Redis Key Inventory

The following table provides a complete inventory of all Redis key patterns currently in use across the noble-trader platform, including the service that owns each key, the current TTL setting, and the recommended action.

| Key Pattern | Service | Current TTL | Recommended Action |
|-------------|---------|-------------|-------------------|
| `renko:snapshot:{sym}:{bs}` | Renko Cache (FE+BE) | 4 hours | Keep (optimal) |
| `renko:snapshot:meta:{sym}` | Renko Cache (BE) | 4 hours | Keep (optimal) |
| `renko:price:{sym}` | Frontend BFF | 15 seconds | Keep (optimal) |
| `renko:regime:{sym}` | Frontend BFF | 5 minutes | Keep (optimal) |
| `renko:backtest:{sym}:{hash}` | Frontend BFF | 1 hour | Keep (optimal) |
| `bt:{prefix}:{sha256}` | Backtest Cache (BE) | 1 hour | Keep (optimal) |
| `regime:prices:{sym}` | Redis Persistence | None (MAXLEN) | Batch XADD writes |
| `regime:meta:{sym}` | Redis Persistence | None | Add 24h TTL |
| `noble:halt:global` | Kill Switch | None | Keep + Supabase audit |
| `noble:halt:user:{uid}` | Kill Switch | None | Keep + cleanup cron |
| `noble:halt:symbol:{sym}` | Kill Switch | None | Keep + cleanup cron |
| `noble:trading:mode` | Mode Manager | None | Migrate to Supabase |
| `noble:trading:mode_token:{tok}` | Mode Manager | 5 minutes | Keep (optimal) |
| `noble:reconciliation:last_run` | Reconciler | None | Migrate to Supabase |
| `noble:reconciliation:results` | Reconciler | None | Delete (never used) |

*Table 10: Complete Redis key inventory with recommended actions*
