"""
Stream Splitter — Multi-Tenant Plan Routing
═══════════════════════════════════════════════════════════════════════════

P5 (2026-07-20) — Splits the canonical raw signal stream
(`signal.raw.noble_trader`) into per-plan Redis streams so that
downstream consumers (proxy, agent, Discord) can subscribe to a single
plan-specific channel and only see signals for symbols they're
authorized to receive.

Stream topology::

    ┌─────────────────────────┐
    │  Sweep Orchestrator    │
    │  (publishes once)      │
    └──────────┬─────────────┘
               │ XADD signal.raw.noble_trader
               ▼
    ┌─────────────────────────┐
    │  StreamSplitter         │  ◄── this module
    │  (consumer group:       │
    │   "stream_splitter")    │
    └──┬───────────────────┬──┘
       │ XADD              │ XADD
       ▼                   ▼
  signals:signal_scout  signals:precision_pro
       │                   │
       ▼                   ▼
  proxy (P3) +          proxy (P3) +
  agent (P4)            agent (P4)

Plan membership is read from `nt_symbol.plan_ids UUID[]` (added in P2
migration `032_nt_symbol_plan_assignment.sql`). The splitter caches
the lookup in-process for 5 minutes (the nt_symbol universe changes
rarely, and PostgREST round-trips on every signal would be expensive).

Fail-open policy
────────────────
If Supabase is unreachable when a signal arrives, the splitter publishes
to BOTH plan streams. This matches the proxy's plan_resolver fail-closed
semantics: when the proxy can't verify plan membership, it serves
nothing. The splitter publishing to both ensures the proxy still has the
signal in its stream to serve if/when its own resolver comes back online.
The proxy's license check is the canonical gate.

The raw stream is NEVER consumed destructively — the splitter ACKs its
own consumer-group entries but does not trim the stream. Other consumers
(proxy, agent) read the same raw stream independently via their own
consumer groups.

Usage::

    splitter = StreamSplitter()
    await splitter.start()    # spawns background consumer task
    ...
    await splitter.stop()     # graceful shutdown — waits for in-flight XADDs
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

import redis.asyncio as redis
from redis.backoff import ConstantBackoff
from redis.retry import Retry
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from regime_platform.core.settings import settings

log = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

RAW_STREAM = "signal.raw.noble_trader"
CONSUMER_GROUP = "stream_splitter"
CONSUMER_NAME = f"splitter-{int(time.time())}"

# Per-plan stream names. Hardcoded because these are part of the public
# contract with the proxy (P3) and agent (P4). Renaming requires a
# coordinated rollout across all three codebases.
STREAM_SIGNAL_SCOUT = "signals:signal_scout"
STREAM_PRECISION_PRO = "signals:precision_pro"

# Cache TTL for nt_symbol → plan_ids lookup. The nt_symbol universe
# changes rarely (admin-curated), so 5 min is a safe TTL. Forces a
# re-fetch on cache expiry. invalidate_cache() can force-evict for
# admin-triggered evictions.
SYMBOL_PLAN_CACHE_TTL_SEC = 300

# XREADGROUP block time — matches the agent's block time (P1).
# Long block = fewer round-trips, lower CPU; short block = faster
# shutdown response. 30s is the sweet spot.
BLOCK_MS = 30000

# Max in-flight XADDs before backpressure kicks in. Each pending
# republish adds ~1KB to the consumer's pending list; 1000 is generous.
MAX_PENDING_REPUBLISH = 1000

# Plan titles stored in public.plans (see nobletradingapp 0001_init.sql).
# Used to auto-resolve plan UUIDs from Supabase at startup so the two
# NOBLE_TRADER_PLAN_*_ID env vars are optional overrides, not requirements.
PLAN_TITLE_SIGNAL_SCOUT = "Signal Scout"
PLAN_TITLE_PRECISION_PRO = "Precision Pro"

# Cache TTL for the startup plan-UUID lookup from public.plans. The plan
# table changes rarely (admin-curated), so 5 min is safe. Forced re-fetch
# on cache expiry; invalidate via invalidate_plan_cache().
PLAN_ID_CACHE_TTL_SEC = 300

# ── Plan→symbol map published to Redis for the proxy (Option A) ──────────
# Key holding the authoritative symbol→plan_ids map (sourced from
# nt_symbol) so the proxy / Discord can authorize plan symbols without
# calling Supabase. Written by the splitter on startup + on invalidation.
# Proxy polls this key (with its own in-process TTL) and falls back to its
# static PLAN_SYMBOLS if the key is absent.
PROXY_PLAN_SYMBOLS_KEY = "proxy:plan_symbols"
PROXY_PLAN_SYMBOLS_TTL_SEC = 3600  # 1h safety-net expiry (not a rewrite timer)
# Pub/sub channel used to tell proxies to refresh immediately after a rewrite.
PROXY_PLAN_SYMBOLS_INVALIDATE_CH = "proxy:plan_symbols:invalidate"


# ── Data classes ────────────────────────────────────────────────────────


@dataclass
class PlanStreamTarget:
    """A target stream + the plan_id it represents."""
    stream: str
    plan_id: str
    plan_name: str  # for logs only


@dataclass
class SplitStats:
    """Running stats for /health and observability."""
    consumed: int = 0
    republished_scout: int = 0
    republished_precision: int = 0
    republished_both_failopen: int = 0
    errors: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    last_consumed_at: Optional[float] = None
    last_republished_at: Optional[float] = None


# ── Stream Splitter ─────────────────────────────────────────────────────


class StreamSplitter:
    """Background task that consumes the raw signal stream and republishes
    to per-plan streams based on `nt_symbol.plan_ids` membership.

    Singleton — one per process. Started by the FastAPI lifespan handler.
    """

    def __init__(self) -> None:
        self._redis: Optional[redis.Redis] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stopping = False

        # In-process cache: symbol → (set of plan_ids, expiry_monotonic)
        self._symbol_plan_cache: Dict[str, Tuple[Set[str], float]] = {}
        self._cache_lock = asyncio.Lock()

        # Guard for reconnecting the Redis client (a dropped Railway
        # connection must not be retried on a dead socket).
        self._reconnect_lock = asyncio.Lock()

        # Supabase client (lazy — only created if plan UUIDs are configured)
        self._supabase_client = None

        # Stats — exposed via /health
        self.stats = SplitStats()

        # Plan targets — resolved from settings / public.plans at start() time
        self._targets: list[PlanStreamTarget] = []
        # Cache for the startup plan-UUID lookup from public.plans
        self._plan_id_cache: Optional[Tuple[dict, float]] = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background consumer task."""
        if self._running:
            return
        if not settings.NOBLE_TRADER_STREAM_SPLIT_ENABLED:
            log.info(
                "StreamSplitter: disabled via NOBLE_TRADER_STREAM_SPLIT_ENABLED=false. "
                "Signals will only be published to the raw stream."
            )
            return

        redis_url = settings.redis_url_effective
        if not redis_url:
            log.warning(
                "StreamSplitter: no Redis URL configured — splitter will not start. "
                "Signals will only be published to the raw stream."
            )
            return

        # Resolve plan targets (explicit env override, else public.plans lookup).
        self._targets = await self._resolve_plan_targets()
        if not self._targets:
            log.warning(
                "StreamSplitter: no plan UUIDs configured "
                "(NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID / NOBLE_TRADER_PLAN_PRECISION_PRO_ID). "
                "Falling back to fail-open: every signal will be published to BOTH plan streams."
            )

        self._redis = redis.from_url(
            redis_url,
            decode_responses=True,
            # Reuse-or-reconnect: ping the connection before handing it out
            # so a connection the Railway proxy silently dropped (idle/blocked
            # reads time out) is discarded instead of raising mid-command.
            health_check_interval=30,
            # Retry once on transient timeouts rather than killing the loop.
            retry_on_timeout=True,
            # Bound connection setup; the blocking XREADGROUP uses BLOCK_MS,
            # socket_timeout MUST exceed BLOCK_MS (30s): a blocked XREADGROUP
            # waits up to BLOCK_MS for data, and redis-py applies socket_timeout
            # to that read. A smaller socket_timeout makes every idle read raise
            # TimeoutError -> reconnect loop. Buffer of 10s covers jitter.
            socket_connect_timeout=10,
            socket_timeout=40,
            # Auto-reconnect at the TCP layer on dropped links.
            retry=Retry(ConstantBackoff(1), 3),
        )

        # Idempotently create the consumer group on the raw stream.
        # MKSTREAM=False because the orchestrator must already be publishing
        # (the stream should exist from the first XADD).
        try:
            await self._redis.xgroup_create(
                RAW_STREAM, CONSUMER_GROUP, id="0", mkstream=False
            )
            log.info(
                "StreamSplitter: created consumer group '%s' on stream '%s'",
                CONSUMER_GROUP, RAW_STREAM,
            )
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                log.debug(
                    "StreamSplitter: consumer group '%s' already exists — reusing.",
                    CONSUMER_GROUP,
                )
            else:
                raise

        self._running = True
        self._task = asyncio.create_task(self._run(), name="stream_splitter")
        log.info(
            "StreamSplitter: started (consumer=%s, targets=%d, block_ms=%d)",
            CONSUMER_NAME, len(self._targets), BLOCK_MS,
        )

        # Option A: publish the authoritative plan→symbol map to Redis for
        # the proxy / Discord (avoids each consumer hitting Supabase).
        # Fire-and-forget; failures are logged, never block startup.
        asyncio.create_task(self._publish_plan_symbols_to_redis())

    async def _reconnect(self) -> bool:
        """Close and recreate the Redis client. Returns True if reconnected.

        Called when a connection-class error (TimeoutError / ConnectionError)
        is detected in the consumer loop — retrying on a dead socket in the
        pool would time out forever. Guarded by a lock so concurrent errors
        don't open duplicate clients.
        """
        async with self._reconnect_lock:
            if self._redis is not None:
                try:
                    await self._redis.aclose()
                except Exception:
                    pass
                self._redis = None
            redis_url = settings.redis_url_effective
            if not redis_url:
                return False
            self._redis = redis.from_url(
                redis_url,
                decode_responses=True,
                health_check_interval=30,
                retry_on_timeout=True,
                socket_connect_timeout=10,
                socket_timeout=10,
                retry=Retry(ConstantBackoff(1), 3),
            )
            # Re-create the consumer group if the stream still exists.
            try:
                await self._redis.xgroup_create(
                    RAW_STREAM, CONSUMER_GROUP, id="0", mkstream=False
                )
            except redis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            return True

    async def stop(self) -> None:
        """Stop the background consumer task gracefully."""
        if not self._running:
            return

        log.info("StreamSplitter: stopping...")
        self._stopping = True
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                log.warning("StreamSplitter: stop timed out after 5s — task may still be running")

        if self._redis:
            await self._redis.aclose()
            self._redis = None

        log.info("StreamSplitter: stopped")

    # ── Consumer loop ──────────────────────────────────────────────────

    async def _run(self) -> None:
        """Main consumer loop — XREADGROUP → resolve plans → XADD per plan.

        Resilient to the Railway proxy silently dropping idle/blocked
        connections: a connection-class error triggers a full client
        reconnect (the dead socket is discarded) rather than retrying on a
        broken pool connection. The broad Exception guard ensures the
        background task never dies — splitting always keeps running.
        """
        assert self._redis is not None
        log.info("StreamSplitter: consumer loop started (consumer=%s)", CONSUMER_NAME)

        backoff = 1.0
        while self._running:
            try:
                # XREADGROUP with block=BLOCK_MS — returns [] on timeout,
                # which is the normal idle case.
                response = await self._redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={RAW_STREAM: ">"},
                    count=50,            # process up to 50 signals per batch
                    block=BLOCK_MS,
                )

                if not response:
                    # Normal idle timeout — reset backoff and continue.
                    backoff = 1.0
                    continue

                for _stream, entries in response:
                    for entry_id, fields in entries:
                        await self._process_entry(entry_id, fields)

            except asyncio.CancelledError:
                log.info("StreamSplitter: consumer loop cancelled")
                break

            except (RedisConnectionError, RedisTimeoutError) as e:
                # The Railway proxy dropped the connection. Reconnect on a
                # fresh socket instead of retrying the dead one, with capped
                # exponential backoff so we don't hammer Redis while it's down.
                self.stats.errors += 1
                log.warning(
                    "StreamSplitter: Redis connection error (%s) — reconnecting "
                    "(backoff=%.1fs)", type(e).__name__, backoff,
                )
                if self._redis is not None:
                    await self._reconnect()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

            except Exception as e:
                # Any other unexpected error: count it, log it, and keep the
                # loop alive (never let the background task die).
                self.stats.errors += 1
                log.error("StreamSplitter: consumer loop error: %s", e, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        log.info("StreamSplitter: consumer loop exited")

    async def _process_entry(self, entry_id: str, fields: Dict[str, Any]) -> None:
        """Process one raw stream entry: resolve plans, republish, ACK."""
        assert self._redis is not None
        self.stats.consumed += 1
        self.stats.last_consumed_at = time.time()

        # The orchestrator publishes the signal as JSON in the "payload" field.
        # Some legacy entries may use "data" — handle both for backward compat.
        payload_str = fields.get("payload") or fields.get("data") or "{}"
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as e:
            log.warning(
                "StreamSplitter: malformed JSON in entry %s — ACKing to skip. error=%s",
                entry_id, e,
            )
            self.stats.errors += 1
            await self._redis.xack(RAW_STREAM, CONSUMER_GROUP, entry_id)
            return

        symbol = payload.get("symbol", "")
        if not symbol:
            log.warning("StreamSplitter: entry %s has no symbol — ACKing to skip", entry_id)
            await self._redis.xack(RAW_STREAM, CONSUMER_GROUP, entry_id)
            return

        # Resolve plan membership for this symbol.
        plan_ids = await self._resolve_symbol_plans(symbol)

        # Decide which plan streams to republish to.
        targets = self._select_targets(plan_ids)

        # Republish to each target stream.
        republished = []
        for target in targets:
            try:
                # Add a `plan_id` field so downstream consumers know which
                # plan this signal was routed to. The raw payload is preserved
                # in the `payload` field unchanged.
                enriched_fields = {
                    "payload": payload_str,
                    "plan_id": target.plan_id,
                    "plan_name": target.plan_name,
                    "split_at": str(int(time.time() * 1000)),
                    "source_entry_id": entry_id,
                }
                await self._redis.xadd(target.stream, enriched_fields, maxlen=10000, approximate=True)
                republished.append(target)
            except Exception as e:
                self.stats.errors += 1
                log.error(
                    "StreamSplitter: failed to XADD to %s for symbol %s: %s",
                    target.stream, symbol, e,
                )

        # Update stats
        if republished:
            self.stats.last_republished_at = time.time()
            if any(t.stream == STREAM_SIGNAL_SCOUT for t in republished):
                self.stats.republished_scout += 1
            if any(t.stream == STREAM_PRECISION_PRO for t in republished):
                self.stats.republished_precision += 1
            if len(republished) > 1:
                self.stats.republished_both_failopen += 1

        # ACK the raw stream entry — we've republished (or chosen not to).
        try:
            await self._redis.xack(RAW_STREAM, CONSUMER_GROUP, entry_id)
        except Exception as e:
            log.error("StreamSplitter: failed to XACK entry %s: %s", entry_id, e)

    # ── Plan resolution ────────────────────────────────────────────────

    async def _resolve_plan_targets(self) -> list[PlanStreamTarget]:
        """Build the list of (stream, plan_id) targets.

        Resolution priority per plan:
          1. Explicit env override (NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID /
             NOBLE_TRADER_PLAN_PRECISION_PRO_ID) — highest precedence.
          2. Auto-load from public.plans by title (Signal Scout /
             Precision Pro) via Supabase PostgREST.

        If a plan UUID cannot be resolved from either source, that plan's
        target is omitted. When NO targets resolve at all, the caller
        falls back to fail-open (publish to both streams) — see start().
        """
        targets: list[PlanStreamTarget] = []

        # 1) Explicit env overrides (may be partial — e.g. only one set).
        scout_id = settings.NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID or ""
        pro_id = settings.NOBLE_TRADER_PLAN_PRECISION_PRO_ID or ""

        # 2) Fill any missing UUIDs from public.plans (keyed by title).
        if not scout_id or not pro_id:
            try:
                resolved = await self._fetch_plan_ids_from_supabase()
            except Exception as e:  # fail-open: don't crash startup
                log.warning(
                    "StreamSplitter: could not auto-resolve plan UUIDs from "
                    "public.plans — %s. Env overrides (if any) still apply.",
                    e,
                )
                resolved = {}
            scout_id = scout_id or resolved.get(PLAN_TITLE_SIGNAL_SCOUT, "")
            pro_id = pro_id or resolved.get(PLAN_TITLE_PRECISION_PRO, "")

        src = "env override"
        if not (settings.NOBLE_TRADER_PLAN_SIGNAL_SCOUT_ID
                and settings.NOBLE_TRADER_PLAN_PRECISION_PRO_ID):
            src = "public.plans lookup"

        if scout_id:
            targets.append(PlanStreamTarget(
                stream=STREAM_SIGNAL_SCOUT,
                plan_id=scout_id,
                plan_name="Signal Scout",
            ))
        if pro_id:
            targets.append(PlanStreamTarget(
                stream=STREAM_PRECISION_PRO,
                plan_id=pro_id,
                plan_name="Precision Pro",
            ))

        if targets:
            log.info(
                "StreamSplitter: resolved %d plan target(s) via %s: %s",
                len(targets), src,
                ", ".join(f"{t.plan_name}={t.plan_id[:8]}…" for t in targets),
            )
        return targets

    async def _fetch_plan_ids_from_supabase(self) -> dict[str, str]:
        """Fetch plan UUIDs from public.plans, keyed by title.

        Returns a dict {plan_title: plan_id}. Only returns plans whose
        titles match the two known plan names. Raises on Supabase error so
        the caller can decide fail-open behavior.

        Uses a short in-process cache (PLAN_ID_CACHE_TTL_SEC) since the
        plan table rarely changes.
        """
        now = time.monotonic()
        if self._plan_id_cache is not None:
            cached, expires_at = self._plan_id_cache
            if now < expires_at:
                return cached

        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            raise RuntimeError("Supabase not configured")

        import httpx
        url = f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/plans"
        params = {
            "select": "id,title",
            "title": f"in.({PLAN_TITLE_SIGNAL_SCOUT},{PLAN_TITLE_PRECISION_PRO})",
        }
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        resolved: dict[str, str] = {}
        for row in data:
            title = row.get("title")
            pid = row.get("id")
            if title in (PLAN_TITLE_SIGNAL_SCOUT, PLAN_TITLE_PRECISION_PRO) and pid:
                resolved[title] = str(pid)

        self._plan_id_cache = (resolved, now + PLAN_ID_CACHE_TTL_SEC)
        return resolved

    async def _fetch_all_symbol_plans(self) -> dict[str, list[str]]:
        """Fetch every active symbol's plan_ids from nt_symbol in ONE query.

        Returns {symbol: [plan_uuid, ...]}. This replaces the per-symbol
        per-5-min Supabase chatter: the splitter builds the authoritative
        plan→symbol map once and publishes it to Redis (proxy:plan_symbols)
        for downstream authorization, instead of each consumer hitting
        Supabase independently.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            raise RuntimeError("Supabase not configured")

        import httpx
        url = f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_symbol"
        params = {
            "select": "symbol,plan_ids",
            "active": "eq.true",
        }
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        result: dict[str, list[str]] = {}
        for row in data:
            sym = (row.get("symbol") or "").upper()
            pids = row.get("plan_ids") or []
            if sym:
                result[sym] = [str(p) for p in pids if p]
        return result

    async def _publish_plan_symbols_to_redis(self) -> None:
        """Publish the authoritative symbol→plan map to Redis (Option A).

        Builds {plan_slug: [symbols]} from nt_symbol and writes it to
        proxy:plan_symbols (TTL = PROXY_PLAN_SYMBOLS_TTL_SEC). Downstream
        consumers (proxy plan_resolver, Discord) read this key instead of
        calling Supabase. Falls back to no-op if Supabase/Redis unavailable
        so startup is never blocked.
        """
        if self._redis is None:
            return
        try:
            symbol_plans = await self._fetch_all_symbol_plans()
        except Exception as e:
            log.warning(
                "StreamSplitter: could not fetch nt_symbol plan map — "
                "proxy:plan_symbols not refreshed. error=%s", e,
            )
            return

        # Resolve plan UUID -> slug via public.plans lookup.
        try:
            title_to_uuid = await self._fetch_plan_ids_from_supabase()
        except Exception as e:
            log.warning(
                "StreamSplitter: could not resolve plan UUIDs — "
                "proxy:plan_symbols not refreshed. error=%s", e,
            )
            return
        uuid_to_slug = {
            uuid: ("signal_scout" if title == PLAN_TITLE_SIGNAL_SCOUT
                   else "precision_pro")
            for title, uuid in title_to_uuid.items()
        }

        # Build slug -> set(symbols).
        by_slug: dict[str, set[str]] = {"signal_scout": set(), "precision_pro": set()}
        for sym, pids in symbol_plans.items():
            for pid in pids:
                slug = uuid_to_slug.get(pid)
                if slug:
                    by_slug[slug].add(sym)

        payload = {slug: sorted(syms) for slug, syms in by_slug.items()}
        try:
            await self._redis.set(
                PROXY_PLAN_SYMBOLS_KEY,
                json.dumps(payload),
                ex=PROXY_PLAN_SYMBOLS_TTL_SEC,
            )
            # Tell any listening proxies to refresh their in-process cache now.
            await self._redis.publish(PROXY_PLAN_SYMBOLS_INVALIDATE_CH, "1")
            log.info(
                "StreamSplitter: published proxy:plan_symbols "
                "(scout=%d, pro=%d symbols)",
                len(payload["signal_scout"]), len(payload["precision_pro"]),
            )
        except Exception as e:
            log.warning(
                "StreamSplitter: failed to write proxy:plan_symbols — %s", e,
            )

    def _select_targets(self, plan_ids: Set[str]) -> list[PlanStreamTarget]:
        """Select which target streams to republish to, given plan membership.

        Fail-open policy: if no targets are configured OR plan_ids is empty
        (Supabase unreachable or symbol not found in cache), publish to ALL
        configured targets. This ensures the proxy still has the signal in
        its stream — the proxy's own plan_resolver is the canonical gate.
        """
        if not self._targets:
            return []  # nothing configured — proxy must read raw stream

        # If we couldn't resolve plan_ids (fail-open), publish to all.
        if not plan_ids:
            return list(self._targets)

        # Normal case: publish only to plans this symbol belongs to.
        selected = [t for t in self._targets if t.plan_id in plan_ids]
        return selected

    async def _resolve_symbol_plans(self, symbol: str) -> Set[str]:
        """Resolve the set of plan_ids for a symbol, with caching.

        Returns an empty set on cache miss + Supabase failure (fail-open).
        Returns the cached set on hit. The cache is in-process only —
        each process maintains its own copy. TTL is SYMBOL_PLAN_CACHE_TTL_SEC.
        """
        now = time.monotonic()

        # Check cache (fast path — no lock needed for reads; race tolerance
        # is acceptable since duplicate fetches are idempotent).
        cached = self._symbol_plan_cache.get(symbol)
        if cached is not None:
            plan_ids, expires_at = cached
            if now < expires_at:
                self.stats.cache_hits += 1
                return plan_ids
            # Expired — fall through to re-fetch.

        self.stats.cache_misses += 1

        # Fetch from Supabase via PostgREST.
        try:
            plan_ids = await self._fetch_plans_from_supabase(symbol)
        except Exception as e:
            log.warning(
                "StreamSplitter: Supabase fetch failed for symbol %s — fail-open. error=%s",
                symbol, e,
            )
            return set()

        # Update cache.
        async with self._cache_lock:
            self._symbol_plan_cache[symbol] = (plan_ids, now + SYMBOL_PLAN_CACHE_TTL_SEC)

        return plan_ids

    async def _fetch_plans_from_supabase(self, symbol: str) -> Set[str]:
        """Fetch nt_symbol.plan_ids for a symbol via PostgREST.

        Returns an empty set if the symbol is not in nt_symbol (fail-open —
        let the proxy decide). Returns the set of plan_id UUIDs as strings.
        """
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            log.debug(
                "StreamSplitter: Supabase not configured — fail-open for symbol %s",
                symbol,
            )
            return set()

        # Lazy-init httpx (avoid import-time hard dep)
        import httpx

        url = f"{settings.SUPABASE_URL.rstrip('/')}/rest/v1/nt_symbol"
        params = {
            "select": "plan_ids",
            "symbol": f"eq.{symbol}",
            "active": "eq.true",
            "limit": "1",
        }
        headers = {
            "apikey": settings.SUPABASE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        }

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        if not data:
            log.debug(
                "StreamSplitter: symbol %s not found in nt_symbol (active=true) — fail-open",
                symbol,
            )
            return set()

        plan_ids_raw = data[0].get("plan_ids") or []
        # PostgREST returns UUID[] as a list of strings.
        return {str(pid) for pid in plan_ids_raw if pid}

    # ── Cache management ───────────────────────────────────────────────

    async def invalidate_cache(self, symbol: Optional[str] = None) -> None:
        """Force-evict cache entries. Call after admin updates nt_symbol.plan_ids.

        If `symbol` is None, evicts the entire cache.
        """
        async with self._cache_lock:
            if symbol is None:
                self._symbol_plan_cache.clear()
                log.info("StreamSplitter: invalidated entire symbol→plan cache")
                # Admin likely curated nt_symbol.plan_ids — refresh the
                # authoritative proxy:plan_symbols map for downstream consumers.
                if self._redis is not None:
                    asyncio.create_task(self._publish_plan_symbols_to_redis())
            else:
                self._symbol_plan_cache.pop(symbol, None)
                log.info("StreamSplitter: invalidated cache for symbol %s", symbol)

    # ── Health ────────────────────────────────────────────────────────

    def health(self) -> Dict[str, Any]:
        """Return health/stats dict for /health endpoint."""
        return {
            "enabled": settings.NOBLE_TRADER_STREAM_SPLIT_ENABLED and self._running,
            "consumer_group": CONSUMER_GROUP,
            "consumer_name": CONSUMER_NAME,
            "raw_stream": RAW_STREAM,
            "targets": [
                {"stream": t.stream, "plan_id": t.plan_id, "plan_name": t.plan_name}
                for t in self._targets
            ],
            "cache_ttl_sec": SYMBOL_PLAN_CACHE_TTL_SEC,
            "cache_size": len(self._symbol_plan_cache),
            "stats": {
                "consumed": self.stats.consumed,
                "republished_scout": self.stats.republished_scout,
                "republished_precision": self.stats.republished_precision,
                "republished_both_failopen": self.stats.republished_both_failopen,
                "errors": self.stats.errors,
                "cache_hits": self.stats.cache_hits,
                "cache_misses": self.stats.cache_misses,
                "last_consumed_at": self.stats.last_consumed_at,
                "last_republished_at": self.stats.last_republished_at,
            },
        }


# ── Singleton ────────────────────────────────────────────────────────────

_stream_splitter: Optional[StreamSplitter] = None


async def get_stream_splitter() -> StreamSplitter:
    """Get or initialize the singleton StreamSplitter instance."""
    global _stream_splitter
    if _stream_splitter is None:
        _stream_splitter = StreamSplitter()
        await _stream_splitter.start()
    return _stream_splitter


def get_stream_splitter_sync() -> Optional[StreamSplitter]:
    """Return the singleton if initialized, else None. Does NOT start it."""
    return _stream_splitter
