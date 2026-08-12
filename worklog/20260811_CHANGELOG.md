## [9.2.0] - 2026-08-11

### Fixed — TDVA candle history order: prices were time-reversed (newest-first) in every sweep consumer

**Incident:** USDJPY signal showed Entry $162.8705 while the TradingView chart
(and the live TDVA quote) showed ~159. The price LEVEL was wrong — not a decimal
precision issue. Root cause traced with a live `/api/price/batch` call:

```
POST /api/price/batch  {"symbol":"OANDA:USDJPY","timeframe":"60","range":200}

history[0]  time=1786496400  close=159.34875   ← NEWEST (real current price)
history[1]  time=1786492800  close=159.2955    ← older
...
history[-1] time=1785434400  close=158.9595    ← OLDEST (~12.3 days back)
```

The TDVA API returns `history` in **descending time order (newest FIRST)** — the
docs (GET and POST `/api/price/batch`) show timestamps decreasing down the array.
`fetch_tradingview_batch()` copied the bars **in order without reversing**, while
its own docstring claimed `"ascending ts"`. Every downstream consumer assumes
ascending (`prices[-1]` = newest), so the whole engine was computing on a
**time-reversed** series:

- `current_price = float(prices[-1])` → the OLDEST close (~8-12 days stale) became
  the entry → SL/TP levels all wrong (162.87 instead of ~159)
- `total_return = np_prices[-1]/np_prices[0]` → inverted
- `returns = np.diff(np.log(np_prices))` → time-reversed log returns → HMM
  fit/predict saw the series backwards (volatility still detected → plausible but
  wrong LEVEL; trend/direction signal corrupted)
- TimesFM `last_price = price_samples[-1]` → stale input
- HMM quote fetcher `prev_closes[-1]` → stale log_return feature
- EOD resolver close-walk → TP/SL outcome resolution walked BACKWARD

**Fix (single point, root level):** `fetch_tradingview_batch()` now reverses each
symbol's price list before returning, so the documented ascending contract is
actually honored. All consumers (light sweep, heavy sweep, HMM, TimesFM, EOD
resolver) are fixed by this one change.

#### regime_platform/sweep_orchestrator/price_fetcher.py - MODIFIED
- `fetch_tradingview_batch()`: after parsing each item's `history` bars into a
  close-price list, the list is reversed to ascending order (oldest → newest)
  before being stored in `results[internal_sym]`. The docstring contract
  (`"ascending ts"`) is now true.
- Both bar shapes are covered (dict bars `{time, close}` and list bars
  `[time, o, h, l, c, v]`) because the reverse happens on the final `prices`
  list, after shape-specific parsing.

#### regime_platform/sweep_orchestrator/orchestrator.py - UNCHANGED
- No changes needed: `current_price = float(np_prices[-1])` (L1419),
  `total_return` (L1387), `returns` (L1345), TimesFM `price_samples[-1]`
  (L253), HMM `prev_closes[-1]` (L1170) all remain correct once the fetcher
  returns ascending data.

#### regime_platform/sweep_orchestrator/heavy_sweep.py - UNCHANGED
- Same — `current_price = float(prices[-1])` (L420) is correct with ascending
  input.

#### regime_platform/sweep_orchestrator/eod_resolver.py - UNCHANGED
- `resolve_signal_outcome()` close-walk is forward-in-time; correct with
  ascending closes.

#### tests - ADDED
- `tests/test_price_fetcher_order.py` — verifies `fetch_tradingview_batch`
  reverses descending TDVA history into ascending close lists for both dict-bar
  and list-bar response shapes, keeps result keys, and preserves intra-list
  values.

### Verified
- Live TDVA call before fix: USDJPY `history[0]=159.35`, `history[-1]=158.96`
  (descending, 200 bars). After fix via `scripts/verify_tdva_order.py`:
  `prices[0]=158.9595` (oldest), `prices[-1]=159.3430` (newest = live price,
  matches the TradingView chart ~159). The stale 162.87 entry level is gone.
- `tests/test_price_fetcher_order.py` — 4 new tests (dict bars, list bars,
  single bar, multi-symbol) PASS.
- Regression: `test_heavy_sweep_resilience.py`, `test_price_full_value.py`,
  `test_eod_resolver.py` — 37 total PASS in the combined run.

---

## [7.7.2] - 2026-08-11

### Changed — Plan-scoped Realtime broadcast channels + `realtime:portfolio` rename

Qualified signal broadcasts are now **plan-scoped** instead of one shared
`realtime:signals` channel, and the paper-validation channel was renamed so it
stops reading as a demo-trading feed.

#### regime_platform/sweep_orchestrator/realtime_publisher.py - MODIFIED
- Topics: `realtime:signals` → `realtime:signals.signal_scout` +
  `realtime:signals.precision_pro` (per plan); `realtime:paper` →
  `realtime:portfolio` (carries the SIMULATED validation book, not a demo
  account).
- `broadcast_signal(payload, plan_ids=None)` — publishes to the topic(s)
  matching the symbol's `nt_symbol.plan_ids` membership. Resolution uses the
  same env overrides as StreamSplitter
  (`NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID` / `NOBLE_TRADER_PLAN_PRECISION_PRO_ID`).
  **Fail-open** mirrors StreamSplitter: no configured UUIDs or unmatched
  plan_ids → publish to BOTH topics so no subscriber misses a signal
  (over-delivery benign, under-delivery not).
- All joins stay PUBLIC (anon Talaria clients must receive) — enforcement
  stays at subscribe (the plugin joins only its server-validated plan's
  topic). Broadcast channels are dynamic in Supabase — nothing to declare in
  the Realtime dashboard.

#### regime_platform/sweep_orchestrator/orchestrator.py - MODIFIED
- `_process_symbol` broadcast call passes `plan_ids=sym.get("plan_ids") or []`.

#### regime_platform/sweep_orchestrator/heavy_sweep.py - MODIFIED
- `_push_heavy_signal` broadcast call passes `plan_ids=sym.get("plan_ids") or []`.

#### tests/test_realtime_publisher.py - MODIFIED
- Updated to the new topics; added `TestPlanSignalTopics` (scout-only,
  pro-only, both, unrecognized → fail-open both, case/whitespace-insensitive,
  no-configured-UUIDs → fail-open). 23 tests; suite 348 passed / 23 skipped.

## [7.7.1] - 2026-08-10

### Fixed — Discord signal delivery decoupled from Redis publish gate (Option 2)

Incident: qualified sweep signals stopped reaching Discord when
NOBLE_TRADER_REDIS_PUBLISH_ENABLED=false (proxy retirement). The ONLY Discord
path for sweep signals was Redis signal.raw.noble_trader -> in-process
SignalAggregator -> Discord. With the Redis feed off the orchestrator writes
nothing to Redis, the aggregator receives nothing, and Discord goes silent
even though the webhooks are healthy.

Fix: sweep signals now go DIRECTLY to the Discord notifier from both sweep
paths, independent of the Redis gate, gated by a new toggle.

#### regime_platform/core/settings.py - MODIFIED
- Added NOBLE_TRADER_DISCORD_SIGNALS_ENABLED: bool = True - decoupled Discord
  toggle (default on, preserves current behavior). When Redis feed is ON the
  direct send is skipped (aggregator already delivers Discord - no duplicates).

#### regime_platform/sweep_orchestrator/discord_delivery.py - NEW FILE
- maybe_send_discord_signal(payload, *, source, redis_delivery_active) - direct
  Discord delivery mirroring SignalAggregator._route_to_discord HEARTBEAT
  buy/sell mapping (pattern="regime_trade", direction upper-cased, full-price
  entry/sl/tp, plan routing). Gated by NOBLE_TRADER_DISCORD_SIGNALS_ENABLED;
  skips when redis_delivery_active (avoids duplicate alerts). Never raises -
  all errors logged; a Discord hiccup cannot break the sweep.

#### regime_platform/sweep_orchestrator/orchestrator.py - MODIFIED
- _process_symbol: after the Redis publish block (where `qualified` is FINAL,
  post cooldown + content-dedup), qualified buy/sell signals dispatch a
  fire-and-forget direct Discord send via asyncio.create_task when
  NOBLE_TRADER_DISCORD_SIGNALS_ENABLED and Redis delivery is inactive.

#### regime_platform/sweep_orchestrator/heavy_sweep.py - MODIFIED
- _push_heavy_signal: same direct Discord dispatch (source="sweep-heavy") after
  the Redis block, with the same gating.

#### tests/test_discord_direct_delivery.py - NEW FILE
- 8 tests: settings default, helper sends with aggregator-identical kwargs,
  disabled toggle skips, redis-active skips (no duplicate), neutral skips,
  never-raises on webhook failure, heavy path dispatches when Redis off,
  heavy path does NOT duplicate when Redis on. Existing
  test_redis_publish_toggle.py regression intact.

## [7.7.0] - 2026-07-24

### Changed — Multivariate Gaussian HMM for `p_markov` + TA persistence
- Replaces the discrete 3-state `MarkovChain` as the live `p_markov` source with a 5-state multivariate Gaussian HMM (`regime_platform/services/hmm/`), trained live via Baum-Welch on a 9-feature observation vector (log_return + 3 microstructure + 5 TA). Emits the same 3x3 transition matrix contract (`{UP, DOWN, FLAT}`) and `current_state` string the EV Engine v4 expects — drop-in replacement.
- Persists the 5 TA values (ADX, AO, BB Power, MACD, RSI) per sweep tick in `nt_sweep_result` + `nt_regime_log` (migration 036) for post-hoc calibration analytics.
- Concurrent TVDA fetch via `asyncio.gather` for 20 symbols (microstructure `/api/quote` + TA `/api/ta`).
- Version metadata bumped to 7.7.0 across `pyproject.toml`, `main.py`, and `docs/openapi.yaml` (resolves prior 7.2.0 drift; see audit worklog H2).

## [7.2.0] - 2026-07-20

### Added — P5: Multi-tenant stream split + execution deprecation + Discord plan routing

P5 reorients noble-trader-fastapi-backend as a **sweep orchestrator only**. Trade execution is deprecated (the hermes agent owns it now), and signals are split into per-plan Redis streams so the proxy + agent can subscribe to a single plan-specific channel. Discord routing is extended to a 4-channel layout (Signal Scout / Precision Pro / system-status / trade-executions).

#### `regime_platform/core/settings.py` — MODIFIED
- Added 5 new env vars:
  - `NOBLE_TRADER_DISABLE_EXECUTION: bool = True` — feature flag gating all execution codepaths. Default ON after P5. Set `false` only for emergency rollback.
  - `NOBLE_TRADER_STREAM_SPLIT_ENABLED: bool = True` — enables the `StreamSplitter` background task.
  - `NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID: str = ""` — UUID of the Signal Scout plan (resolved from `nobletradingapp.plans.title = 'Signal Scout'`).
  - `NOBLE_TRADER_PLAN_PRECISION_PRO_ID: str = ""` — UUID of the Precision Pro plan.
  - `DISCORD_WEBHOOK_SIGNAL_SCOUT: str = ""` and `DISCORD_WEBHOOK_SIGNAL_PRECISION: str = ""` — per-plan Discord webhook URLs.
- The `# ── Alpaca ──` section is renamed to `# ── Alpaca (DATA FEED ONLY — execution deprecated) ──` and annotated to clarify ALPACA_API_KEY/SECRET are read-only data credentials now, not order-placement credentials.

#### `regime_platform/services/execution_deprecation.py` — NEW FILE
- Central gate `assert_execution_enabled(context=None)` that raises `ExecutionDisabledError` when `settings.NOBLE_TRADER_DISABLE_EXECUTION` is True.
- `execution_enabled()` convenience helper for callers that want to skip work without paying the exception cost.
- `ExecutionDisabledError` carries a `context` field for audit logging (e.g. `"AlpacaExecutor.__init__"`, `"kill_switch.cancel_all_orders"`).
- The error message is operator-actionable: explains the deprecation, points to the hermes agent, and tells operators how to re-enable in an emergency.

#### `renko/executor.py` — MODIFIED
- Module docstring rewritten with `.. deprecated:: P5 (2026-07-20)` directive.
- `AlpacaExecutor.__init__` now calls `assert_execution_enabled(context="AlpacaExecutor.__init__")` as its first statement. Constructing the executor raises immediately when the gate is active.
- File is retained for emergency rollback (`NOBLE_TRADER_DISABLE_EXECUTION=false`) and as reference for the hermes agent's new executor implementation.

#### `regime_platform/services/operational/executor_factory.py` — MODIFIED
- `ExecutorFactory.get_executor()` now calls `assert_execution_enabled(context="ExecutorFactory.get_executor")` — short-circuits before reaching the Alpaca import.
- `ExecutorFactory.get_alpaca_rest()` returns `None` when the gate is active (instead of constructing a `TradingClient`). This makes `kill_switch` and `reconciler` callers see "no API client" and follow their existing no-op paths, rather than crashing on Alpaca SDK init.

#### `regime_platform/services/operational/kill_switch.py` — MODIFIED
- `cancel_all_orders()` and `close_all_positions()` short-circuit when the gate is active, returning `{"cancelled_count": 0, ..., "deprecated": True}` (or `closed_count` for close). The `deprecated` flag lets admin UIs surface a "this endpoint is retired" banner without parsing the error stream.
- Both methods log a WARNING with the user_id so operators can audit any emergency-action attempts that hit the disabled gate.

#### `regime_platform/services/operational/reconciler.py` — MODIFIED
- `reconcile_order()` returns a synthetic `ReconciliationResult` with `status=PENDING` and a `discrepancy_notes` field explaining "P5: execution disabled — reconciliation is the hermes agent's responsibility."
- `reconcile_all_recent()` returns `[]` (empty list).
- `reconcile_positions()` returns a synthetic `PositionReconciliationResult(our_positions=0, alpaca_positions=0, matched=True, discrepancies=[])` — i.e. "nothing to compare because we hold nothing."

#### `regime_platform/services/operational/mode_manager.py` — MODIFIED
- `_switch_executor_mode()` is a no-op when the gate is active. Logs a WARNING explaining the hermes agent owns paper/live mode via its own env vars. The HTTP route `/operational/mode/confirm` will succeed but produce no state change — operators should rely on the hermes agent's mode configuration.

#### `regime_platform/core/exceptions.py` — MODIFIED
- `classify_exception()` now returns `410 Gone` for `ExecutionDisabledError`. This is distinct from `503 Service Unavailable` (which would imply temporary outage) — `410` is the correct HTTP semantic for "permanently retired, look elsewhere." The error body explains where to find the replacement (hermes agent).

#### `regime_platform/services/stream_splitter.py` — NEW FILE (~330 lines)
- `StreamSplitter` background task — consumes `signal.raw.noble_trader` via `XREADGROUP` (consumer group `stream_splitter`, block=30000ms matching the agent's block time).
- Resolves `nt_symbol.plan_ids` for each signal's symbol via PostgREST, with a 5-minute in-process cache (the nt_symbol universe changes rarely).
- Republishes to `signals:signal_scout` and/or `signals:precision_pro` based on plan membership, enriching each republished entry with `plan_id`, `plan_name`, `split_at`, `source_entry_id`.
- **Fail-open policy**: if Supabase is unreachable OR `plan_ids` is empty (symbol not curated), publishes to ALL configured plan streams. This matches the proxy's `plan_resolver` fail-closed semantics — when the proxy can't verify membership, it serves nothing; the splitter publishing to both ensures the proxy still has the signal in its stream.
- **Raw stream is preserved**: the splitter ACKs its own consumer-group entries but does not trim the stream. Proxy and agent read the same raw stream independently via their own consumer groups.
- `invalidate_cache(symbol=None)` for admin-triggered evictions after `nt_symbol.plan_ids` updates.
- `health()` returns stats (consumed, republished_scout, republished_precision, errors, cache hit/miss, last activity timestamps) for observability.
- Singleton via `get_stream_splitter()` — started by the FastAPI lifespan handler.

#### `notifications/discord.py` — MODIFIED
- `DiscordNotifier.__init__` adds `_plan_webhooks` dict (plan_id UUID → webhook URL), populated lazily via `_init_plan_webhooks_if_needed()`. Lazy init lets test fixtures set env vars before the map is built.
- `_resolve_signal_webhook()` accepts a new `plan_ids: Optional[list[str]]` parameter. Routing priority (P5):
  1. Per-plan webhook (if `plan_ids` contains a configured plan UUID)
  2. Per-asset-class webhook (existing behavior)
  3. Default `DISCORD_WEBHOOK_SIGNALS` (fallback)
- When a symbol belongs to BOTH plans (the backfill default from P2), the FIRST configured plan webhook wins. Operators can choose a primary channel by ordering the env vars.
- `send_signal()` accepts a new `plan_ids: Optional[list[str]]` parameter, threaded through to `_resolve_signal_webhook`. Existing callers are unaffected (the parameter is optional).
- 4-channel canonical Discord layout for Noble Trader:
  1. `#signal-scout` ← `DISCORD_WEBHOOK_SIGNAL_SCOUT` — Signal Scout ($79/mo) subscriber signals
  2. `#precision-pro` ← `DISCORD_WEBHOOK_SIGNAL_PRECISION` — Precision Pro ($199/mo) subscriber signals
  3. `#system-status` ← `DISCORD_WEBHOOK_STATUS` — system health + regime updates
  4. `#trade-executions` ← `DISCORD_WEBHOOK_EXECUTIONS` — hermes agent execution alerts (rare on noble server now; mostly fired by agent itself)

#### `regime_platform/services/signal_aggregator.py` — MODIFIED
- `_route_to_discord()` now passes `plan_ids=signal.data.get("plan_ids")` to both `send_signal()` call sites (RENKO_SIGNAL and HEARTBEAT-with-buy/sell). The orchestrator's signal payload already includes `plan_ids` from P2's `supabase_client.py` projection change — no orchestrator code change needed here.
- Comment added explaining the P5 plan routing flow.

#### `docs/Setup/REDIS_ACL_SETUP.md` — NEW FILE
- Comprehensive guide for creating the `sub_<32hex>` (proxy) and `agent_<32hex>` (agent) ACL users on Railway Redis.
- Documents the ACL URL format: `rediss://<username>:<password>@<host>.railway.app:<port>/0` — RFC 3986 compliant, parsed natively by `redis-py` and `aioredis.from_url()`.
- Covers `ACL SETUSER`, `ACL SAVE` (for persistence across restarts), verification commands, troubleshooting common errors (`NOAUTH`, `WRONGPASS`, `NOPERM`, `BUSYGROUP`), and migration from pre-P5 default-user deployments.
- Notes that `XAUTOCLAIM` for stale PEL entries is intentionally NOT automated — operators should monitor `/health` `stats.pending` and reclaim manually if it grows past 100.

### Architectural decisions

1. **Gate, don't delete** — Execution code is gated behind `assert_execution_enabled()` rather than deleted. This preserves the code for emergency rollback (`NOBLE_TRADER_DISABLE_EXECUTION=false`), provides reference for the hermes agent's new executor, and avoids breaking imports in `renko/__init__.py` and `regime_platform/services/operational/__init__.py`. The gate is the contract; deletion would be a forcing function we don't need yet.

2. **410 Gone, not 503 Service Unavailable** — `ExecutionDisabledError` maps to HTTP 410, not 503. 503 implies "try again later"; 410 means "permanently retired, look elsewhere." Operators and admin UIs can branch on the status code to show a deprecation banner vs. a retry button.

3. **Fail-open on Supabase outage, fail-closed on unknown symbol** — The StreamSplitter publishes to BOTH plan streams when Supabase is unreachable (fail-open: better to over-deliver than under-deliver; the proxy is the canonical gate). But it returns an empty set when the symbol isn't in `nt_symbol` (fail-closed: an unknown symbol is a hard error regardless of gate state).

4. **Sequential XADDs in StreamSplitter, not parallel** — Per-signal XADDs to multiple plan streams are sequential. Parallel would add latency complexity for ~0ms gain (Redis pipeline round-trip is sub-ms locally, ~2ms over TLS to Railway). Sequential is easier to reason about and matches the orchestrator's existing sequential XADD pattern.

5. **Plan UUIDs in env vars, not derived at runtime** — `NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID` and `NOBLE_TRADER_PLAN_PRECISION_PRO_ID` are env vars, not derived from `plans.title` at startup. This avoids a Supabase round-trip during boot and keeps the plan-routing logic testable without a live database. Operators resolve the UUIDs once via `SELECT id FROM plans WHERE title = 'Signal Scout'` and bake them into the env.

6. **Lazy plan-webhook initialization** — `_init_plan_webhooks_if_needed()` populates the `_plan_webhooks` dict on first use, not at module import. This lets test fixtures set `NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID` etc. AFTER importing the discord module, mirroring the lazy-settings pattern used in the P3 proxy codebase.

### Verification checklist

- [x] New modules import cleanly: `from regime_platform.services.execution_deprecation import ...` and `from regime_platform.services.stream_splitter import ...`
- [x] `assert_execution_enabled(context='smoke_test')` raises `ExecutionDisabledError` when `NOBLE_TRADER_DISABLE_EXECUTION=true`; does not raise when `false`.
- [x] `DiscordNotifier._resolve_signal_webhook(asset_class, plan_ids)` routes correctly:
  - Scout UUID in plan_ids → Scout webhook
  - Precision UUID in plan_ids → Precision webhook
  - Empty/None plan_ids → falls back to asset-class routing → falls back to default SIGNALS webhook
  - Unknown UUID in plan_ids → falls back to default
- [ ] `StreamSplitter.start()` creates consumer group `stream_splitter` on `signal.raw.noble_trader` (requires live Redis — integration test deferred)
- [ ] `StreamSplitter._process_entry()` correctly resolves plan_ids from PostgREST and republishes to the right streams (integration test deferred)
- [ ] HTTP routes in `routers/operational.py` return 410 Gone when execution is disabled (smoke-test via TestClient deferred)
- [ ] End-to-end: orchestrator publishes → splitter republishes to both plan streams → proxy consumes from plan stream → agent consumes from plan stream (deferred to P5 integration testing)

### Deferred to later phases

- **P6 (nobletradingapp)**: Admin UI for curating `nt_symbol.plan_ids` — let operators move a symbol from "both plans" to "Precision Pro only" with one click. Will call a new Edge Function `nt_symbol_set_plan_ids` (wraps the UPDATE so the UI never needs service-role key).
- **P6 (nobletradingapp)**: Edge Functions `license-validate` and `symbols-for-plan` — required for the proxy (P3) to actually validate licenses. Until they exist, the proxy 503s on `/quotes` calls.
- **StreamSplitter test coverage** — needs `fakeredis` + `respx` mocking. Deferred to keep P5 scoped.
- **`XAUTOCLAIM` automation** — currently manual per the ACL setup doc. Could be automated as a periodic background task if PEL growth becomes an issue.
- **Removal of `alpaca-py` from `requirements.txt`** — The SDK is still used by `adapters/l2_adapters.py` and `adapters/feed_adapters.py` for L2/market-data feeds. Phase 2 of the deprecation (post-P5) will migrate these to raw `httpx` + `websockets` so `alpaca-py` can be dropped entirely.
- **`renko/executor.py` deletion** — File is gated, not deleted. Deletion target: post-P6 once the hermes agent's executor is stable in production for 30 days.

### Known gaps

- **StreamSplitter not wired into FastAPI lifespan yet** — The class exists and is fully functional, but `main.py` / `app.py` doesn't yet call `get_stream_splitter()` on startup. Will be added in the deployment-focused sub-phase before P5 ships to Railway. (One-line addition: `await get_stream_splitter()` in the lifespan handler.)
- **`/health` endpoint doesn't expose StreamSplitter stats yet** — The `health()` method exists but isn't surfaced in the existing `/health` route. Same deployment sub-phase will add a `stream_splitter` section to the response.
- **Orchestrator doesn't publish `plan_ids` in the signal payload yet** — P2 added `plan_ids` to the `fetch_active_symbols()` SELECT, but the orchestrator's `_process_symbol()` doesn't copy it into the published Redis payload. The StreamSplitter works around this by fetching plan_ids itself via PostgREST, but a future optimization is to have the orchestrator embed `plan_ids` directly in the payload (eliminates the splitter's Supabase round-trip).
- **No deprecation log on import** — `renko/executor.py` logs the gate block at WARNING level when `AlpacaExecutor.__init__` is called, but doesn't log at import time. Operators won't see "execution is deprecated" until someone tries to construct an executor. A startup log line in `main.py` would be more visible — deferred.

---

## [7.1.0] - 2026-07-20

### Added — P2: nt_symbol plan assignment (multi-plan curation)

#### `supabase/migrations/032_nt_symbol_plan_assignment.sql` — NEW FILE
- Adds three columns to `nt_symbol`:
  - `plan_ids UUID[]` — array of `public.plans.id` values (NULL = unassigned)
  - `curated_at TIMESTAMPTZ` — when plan_ids was last manually curated (NULL = system backfill)
  - `curated_by UUID` — `public.users.id` of the admin who last curated (FK with ON DELETE SET NULL)
- Adds GIN index `idx_nt_symbol_plan_ids_gin` for `WHERE plan_ids @> ARRAY[<plan_uuid>]::uuid[]` queries (single round-trip plan membership lookup)
- Adds trigger `trg_nt_symbol_plan_ids_fk` (BEFORE INSERT OR UPDATE OF plan_ids) that validates every array element exists in `public.plans(id)`. Postgres cannot enforce FK on array elements natively — this trigger closes that gap and raises `foreign_key_violation` on orphaned UUIDs.
- Backfills all currently-active symbols to BOTH `Signal Scout` and `Precision Pro` plans via a `plans.title IN (...)` subquery (NOT hardcoded UUIDs) so the backfill is idempotent and works regardless of the auto-generated plan UUID.
- `curated_at` / `curated_by` left NULL on backfill to distinguish "system-assigned" from "human-curated". A subsequent admin-UI edit will populate these fields.
- Includes verification SELECTs at the bottom showing symbol → plan_ids mapping and the new trigger/index registrations.

#### `regime_platform/sweep_orchestrator/supabase_client.py` — MODIFIED
- `fetch_active_symbols()` SELECT projection extended to include `plan_ids` (line 52). The field is now present in every row returned to the orchestrator. The orchestrator itself does not yet consume `plan_ids` — plan filtering happens proxy-side (per the new `noble-trader-proxy` codebase in P3). Exposing the field now means the orchestrator's symbol dict is forward-compatible with any downstream consumer that needs plan membership.

#### Architectural note — why `plan_ids` lives on `nt_symbol` and not in a join table
- A symbol can belong to 0, 1, or 2 plans (today) — possibly more in the future.
- The access pattern is "give me all symbols for plan X" → `WHERE plan_ids @> ARRAY[X]::uuid[]` against a GIN index. This is a single index scan, faster than a join-table lookup for a 2-plan universe.
- The array is small (≤ a handful of plans), so per-row storage overhead is negligible.
- Referential integrity is preserved by the trigger; orphaned plan UUIDs cannot be written.
- The join-table alternative (`nt_symbol_plans(symbol_id, plan_id)`) was rejected because it would require an additional hop on every proxy symbol-validation call, and the universe of plans is tiny enough that array storage is simpler.

#### Verification checklist
- [ ] Migration applies cleanly on a Supabase project with `nobletradingapp` 0001_init.sql + 0002_seed_plans.sql already applied (dependency: `public.plans`, `public.users`).
- [ ] GIN index `idx_nt_symbol_plan_ids_gin` shows in `pg_indexes`.
- [ ] Trigger `trg_nt_symbol_plan_ids_fk` shows in `pg_trigger` for `nt_symbol`.
- [ ] Inserting a row with an orphaned plan UUID raises `foreign_key_violation`.
- [ ] All currently-active symbols now have `plan_ids` populated with both plan UUIDs.
- [ ] `fetch_active_symbols()` returns rows containing `plan_ids` as a list of UUID strings.

#### Deferred to later phases
- P3: proxy reads `nt_symbol.plan_ids` via a Supabase Edge Function (license-validate or a new `symbols-for-plan` endpoint) to validate that an agent's selected symbols are in its plan.
- P6: nobletradingapp admin UI for curating `plan_ids` (set/unset plan membership per symbol, populate `curated_at` + `curated_by`).
- A future admin-only Edge Function `nt_symbol_set_plan_ids` will wrap the UPDATE so the UI never needs service-role key access.

---

## [7.0.1] - 2026-05-24

### Fixed - Renko Endpoint 500s + Supabase Env Var Clarification

#### Root Cause: Missing Supabase env vars on Render
- Renko GET endpoints (`/renko/signals`, `/renko/state`, `/renko/bricks`) returned HTTP 500 because `runtime_config` could not initialise - `SUPABASE_URL` and `SUPABASE_KEY` were not configured in the Render deployment environment
- `_get_pipeline_async()` calls `RenkoConfig.from_runtime_config()` which requires Supabase connectivity to load the 148-key `system_config` table
- Without Supabase, the pipeline initialisation fails with an unhandled exception, surfacing as HTTP 500

#### Fix: Render environment variables configured
- `SUPABASE_URL` - set to `https://pcvscowltlrxzgxjurcr.supabase.co`
- `SUPABASE_KEY` - set to the Supabase **service_role** key (NOT the anon/publishable key)
- All 3 Renko endpoints now return 401 (correct auth challenge) instead of 500

#### Important clarification: No `SUPABASE_PUBLISHABLE_KEY` variable exists on the backend
- The backend uses `SUPABASE_KEY` which must be the **service_role** key for full DB access
- The anon/publishable key (`NEXT_PUBLIC_SUPABASE_ANON_KEY`) is for the **frontend only** (Next.js/Vercel)
- Backend requires service_role for `system_config`, `ta_renko_snapshot`, `api_keys`, `system_config_audit`, and `rate_limit_violations` tables

#### Diagnostic endpoint available
- `GET /renko/health` (no auth required) now tests: runtime_config, config_creation, pipeline_creation, supabase_snapshot, and pipeline_async connectivity
- Use this to verify Supabase env vars are correctly configured on any deployment

### Changed - Documentation Updates

#### `docs/deployment.md`
- Expanded Supabase section with critical Render deployment warning
- Added cross-stack key summary table (backend vs frontend key names and types)
- Added `/renko/health` diagnostic reference

#### `docs/openapi.yaml`
- Added 39 missing endpoint paths across 7 sections: Admin Config (9), Operational Hardening (18), PnL Dashboard (7), Risk Dashboard (1), Auth Debug (1), SSE PnL (2), and previously missing stream/feed endpoints
- Added 4 new tags: Admin Config, Operational Hardening, PnL Dashboard, Live Stream REST
- Total documented paths: 94 unique paths

#### `docs/API.md`
- Bumped to v7.0.0 (was v6.0.0)
- Updated endpoint count from 57 to 104
- Added Operational Hardening section (18 endpoints with full descriptions)
- Added Admin Config API, PnL Dashboard, Live Stream REST, SSE Streaming sections
- Added Deployment section to table of contents

---

## [7.0.0] - 2026-05-24

### Added - Runtime Configuration Engine + Admin API

#### `core/runtime_config.py` - NEW FILE
- `RuntimeConfig` singleton with 30-second TTL in-memory cache backed by Supabase `system_config` table
- Resolution order: DB override (highest) -> env var -> hardcoded default (lowest)
- Async-safe via `asyncio.Lock` with double-checked locking pattern
- Type-strict getters: `get_float()`, `get_int()`, `get_bool()`, `get_str()`, `get_json()`
- `set_value()` with validation (type coercion, range check, enum check), DB persist, audit logging, and on-change callbacks
- `reset_value()` to revert a key to its hardcoded default
- `reload()` to force full cache refresh from DB (bypasses TTL)
- `_SyncProxy` / `sync_config` for non-async contexts (env var -> default only, no DB)
- 148 default values registered across 8 categories (renko, execution, alpaca, risk, sizing, stream, auth, regime)

#### `routers/config.py` - NEW FILE
- 9 REST endpoints under `/config/`:
  - `GET /config/` - List all entries grouped by category
  - `GET /config/categories` - List category names
  - `GET /config/{category}` - Get entries for a category
  - `GET /config/key/{key}` - Get single entry
  - `PATCH /config/key/{key}` - Update value (admin only, validated)
  - `POST /config/key/{key}/reset` - Reset to default (admin only)
  - `GET /config/reload` - Force cache reload (admin only)
  - `GET /config/schema` - Full schema with types/ranges
  - `GET /config/audit` - Config change audit log (admin only)
- Sensitive value masking for non-admin readers
- Pydantic models: `ConfigValueUpdate`, `ConfigEntryResponse`, `ConfigCategoryResponse`, `ConfigListResponse`, `ConfigReloadResponse`, `ConfigAuditEntry`

#### `supabase/migrations/007_system_config.sql` - NEW FILE
- `system_config` table with JSONB values, type/category metadata, range constraints, RLS policies
- `system_config_audit` table with immutable append-only change log
- Auto-updating `updated_at` trigger
- Seeds for 139 config keys across renko, sizing, risk, regime, execution, stream, alpaca, auth

#### `supabase/migrations/008_system_config_extended.sql` - NEW FILE
- Adds 9 extended config keys: execution financing (4), fill probability internals (5), stream subscriber queue (1), Alpaca WS timeouts/throttling (10), risk stress test extended (3)
- Uses `ON CONFLICT (key) DO NOTHING` - safe to re-run

#### Documentation - NEW FILES
- `docs/ADMIN_CONFIG_API.md` - Full API reference for all 9 config endpoints
- `docs/ADMIN_CONFIG_KEY_CATALOG.md` - Complete catalog of all 148 config keys with types, defaults, ranges, and descriptions

### Fixed - Deployment Import Errors
- `routers/backtest.py` - Added missing `raise_http_exception` import
- `routers/regime.py` - Added missing `limiter`, `RATE_LIMIT_COMPUTE`, `raise_http_exception`, `Request` imports; fixed `@limiter.limit` decorator order
- `routers/risk.py` - Added missing `limiter`, `RATE_LIMIT_COMPUTE`, `RATE_LIMIT_DEFAULT`, `raise_http_exception`, `Request`, `Query`, `RiskDashboardResponse`; fixed decorator order
- `routers/sizing.py` - Added missing `limiter`, `RATE_LIMIT_COMPUTE`, `raise_http_exception`, `Request`, all Masaniello schema imports; fixed import sources
- `routers/strategy.py`, `routers/observation.py`, `routers/pipeline.py`, `routers/tda.py` - Added missing `raise_http_exception` import
- `routers/auth_router.py` - Added missing `import os`
- `routers/stream_ws.py` - Added missing `import logging` and `_log = logging.getLogger(__name__)`
- `renko/router.py` - Added `Request` import and `request: Request` parameter to 3 endpoints (`process_tick`, `process_batch`, `reset_pipeline`); fixed `@limiter.limit` decorator order
- `main.py` - Added missing `import os` (used by `os.getenv("CORS_ALLOWED_ORIGINS")` and `os.getenv("DEPLOY_ENV")`)

### Changed
- All backend hardcoded parameters are now runtime-configurable via `runtime_config.get_*()` calls
- Config changes propagate to all services within 30 seconds (TTL cache)
- Admin role required for all config mutations (`require_admin` dependency)
- Auth level summary updated: 4 admin-only config endpoints added (PATCH, POST reset, GET reload, GET audit)

---

## [3.4.0] - 2026-05-21

### Fixed - Auth Hardening (All 57 Endpoints Now Protected)

#### Critical: SSE Endpoints Had No Authentication
- `GET /sse/{symbol}` - was completely unauthenticated; now requires `get_authed_user`
- `GET /sse/alerts` - was completely unauthenticated; now requires `get_authed_user`
- These endpoints exposed real-time regime data, risk metrics, and position info to anyone

#### Renko Write Operations Upgraded from Viewer to Trader/Admin
- `POST /renko/tick` - `get_authed_user` -> `require_write` (viewers can no longer inject ticks)
- `POST /renko/tick/batch` - `get_authed_user` -> `require_write`
- `POST /renko/backtest/run` - `get_authed_user` -> `require_write`
- `POST /renko/backtest/run/stream` - `get_authed_user` -> `require_write`
- `POST /renko/backtest/optimize` - `get_authed_user` -> `require_write`

#### `regime_platform/routers/stream_ws.py` - Updated
- Added `Depends` import and `get_authed_user`, `TokenData` imports from `jwt_auth`
- Both SSE endpoints now accept `user: TokenData = Depends(get_authed_user)`

#### `renko/router.py` - Updated
- All mutation endpoints upgraded to `require_write` dependency
- Updated docstring to reflect new auth levels

### Added - Clerk JWT Template System + Backend Enrichment

#### `regime_platform/auth/clerk_auth.py` - Updated
- New `_enrich_from_clerk_api(sub)` function: calls `GET /v1/users/{sub}` when JWT claims are null
- Extracts email, first_name, last_name, username from Clerk user profile
- Reads role from `private_metadata.role` (matching frontend convention)
- Results cached 5 minutes per user to limit Clerk API calls
- Integrated into `verify_clerk_token()`: when email/name/username are all null, enrichment triggers automatically
- This ensures `/auth/clerk/me` and `/auth/clerk/verify` always return complete user info

#### Frontend JWT Resolution
- `getClerkJWT()` in `fastapi-auth.js` now tries `"server"` JWT template first
- Falls back to default JWT if template not configured
- Updated all 7 `getClerkJWT()` locations across the codebase
- Template adds email, first_name, last_name, username, role claims to the JWT

### Changed
- Auth level summary: 4 public (by design), 27 any-role, 22 write (admin/trader), 4 admin-only
- All docs updated: API.md (v5.5.0), openapi.yaml (v5.5.0), CHANGELOG.md

---

## [3.3.0] - 2026-05-19

### Added - SSE Streaming for Progressive/Chunked Backtest Loading

#### `renko/pipeline.py` - Updated
- `backtest_chunked()` - new generator method that yields intermediate results after every N ticks
- Yields `progress` events with: chunk number, total chunks, percent complete, trades so far, running stats, bricks/ticks processed
- Final `complete` event matches the shape of `backtest()` return value for seamless compatibility
- Configurable `chunk_size` parameter (default 150 ticks)
- Supports `timestamps` and `regimes` parameters like the regular `backtest()`

#### `renko/router.py` - Updated
- `POST /renko/backtest/run/stream` - new SSE endpoint with `StreamingResponse`
- Streams `progress` -> `progress` -> ... -> `complete` events as `data: {...}\n\n` SSE format
- `error` event type for pipeline failures
- Redis cache check on entry - if cache HIT, emits single `complete` event immediately
- Final complete result is cached in Redis (1h TTL) for subsequent non-streaming requests
- Rate-limited under `RATE_LIMIT_WRITE`
- Requires authentication via `get_authed_user`

#### `tests/test_renko_backtest.py` - Updated
- `TestRenkoBacktestChunked` - 6-test suite covering:
  - Progress events are yielded with correct count (>= 2 progress + 1 complete)
  - Each progress event has required keys (chunk, total_chunks, percent, trades_so_far, stats_so_far, etc.)
  - Percent increases monotonically across progress events
  - Complete event matches regular `backtest()` result (same total_ticks, total_bricks, trades count)
  - Last progress event has percent = 100
  - Chunked backtest accepts regime labels

### Changed
- Total backend test count: 25 Renko backtest tests (19 existing + 6 new chunked)

---

## [3.2.0] - 2026-05-19

### Added - Walk-Forward Backtest Engine + Persistence + Optimization

#### `core/backtester.py` - NEW FILE
- 503-line walk-forward HMM backtest engine
- Rolling HMM fit on trailing window with configurable refit interval
- Strategy signal generation based on detected regime
- Regime-gated Kelly position sizing
- Risk check gates (VaR, max drawdown)
- Trade execution with configurable commission and slippage
- 30+ performance metrics: win rate, Sharpe, Sortino, Calmar, profit factor,
  max drawdown, VaR/CVaR, avg hold bars, regime distribution, trades by regime
- Equity curve, drawdown curve, and full trade log output
- Optional date labels in trade log (pass `dates` array)

#### `services/backtest_store.py` - NEW FILE
- Supabase persistence layer for backtest results
- `save_backtest_result()` - persist to `ta_backtest_result` table (user-scoped)
- `get_backtest_history()` - paginated history with summary metrics only
- `get_backtest_detail()` - full result by ID (user-scoped)
- `delete_backtest()` - delete by ID (user-scoped)
- Graceful degradation when Supabase is not configured

#### `routers/backtest.py` - NEW FILE
- `POST /backtest/run` - run walk-forward backtest (rate-limited, Clerk/JWT auth)
- `GET /backtest/history` - paginated list of saved results
- `GET /backtest/{id}` - full result by ID
- `POST /backtest/compare` - side-by-side comparison with diff summary
- `POST /backtest/optimize` - parameter grid sweep (max 50 combos)
- `POST /backtest/export` - CSV/JSON export with section selection
- `DELETE /backtest/{id}` - delete saved result

#### `models/schemas.py` - Updated
- `BacktestRequest` - extended with `dates` and `save` fields
- `BacktestResponse` - full response with 30+ metric fields
- `BacktestSummary` - lightweight summary for list views
- `BacktestHistoryResponse` - paginated summary list
- `BacktestCompareRequest` / `BacktestCompareResponse` - comparison + diff
- `BacktestOptimizeRequest` / `BacktestOptimizeResponse` - grid sweep
- `BacktestExportRequest` - export format + section selection

#### `supabase/migrations/00000000000007_backtest_results.sql` - NEW FILE
- `ta_backtest_result` table with RLS policies (user-scoped access)
- Summary columns + JSONB columns for heavy data (equity curve, trade log, etc.)

### Fixed
- `pnl_pct` was always returning 0.0 in backtest trade log - now correctly computed

### Changed
- All backtest endpoints require Clerk/JWT authentication
- Rate-limited under `RATE_LIMIT_COMPUTE`

---

## [3.1.0] - 2026-Q2

### Changed - Dynamic Masaniello + Kelly Unified Sizing Engine

#### `core/position_sizer.py` - Full Refactor

Two complementary sizing paths now live in the same module, independently usable.

**Path A - Portfolio-fraction Kelly (preserved, interface unchanged)**
`PositionSizer`, `PositionRequest`, `KellyResult` - stateless, returns-based.
All existing callers continue to work without modification.

**Path B - Dynamic Masaniello Sizer (new)**

Implements the formula:

```
f_i = β × (0.5 + M_i) × Q_i × DD_i × V_i
```

New public symbols:

| Symbol | Purpose |
|--------|---------|
| `SizingConfig` | β, min/max risk, gates, kelly overlay, batch params |
| `TradeContext` | Per-trade inputs (equity, stop, point_value, p_win, R/R, regime fields) |
| `TradeContext.from_snapshot(snap, ...)` | Build context directly from `RegimeSnapshot` |
| `PositionSizeResult` | All intermediate factors + contracts + block reason |
| `PositionSizeResult.summary()` | One-line human-readable string |
| `BatchState` | Cumulative wins/losses/pnl/drawdown/peak_equity tracker |
| `DynamicMasanielloSizer` | Stateful sizer; `.batch` tracks inter-trade state |
| `.size_trade(ctx)` | Core formula computation |
| `.size_from_snapshot(snap, ...)` | Convenience wrapper - builds context + sizes |
| `.run_batch(contexts, outcomes, pnls)` | Size N trades sequentially with state injection |
| `monte_carlo_batch(sizer, ctx, n)` | Simulate N independent batches; return PnL/DD distribution |

**Pure factor functions (testable in isolation):**
`_masaniello_factor`, `_quality_factor`, `_drawdown_factor`, `_volatility_factor`,
`_fractional_kelly`, `_expected_edge`

**Factor details:**

`M_i = clip((W - w) / (N - i + 1), 0.0, 1.5)`
- Wins needed divided by trades remaining. Zero when target already met.
- `(0.5 + M_i)` term guarantees minimum 0.5× base risk even when M_i = 0.

`Q_i = prob_factor × regime_factor × conf_factor`
- `prob_factor  = clip((p_win - min_prob) / 0.10, 0.0, 1.5)`
- `regime_factor = clip(regime_quality, 0.0, 1.5)`  ← from `RegimeSnapshot.risk_multiplier`
- `conf_factor   = clip(state_confidence, 0.0, 1.0)` ← from `RegimeSnapshot.confidence`
- Q_i = 0 when any sub-factor is zero; acts as a quality gate.

`DD_i = clip(1 - dd / max_dd, 0.25, 1.0)`
- Floor at 0.25 preserves minimum position participation during drawdown.

`V_i = clip(ATR_baseline / ATR_current, 0.5, 1.5)`
- Inverted ATR ratio. High vol -> shrink; low vol -> mild expansion.

**Pre-flight gates (in priority order, fire before formula):**
1. equity > 0
2. stop_distance_price > 0
3. point_value > 0
4. regime_quality ≥ regime_floor
5. p_win ≥ min_prob
6. reward_risk ≥ min_rr
7. batch not halted
8. drawdown < batch_halt_dd (triggers halt on violation)
9. expected_edge > 0

**Kelly overlay:**
When `use_kelly_overlay=True`, the Masaniello fraction is capped at
`kelly_fraction × max(0, (b·p - q) / b)`. When kelly_cap = 0 (no edge),
the min_risk floor still applies (position is not zeroed out).

**Hard clamp:**
`risk_fraction = clip(f_i, min_risk, max_risk)` - always enforced last.

#### `test_position_sizer.py` - NEW FILE
95-test suite covering:
- Factor math boundary conditions (all 4 factors + Kelly + edge)
- Core formula with known inputs (manual verification)
- Gate ordering - each block reason fires correctly
- Kelly overlay (cap applied, cap=0 fallback)
- Hard clamp (upper and lower bounds)
- RegimeSnapshot integration (`from_snapshot`, `size_from_snapshot`)
- BatchState lifecycle (record, drawdown, reset)
- `run_batch` state injection and sequential progression
- Monte Carlo (p_target_hit ~69.5%, p_halt, mean/std/p10/p90 PnL)
- Path A interface preserved (KellyResult fields, regime gating, vol scaling)

### Fixed
- Uploaded `dataclass.md` bug: `_init_` (single underscores) corrected to `__init__` in `DynamicMasanielloSizer`
- Corrected Kelly gate test: `p=0.50, R=1.0` correctly yields `f=0.0` (not `p=0.50, R=2.0` which has positive edge)

---

## [3.0.0] - 2025-Q1

### Added - GPU HMM · Live Feeds · Correlation Regimes · Portfolio Optimisation

#### `core/gpu_engine.py` - NEW FILE
- `GpuRegimeHMM` - pomegranate PyTorch-backed 4-state HMM, identical interface to `RegimeHMM`
- `create_regime_hmm(force_cpu)` - auto-selecting factory: GPU if pomegranate + CUDA/MPS available, else hmmlearn CPU
- `gpu_capabilities()` - returns `{pomegranate_installed, cuda_available, mps_available, active_device, hmm_backend}`
- Transparent CPU fallback - zero code changes required when GPU unavailable
- Supports CUDA (NVIDIA), MPS (Apple Silicon), and CPU targets

#### `adapters/feed_adapters.py` - NEW FILE
- `FeedAdapter` - abstract base with exponential-backoff reconnect (max 10 attempts)
- `AlpacaFeedAdapter` - alpaca-py WebSocket, supports SIP and IEX feeds, equities + crypto
- `BinanceFeedAdapter` - native WebSocket kline stream, any interval (1m/5m/1h/...), mainnet + testnet
- `IBFeedAdapter` - ib_async TWS API, real-time 5-second bars, equities/futures/FX
- `OHLCVBar` - unified OHLCV dataclass with `source` field
- `FeedManager` - multi-adapter orchestrator, routes bars into `SessionRegistry`, chainable `.add()`

#### `core/correlation_regime.py` - NEW FILE
- `CorrelationRegimeDetector` - DCC-based portfolio-level correlation regime classifier
- Extracts rolling Pearson correlation matrices (configurable window, default 60 bars)
- Applies EWMA vol standardisation before correlation computation
- Fits a 4-state Gaussian HMM on upper-triangle correlation feature vectors
- States labelled by mean |ρ|: `low_corr` | `mid_corr` | `high_corr` | `crisis`
- `CorrelationSnapshot` - per-asset breakdowns + `blended_risk_multiplier` = mean(asset_mults) × corr_mult
- Correlation risk multipliers: `low_corr=1.00` · `mid_corr=0.85` · `high_corr=0.60` · `crisis=0.35`

#### `core/portfolio_optimiser.py` - NEW FILE
- `DrawdownOptimiser` - regime-aware max-Sharpe subject to drawdown constraint
- Three regime layers: (1) regime-adjusted μ, (2) per-asset weight bounds, (3) correlation exposure
- Drawdown constraint via Cornish-Fisher CVaR proxy (analytic - no simulation required)
- Correlation exposure scalar: `low_corr=1.00` · `mid_corr=0.90` · `high_corr=0.70` · `crisis=0.50`
- Weight bounds tightened per-asset: `high_vol_strong_bear -> max_weight × 0.25`
- Scipy SLSQP solver with configurable `max_iter`, `tol`, and `dd_penalty_lambda`
- `OptimisationResult` - weights, regime_adj_weights, expected return/vol/Sharpe/DD, convergence info

#### `routers/gpu.py` - NEW FILE
- `GET /gpu/capabilities` - hardware and backend report
- `POST /gpu/benchmark` - fit + predict latency benchmark on synthetic data, `force_cpu` flag

#### `routers/feeds.py` - NEW FILE
- `POST /feeds/start` - register and start feed adapters as background asyncio tasks
- `POST /feeds/stop` - graceful shutdown of all adapter tasks
- `GET /feeds/status` - adapter health + per-symbol bar counts

#### `routers/multi_asset.py` - NEW FILE
- `POST /correlation/detect` - fit DCC correlation HMM + return correlation regime snapshot
- `POST /optimise` - solve drawdown-controlled max-Sharpe with regime overlays
- `POST /optimise/full` - one-shot: correlation detect + portfolio optimise

#### `main_v4.py` - NEW FILE
- v3.0 app factory with GPU capability logging on startup
- `GET /health` now includes `gpu_backend` and `device` fields

### Changed
- `requirements.txt` - added optional GPU and feed adapter dependency comments
- `core/portfolio_optimiser.py` - fixed array ambiguity bug in `neg_sharpe_with_dd` (scipy `max()` applied to matrix)

### Architecture summary

```
GET  /gpu/capabilities       pomegranate install check -> CUDA/MPS/CPU selection
POST /gpu/benchmark          synthetic fit -> latency report

POST /feeds/start            FeedManager -> AlpacaFeedAdapter/BinanceFeedAdapter/IBFeedAdapter
                             -> SessionRegistry.get(symbol).tick(close, ts)

POST /correlation/detect     rolling corr features -> 4-state HMM -> corr regime label
POST /optimise               regime-adj μ -> SLSQP -> DD-constrained weights
POST /optimise/full          detect + optimise in one request
```

---

## [2.1.0] - 2024-Q4

### Added - Simulation · Portfolio · Redis Persistence · JWT Auth

#### `core/simulator.py` - NEW FILE
- `RegimeSimulator` - Markov-chain Monte Carlo regime transition simulator
- Builds a 16×16 combined transition matrix via Kronecker product of the fitted
  4-state vol and trend HMM transition matrices: `T_combined = T_vol ⊗ T_trend`
- Fits per-regime (μ, σ) return distributions from Viterbi-decoded historical paths
- Simulates `n_paths` forward trajectories of `horizon` steps with regime switching
- Returns: price fan (p5/p25/median/p75/p95), expected risk multiplier per step,
  terminal return distribution (mean, std, VaR95, CVaR95), most likely terminal
  regime, % paths with positive return, mean maximum drawdown across paths

#### `services/portfolio_service.py` - NEW FILE
- `PortfolioService` - aggregates regime + risk analytics across all active sessions
- Per-symbol: regime label, confidence, risk_multiplier, Kelly f*, VaR95, Sharpe
- Portfolio-level: dominant regime, regime consensus, avg risk multiplier,
  portfolio VaR (√ΣVaR² - independence assumption), portfolio CVaR
- Risk flags: `high_risk_count`, `concentration_flag`, `regime_divergence_flag`
- Regime divergence detection via normalised entropy of vol_state distribution

#### `services/redis_persistence.py` - NEW FILE
- `RedisPersistence` - optional Redis Streams price buffer persistence layer
- All methods are no-ops when `REDIS_URL` env var is not set (zero overhead)
- `restore(session)` - hydrates session price buffer from Redis on startup
- `append_price(symbol, price)` - appends to Redis Stream with MAXLEN cap
- `save_meta / load_meta` - stores session config (kelly_fraction, etc.)
- `list_persisted_symbols()` - returns all symbols with persisted streams
- Failure-tolerant: Redis errors never raise in the tick path (log + continue)

#### `auth/jwt_auth.py` - NEW FILE
- `create_access_token(data)` - creates signed HS256 JWT
- `decode_token(token)` - validates and decodes JWT -> `TokenData`
- `get_current_user` - FastAPI Depends for HTTP endpoint auth
- `require_write` - Depends requiring trader or admin role
- `require_admin` - Depends requiring admin role
- `ws_auth(websocket, token, api_key)` - WebSocket auth (closes 4001 on failure)
- `make_login_response(sub, role)` - standard token response helper
- API key fallback via `X-API-Key` header and `API_KEYS` env var
- `AUTH_ENABLED=false` disables auth entirely for development

#### `routers/simulate.py` - NEW FILE
- `POST /simulate/{symbol}` - fits HMM + simulates regime transition paths
- Parameters: `horizon` (1–252), `n_paths` (50–5000), `seed`, `current_price`

#### `routers/portfolio.py` - NEW FILE
- `GET /portfolio` - aggregated portfolio risk view across all active sessions
- Query params: `symbols` (CSV), `kelly_fraction`, `target_vol`, `base_risk_limit`
- `GET /portfolio/symbols` - list all seeded symbols

#### `routers/auth_router.py` - NEW FILE
- `POST /auth/token` - OAuth2 password flow, issues JWT
- `GET /auth/me` - returns current user info from token
- `POST /auth/refresh` - issues a fresh token (same sub/role)
- User store configured via `AUTH_USERS` env var (user:pass:role CSV)

#### `main_v3.py` - NEW FILE
- v2.1 app factory with `lifespan` context manager
- On startup: restores persisted sessions from Redis, re-fits HMMs
- All v2.0 endpoints unchanged; v2.1 routers added alongside
- `/health` returns Redis status and active session count

### Changed
- `requirements.txt` - added `python-jose[cryptography]`, `passlib[bcrypt]`, `redis`

### Architecture summary

```
POST /simulate/{symbol}    HMM fit -> 16-state Kronecker system -> Monte Carlo
GET  /portfolio            SessionRegistry -> per-symbol regime + risk -> aggregate
POST /auth/token           credential check -> JWT issue
WS   /ws/{symbol}          (auth-ready: pass ?token=<jwt> to authenticate)
REDIS_URL (env)            on startup -> restore buffers -> re-fit HMMs
```

---

## [2.2.0] - 2024-Q3

### Added - 24-Feature Observation Vector & Stateful Builder

#### `core/obs_builder.py` - NEW FILE
- `InferenceObservationBuilder` - stateful class that maintains a fitted `RegimeHMM`
  across calls, ensuring Markov features (indices 14–19) are always computed from
  live HMM posteriors, never from a uniform prior
- `F` - feature index namespace (single source of truth for all 24 indices)
- `Observation` - output dataclass with `.markov_features` slice property and
  `.is_markov_uniform(tol)` guard method
- `build(prices, high, low)` - full-window build; re-fits HMM every `refit_every` bars
- `build_from_tick(price, high, low)` - streaming single-bar variant
- `seed(prices, high, low)` - historical warm-up and initial HMM fit
- `update_recommended_f(f)` - injects current Kelly fraction for Masaniello feature

#### 24-Feature Vector

| Index | Feature | Notes |
|-------|---------|-------|
| 0 | `log_return_1bar` | 1-bar log return |
| 1 | `log_return_3bar` | 3-bar log return |
| 2 | `log_return_10bar` | 10-bar log return |
| 3 | `normalised_atr` | ATR(14) / price |
| 4 | `rolling_vol_20` | 20-bar σ (annualised) |
| 5 | `ema_distance` | (price − EMA20) / EMA20 |
| 6 | `hhll_score` | HH/HL structure score ∈ [−1, 1] |
| 7 | `vol_regime_percentile` | Vol percentile vs 252-bar history |
| 8 | `atr_vs_baseline` | ATR(14) / ATR(252-bar mean) |
| 9 | `vol_trend_slope` | Standardised 10-bar vol slope |
| 10–13 | `regime_prob_{0..3}` | Raw HMM state posteriors (unsorted) |
| **14** | **`vol_prob_low`** | P(low vol) - Markov feature |
| **15** | **`vol_prob_medium`** | P(med_low + med_high) - Markov feature |
| **16** | **`vol_prob_high`** | P(high vol) - Markov feature |
| **17** | **`trend_prob_down`** | P(strong_bear + ½·bear) - Markov feature |
| **18** | **`trend_prob_neutral`** | P(½·bear + ½·bull) - Markov feature |
| **19** | **`trend_prob_up`** | P(½·bull + strong_bull) - Markov feature |
| 20 | `regime_quality` | HMM confidence scalar |
| 21 | `state_confidence` | max(vol_probs) × max(trend_probs) |
| 22 | `masaniello_pressure` | f* × (1 − f*) - peaks at 0.25 when f=0.5 |
| 23 | `drawdown_factor` | Current DD from peak ∈ [−1, 0] |

#### `core/regime_engine.py` - Updated
- Added `_sanitize_model()` static method: fixes NaN `startprob_`, zero `transmat_`
  rows, NaN `means_`, and degenerate `_covars_` after EM convergence failure
- `fit()` calls `_sanitize_model()` on both sub-models after every EM run
- `predict()` adds NaN fallback: degenerate posteriors replaced with uniform, then
  re-normalised - guarantees finite output even on pathological price series

#### `test_obs_builder.py` - NEW FILE
- 7-test suite: shape, finite values, Markov non-uniformity, Masaniello response,
  drawdown sign, streaming tick path, and all-zero sentinel guard

### Fixed
- `RegimeHMM._sanitize_model()` writes to `_covars_` private attribute (not the
  property setter) to avoid hmmlearn's shape-validation error on `diag` covariance
- Uniform-prior bug documented and guarded: `is_markov_uniform()` method on
  `Observation` detects std < 0.05 across Markov features and warns

---

## [2.1.0] - 2024-Q3

### Changed - 4-State HMM Regime Detection

#### `core/regime_engine.py`
- **n_components: 3 -> 4** for both the volatility and trend HMMs
- Volatility states: `low | med_low | med_high | high`  (was `low | medium | high`)
- Trend states: `strong_bear | bear | bull | strong_bull`  (was `bear | neutral | bull`)
- Volatile feature set expanded: vol HMM now uses 3 features (20-bar RV, 5-bar RV, |return|); trend HMM uses 3 features (return, 10-bar cum, 30-bar cum)
- Trend state labelling now sorts on feature index 2 (30-bar cumulative return) for cleaner strong_bull/strong_bear separation
- Min bars requirement: 80 (was 50) to support stable 4-state EM convergence
- Risk multiplier range extended: **[0.10, 1.75]** (was [0.25, 1.50])
- 4×4 = 16 distinct regime cells (was 9)

#### Downstream changes (label references only)
- `routers/regime.py`, `routers/pipeline.py` - label zips use `RegimeHMM.VOL_LABELS` / `RegimeHMM.TREND_LABELS`
- `routers/stream_rest.py`, `routers/stream_ws.py` - same label zip update
- `models/schemas.py`, `models/stream_schemas.py` - `min_length` / `ge` validators updated to 81
- `services/stream_session.py` - `MIN_PRICES_FOR_FIT` updated from 51 -> 81

#### Added
- `test_platform_v4.py` - 4-state smoke test verifying 4 prob vectors, 16-cell multiplier table, and correct assertions

---

# Changelog

All notable changes to the Dynamic Regime Risk Management Platform are documented here.
Follows [Semantic Versioning](https://semver.org/).

---

## [2.0.0] - 2024-Q2

### Added - Live Streaming Layer

#### `services/stream_session.py` - `StreamSession`
- Per-symbol stateful streaming engine with rolling price deque (configurable window, default 500 bars)
- Background HMM re-fit via `asyncio.run_in_executor` every N bars (default 50) - non-blocking
- 3-bar alert debounce: regime must persist for 3 consecutive bars before a `RegimeAlert` fires
- NaN/zero-price guard: invalid prices are filtered from the buffer before every HMM call
- Multiple concurrent subscriber queues per session (bounded at 500 items; slow consumers drop ticks)
- `RegimeAlert` dataclass with `severity` field: `info` | `warning` | `critical`

#### `services/registry.py` - `SessionRegistry`
- Singleton session store (`asyncio.Lock`-protected)
- `get_or_create(symbol, ...)` - idempotent session creation with per-session config
- `stats()` - returns live metrics for all sessions (ready, n_bars, refit_count, tick_count, last_regime)

#### `routers/stream_rest.py` - REST streaming endpoints
- `POST /stream/seed` - seed a symbol session with historical prices
- `POST /stream/tick` - push one price tick, receive `TickResponse` or buffering stub
- `POST /stream/ticks` - batch tick ingestion / backtest replay
- `GET /stream/sessions` - list all active sessions + stats
- `GET /stream/session/{symbol}` - single session status
- `DELETE /stream/session/{symbol}` - remove a session

#### `routers/stream_ws.py` - WebSocket + SSE endpoints
- `WS /ws/{symbol}` - full-duplex WebSocket: seed / tick / subscribe / ping message types
- `GET /sse/{symbol}` - SSE stream pushing `TickResponse` JSON on every bar
- `GET /sse/alerts` - global SSE broadcast for all regime-change alerts across all symbols
- 30-second heartbeat in subscriber mode; 15-second SSE comment heartbeat

#### `models/stream_schemas.py` - Streaming Pydantic models
- `SeedRequest`, `TickIngest`, `TickResponse`, `SessionStatus`, `WsMessage`

#### `main_v2.py`
- Updated app factory including all streaming routers alongside existing batch routers

#### `test_streaming.py`
- Async smoke test: seed -> 160-tick stream -> subscriber queue -> regime transition assertions

### Fixed

- `RegimeHMM` covariance type changed from `"full"` to `"diag"` for numerical stability on short rolling windows (< 300 bars)
- NaN/zero-price guard added to `_refit()` and `_compute_tick()` in `StreamSession`
- Test data continuity bug fixed (live price segment now correctly continues from seed tail)

---

## [1.0.0] - 2024-Q1

### Added - Batch Risk Management API

#### `core/regime_engine.py` - `RegimeHMM`
- Dual 3-state Gaussian HMM for volatility and trend regime classification
- Forward-backward posterior probabilities (not Viterbi hard assignments)
- Semantic state labelling by sorting component means - no manual mapping
- `RegimeSnapshot` dataclass with `risk_multiplier` property [0.25 – 1.50]

#### `core/position_sizer.py` - `PositionSizer`
- Full Kelly Criterion: `f* = μ / σ²` (continuous log-return formula)
- Fractional Kelly: `f* × kelly_fraction` (default 0.5)
- Volatility-scaled Kelly: `fractional_f × (target_vol / realised_annual_vol)`
- Regime-gated sizing: `vol_scaled_f × risk_multiplier`
- Automatic diagnostic notes (over-Kelly, negative Sharpe, vol mismatch, regime warnings)

#### `core/risk_manager.py` - `RiskManager`
- Historical (empirical) VaR at 95% and 99%
- CVaR / Expected Shortfall at 95% and 99%
- Maximum drawdown (peak-to-trough)
- Annualised return, volatility, Sortino ratio, Calmar ratio
- Regime-adjusted max loss limit: `base_risk_limit × risk_multiplier`
- Suggested stop-loss (2× CVaR95) and take-profit (3:1 reward/risk)
- Risk budget utilisation: `recommended_f / max_safe_f`

#### `models/schemas.py` - Pydantic v2 models
- `PricePayload`, `RegimeRequest/Response`, `SizeRequest/Response`, `RiskRequest/Response`, `FullAnalysisRequest/Response`
- Field-level validation: `min_length=51`, `ge`, `le`, `gt`, all-positive price guard

#### Routers
- `POST /regime/detect`
- `POST /size/kelly`
- `POST /risk/analyse`
- `POST /analyse/full` - one-shot full pipeline

#### Infrastructure
- `main.py` - FastAPI app factory with CORS middleware
- `requirements.txt` - pinned dependency versions
- `test_platform.py` - batch pipeline smoke test with synthetic multi-regime prices

---

## Roadmap

### Planned for v2.1
- `POST /simulate/{symbol}` - ODE-based regime transition simulation (simupy integration)
- `GET /portfolio` - multi-symbol aggregated regime + risk summary
- Redis Streams persistence for price buffers (survive process restarts)
- JWT authentication middleware for WebSocket handshake

### Planned for v3.0
- GPU-accelerated HMM via `pomegranate` (PyTorch backend)
- Real-time OHLCV feed adapters (Alpaca, Interactive Brokers, Binance)
- Multi-asset correlation regime detection
- Drawdown-controlled portfolio optimisation layer
