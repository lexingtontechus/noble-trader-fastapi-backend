"""
AlpacaStreamManager — Per-user Alpaca WebSocket streaming for real-time P&L.

Manages Alpaca trade and data WebSocket connections for authenticated users.
Pushes position updates, price ticks, P&L snapshots, and account updates
to SSE consumers via asyncio queues.

Architecture:
  ┌─────────────────────────────────────────────────────┐
  │  AlpacaStreamManager (singleton)                     │
  │                                                      │
  │  Per-user streams:                                   │
  │  ├─ trade_ws  (wss://api.alpaca.markets/stream)     │
  │  │   → fills, account updates, position changes      │
  │  ├─ data_ws   (wss://stream.data.alpaca.markets/..) │
  │  │   → real-time quotes/trades for held positions    │
  │  │                                                    │
  │  Fan-out:                                            │
  │  └─ asyncio.Queue per SSE consumer                   │
  │     → position_update, price_tick, pnl_snapshot,     │
  │       account_update events                          │
  └─────────────────────────────────────────────────────┘

Credential Resolution:
  1. Supabase credentials table (per-user, encrypted)
  2. Global ALPACA_API_KEY / ALPACA_SECRET_KEY env vars (fallback)

Lifecycle:
  - start(user_id): resolves credentials, opens WS connections
  - subscribe(user_id): creates a queue for SSE consumer
  - unsubscribe(user_id, queue): removes queue, stops if no consumers
  - stop(user_id): closes all connections for a user
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.settings import settings
from ..core.runtime_config import runtime_config

log = logging.getLogger("regime.alpaca_stream")

# ─── Event Types ─────────────────────────────────────────────────────────────

@dataclass
class PnlEvent:
    """Base class for P&L SSE events."""
    event: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"event": self.event, "timestamp": self.timestamp, "data": {}}

    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict())}\n\n"


@dataclass
class PositionUpdateEvent(PnlEvent):
    """Pushed when a position changes (fill, partial fill, adjustment)."""
    event: str = "position_update"
    symbol: str = ""
    qty: str = "0"
    side: str = "long"
    avg_entry_price: str = "0"
    current_price: str = "0"
    market_value: str = "0"
    unrealized_pl: str = "0"
    unrealized_plpc: str = "0"

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "data": {
                "symbol": self.symbol,
                "qty": self.qty,
                "side": self.side,
                "avg_entry_price": self.avg_entry_price,
                "current_price": self.current_price,
                "market_value": self.market_value,
                "unrealized_pl": self.unrealized_pl,
                "unrealized_plpc": self.unrealized_plpc,
            },
        }


@dataclass
class PriceTickEvent(PnlEvent):
    """Pushed on every real-time quote/trade for a held position."""
    event: str = "price_tick"
    symbol: str = ""
    price: str = "0"
    bid: str = "0"
    ask: str = "0"
    volume: int = 0

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "data": {
                "symbol": self.symbol,
                "price": self.price,
                "bid": self.bid,
                "ask": self.ask,
                "volume": self.volume,
            },
        }


@dataclass
class PnlSnapshotEvent(PnlEvent):
    """Periodic aggregate P&L snapshot (every 5s)."""
    event: str = "pnl_snapshot"
    total_unrealized_pnl: float = 0.0
    total_unrealized_pnl_pc: float = 0.0
    total_market_value: float = 0.0
    day_pnl: float = 0.0
    day_pnl_pc: float = 0.0
    positions_count: int = 0

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "data": {
                "total_unrealized_pnl": self.total_unrealized_pnl,
                "total_unrealized_pnl_pc": self.total_unrealized_pnl_pc,
                "total_market_value": self.total_market_value,
                "day_pnl": self.day_pnl,
                "day_pnl_pc": self.day_pnl_pc,
                "positions_count": self.positions_count,
            },
        }


@dataclass
class AccountUpdateEvent(PnlEvent):
    """Pushed when account equity/cash changes."""
    event: str = "account_update"
    equity: str = "0"
    cash: str = "0"
    buying_power: str = "0"
    long_market_value: str = "0"
    short_market_value: str = "0"
    last_equity: str = "0"

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "data": {
                "equity": self.equity,
                "cash": self.cash,
                "buying_power": self.buying_power,
                "long_market_value": self.long_market_value,
                "short_market_value": self.short_market_value,
                "last_equity": self.last_equity,
            },
        }


@dataclass
class PnlAlertEvent(PnlEvent):
    """Pushed when a P&L alert threshold is breached."""
    event: str = "pnl_alert"
    alert_id: str = ""
    metric: str = ""
    operator: str = ""
    threshold_value: float = 0.0
    current_value: float = 0.0
    severity: str = "warning"
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "data": {
                "alert_id": self.alert_id,
                "metric": self.metric,
                "operator": self.operator,
                "threshold_value": self.threshold_value,
                "current_value": self.current_value,
                "severity": self.severity,
                "message": self.message,
            },
        }


@dataclass
class CredentialsErrorEvent(PnlEvent):
    """Pushed when Alpaca credentials fail (expired, revoked, or invalid)."""
    event: str = "credentials_error"
    reason: str = ""
    stream_type: str = ""  # "trade" or "data"

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "timestamp": self.timestamp,
            "data": {
                "reason": self.reason,
                "stream_type": self.stream_type,
            },
        }


# ─── Per-User Stream State ───────────────────────────────────────────────────

@dataclass
class UserStream:
    """Holds the state for one user's Alpaca streaming connections."""
    user_id: str
    api_key: str
    secret_key: str
    base_url: str  # paper or live

    # Fan-out queues — one per SSE consumer
    queues: list[asyncio.Queue] = field(default_factory=list)

    # Background tasks
    trade_task: Optional[asyncio.Task] = None
    data_task: Optional[asyncio.Task] = None
    snapshot_task: Optional[asyncio.Task] = None

    # Current positions cache (symbol → position dict)
    positions_cache: dict[str, dict] = field(default_factory=dict)

    # Last account snapshot
    account_cache: dict = field(default_factory=dict)

    # Running flag
    running: bool = False

    # Tracked symbols in data stream (to detect when re-subscribe is needed)
    data_subscribed_symbols: set[str] = field(default_factory=set)

    # Signal to data stream that it should re-subscribe
    data_resubscribe_event: asyncio.Event = field(default_factory=asyncio.Event)

    def add_queue(self, maxsize: int = 0) -> asyncio.Queue:
        if maxsize <= 0:
            # Will be set from runtime_config at call site
            maxsize = 500
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.queues.append(q)
        return q

    def remove_queue(self, q: asyncio.Queue) -> None:
        if q in self.queues:
            self.queues.remove(q)

    async def broadcast(self, event: PnlEvent) -> None:
        """Push an event to all connected SSE consumers."""
        sse_line = event.to_sse()
        for q in self.queues:
            try:
                q.put_nowait(sse_line)
            except asyncio.QueueFull:
                # Drop oldest and push new
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(sse_line)
                except asyncio.QueueFull:
                    pass


# ─── Credential Resolution ───────────────────────────────────────────────────

async def resolve_alpaca_credentials(user_id: str, org_id: str = None) -> Optional[tuple[str, str, str]]:
    """
    Resolve Alpaca API credentials for a user (optionally within an org context).

    Priority:
      1. Supabase credentials table — org-level keys (if org_id provided)
      2. Supabase credentials table — user-level keys (per-user, encrypted)
      3. Global ALPACA_API_KEY / ALPACA_SECRET_KEY env vars (fallback)

    Returns:
      (api_key, secret_key, base_url) or None if no keys found.
    """
    # Try Supabase — org-level credentials first
    if settings.SUPABASE_URL and settings.SUPABASE_KEY and org_id:
        try:
            import httpx
            supabase_url = f"{settings.SUPABASE_URL}/rest/v1/credentials"
            headers = {
                "apikey": settings.SUPABASE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                "Content-Type": "application/json",
            }
            params = {
                "select": "api_key,secret_key,credential_type",
                "org_id": f"eq.{org_id}",
                "is_valid": "eq.true",
                "order": "credential_type.asc",  # paper first, live second
                "limit": "1",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(supabase_url, headers=headers, params=params)
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows:
                        row = rows[0]
                        api_key = row.get("api_key", "")
                        secret_key = row.get("secret_key", "")
                        if api_key and secret_key:
                            cred_type = row.get("credential_type", "paper")
                            base_url = (
                                settings.ALPACA_LIVE_BASE_URL
                                if cred_type == "live"
                                else settings.ALPACA_PAPER_BASE_URL
                            )
                            log.info(f"Resolved org-level Alpaca credentials for org {org_id} via Supabase ({cred_type})")
                            return (api_key, secret_key, base_url)
        except Exception as e:
            log.warning(f"Supabase org-level credential lookup failed for org {org_id}: {e}")

    # Try Supabase — user-level credentials
    if settings.SUPABASE_URL and settings.SUPABASE_KEY:
        try:
            import httpx
            supabase_url = f"{settings.SUPABASE_URL}/rest/v1/credentials"
            headers = {
                "apikey": settings.SUPABASE_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_KEY}",
                "Content-Type": "application/json",
            }
            params = {
                "select": "api_key,secret_key,credential_type",
                "user_id": f"eq.{user_id}",
                "is_valid": "eq.true",
                "order": "credential_type.asc",  # paper first, live second
                "limit": "1",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(supabase_url, headers=headers, params=params)
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows:
                        row = rows[0]
                        api_key = row.get("api_key", "")
                        secret_key = row.get("secret_key", "")
                        if api_key and secret_key:
                            cred_type = row.get("credential_type", "paper")
                            base_url = (
                                settings.ALPACA_LIVE_BASE_URL
                                if cred_type == "live"
                                else settings.ALPACA_PAPER_BASE_URL
                            )
                            log.info(f"Resolved Alpaca credentials for user {user_id} via Supabase ({cred_type})")
                            return (api_key, secret_key, base_url)
        except Exception as e:
            log.warning(f"Supabase credential lookup failed for user {user_id}: {e}")

    # Fallback: global env vars
    if settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY:
        log.info(f"Using global Alpaca credentials for user {user_id}")
        return (
            settings.ALPACA_API_KEY,
            settings.ALPACA_SECRET_KEY,
            settings.ALPACA_BASE_URL,
        )

    log.warning(f"No Alpaca credentials found for user {user_id}")
    return None


# ─── AlpacaStreamManager ─────────────────────────────────────────────────────

class AlpacaStreamManager:
    """
    Manages per-user Alpaca WebSocket streaming connections.

    Singleton — one instance shared across all SSE consumers.
    Each authenticated user gets their own trade + data WebSocket connections.
    Events are fanned out to all SSE consumers for that user.
    """

    def __init__(self):
        self._streams: dict[str, UserStream] = {}  # user_id → UserStream
        self._lock = asyncio.Lock()

    async def start(self, user_id: str) -> Optional[UserStream]:
        """
        Start streaming for a user. Resolves credentials, opens connections.

        Returns the UserStream (or None if no credentials found).
        If already running, returns existing stream.
        """
        async with self._lock:
            if user_id in self._streams:
                stream = self._streams[user_id]
                if stream.running:
                    return stream
                # Stale entry — clean up
                await self._stop_stream(user_id)

        # Resolve credentials
        creds = await resolve_alpaca_credentials(user_id)
        if not creds:
            return None

        api_key, secret_key, base_url = creds
        stream = UserStream(
            user_id=user_id,
            api_key=api_key,
            secret_key=secret_key,
            base_url=base_url,
        )

        async with self._lock:
            self._streams[user_id] = stream

        # Bootstrap: fetch current positions/account via REST
        await self._bootstrap_positions(stream)

        # Start background tasks
        stream.running = True
        stream.trade_task = asyncio.create_task(
            self._run_trade_stream(stream), name=f"alpaca-trade-{user_id}"
        )
        stream.data_task = asyncio.create_task(
            self._run_data_stream(stream), name=f"alpaca-data-{user_id}"
        )
        stream.snapshot_task = asyncio.create_task(
            self._run_snapshot_loop(stream), name=f"alpaca-snapshot-{user_id}"
        )

        log.info(f"Started Alpaca streaming for user {user_id} (base_url={base_url})")
        return stream

    async def subscribe(self, user_id: str) -> Optional[asyncio.Queue]:
        """
        Subscribe to P&L events for a user. Returns an asyncio.Queue.

        If the user's stream isn't running yet, starts it automatically.
        Queue size is resolved from runtime_config (alpaca.sse_queue_size).
        """
        stream = self._streams.get(user_id)
        if stream is None or not stream.running:
            stream = await self.start(user_id)
            if stream is None:
                return None

        queue_size = await runtime_config.get_int("alpaca.sse_queue_size", default=500)
        return stream.add_queue(maxsize=queue_size)

    async def unsubscribe(self, user_id: str, q: asyncio.Queue) -> None:
        """Unsubscribe a queue. Stops the stream if no more consumers."""
        stream = self._streams.get(user_id)
        if stream is None:
            return

        stream.remove_queue(q)

        # If no more consumers, stop the stream after a grace period
        if not stream.queues:
            grace = await runtime_config.get_int("alpaca.stream_grace_period", default=30)
            log.info(f"No more SSE consumers for user {user_id} — scheduling stop in {grace}s")
            await asyncio.sleep(grace)  # grace period for reconnections
            stream = self._streams.get(user_id)
            if stream and not stream.queues:
                await self.stop(user_id)

    async def stop(self, user_id: str) -> None:
        """Stop all streaming connections for a user."""
        async with self._lock:
            await self._stop_stream(user_id)

    async def _stop_stream(self, user_id: str) -> None:
        """Internal: stop and clean up a user's stream."""
        stream = self._streams.pop(user_id, None)
        if stream is None:
            return

        stream.running = False
        for task in (stream.trade_task, stream.data_task, stream.snapshot_task):
            if task and not task.done():
                task.cancel()

        # Wait for tasks to finish
        tasks = [t for t in (stream.trade_task, stream.data_task, stream.snapshot_task) if t]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        log.info(f"Stopped Alpaca streaming for user {user_id}")

    def get_stream(self, user_id: str) -> Optional[UserStream]:
        """Get the UserStream for a user (if running)."""
        return self._streams.get(user_id)

    # ─── Bootstrap: fetch current state via REST ──────────────────────────

    async def _bootstrap_positions(self, stream: UserStream) -> None:
        """Fetch current positions and account data via Alpaca REST API (rate-throttled)."""
        try:
            import httpx
            from ..middleware.alpaca_throttle import get_alpaca_throttle

            account_url = f"{stream.base_url}/v2/account"
            positions_url = f"{stream.base_url}/v2/positions"

            headers = {
                "APCA-API-KEY-ID": stream.api_key,
                "APCA-API-SECRET-KEY": stream.secret_key,
            }

            throttle = get_alpaca_throttle()

            async with httpx.AsyncClient(timeout=10.0) as client:
                # Fetch account (rate-throttled)
                await throttle.acquire(timeout=10.0)
                acc_resp = await client.get(account_url, headers=headers)
                if acc_resp.status_code == 200:
                    stream.account_cache = acc_resp.json()
                    await stream.broadcast(AccountUpdateEvent(
                        equity=stream.account_cache.get("equity", "0"),
                        cash=stream.account_cache.get("cash", "0"),
                        buying_power=stream.account_cache.get("buying_power", "0"),
                        long_market_value=stream.account_cache.get("long_market_value", "0"),
                        short_market_value=stream.account_cache.get("short_market_value", "0"),
                        last_equity=stream.account_cache.get("last_equity", "0"),
                    ))

                # Fetch positions (rate-throttled)
                await throttle.acquire(timeout=10.0)
                pos_resp = await client.get(positions_url, headers=headers)
                if pos_resp.status_code == 200:
                    positions = pos_resp.json()
                    for pos in positions:
                        symbol = pos.get("symbol", "")
                        stream.positions_cache[symbol] = pos
                        await stream.broadcast(PositionUpdateEvent(
                            symbol=symbol,
                            qty=pos.get("qty", "0"),
                            side="long" if int(pos.get("qty", "0")) > 0 else "short",
                            avg_entry_price=pos.get("avg_entry_price", "0"),
                            current_price=pos.get("current_price", "0"),
                            market_value=pos.get("market_value", "0"),
                            unrealized_pl=pos.get("unrealized_pl", "0"),
                            unrealized_plpc=pos.get("unrealized_plpc", "0"),
                        ))

            log.info(f"Bootstrapped {len(stream.positions_cache)} positions for user {stream.user_id}")
        except Exception as e:
            log.warning(f"Bootstrap failed for user {stream.user_id}: {e}")

    # ─── Trade Stream ─────────────────────────────────────────────────────

    async def _run_trade_stream(self, stream: UserStream) -> None:
        """
        Connect to Alpaca trade WebSocket for fills and account updates.

        Uses raw websockets (not alpaca-py) for maximum control and
        minimal dependencies. The Alpaca trade stream protocol:
          1. Connect to wss://api.alpaca.markets/stream (paper or live)
          2. Send auth message: {"action": "auth", "key": ..., "secret": ...}
          3. Send listen message: {"action": "listen", "data": {"streams": [...]}}
          4. Receive JSON messages for: trade_updates, account_updates
        """
        try:
            import websockets
        except ImportError:
            log.error("websockets not installed — trade stream unavailable")
            return

        # Build WS URL
        if "paper" in stream.base_url:
            ws_url = "wss://paper-api.alpaca.markets/stream"
        else:
            ws_url = "wss://api.alpaca.markets/stream"

        # Resolve WS connection params from runtime_config
        max_reconnects = await runtime_config.get_int("alpaca.ws_max_reconnects", default=20)
        base_backoff = await runtime_config.get_float("alpaca.ws_base_backoff", default=1.0)
        ping_interval = await runtime_config.get_int("alpaca.ws_ping_interval", default=30)
        ping_timeout = await runtime_config.get_int("alpaca.ws_ping_timeout", default=10)

        reconnect_attempts = 0

        while stream.running and reconnect_attempts < max_reconnects:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                    close_timeout=5,
                ) as ws:
                    # Authenticate
                    auth_msg = json.dumps({
                        "action": "auth",
                        "key": stream.api_key,
                        "secret": stream.secret_key,
                    })
                    await ws.send(auth_msg)
                    auth_resp = json.loads(await ws.recv())
                    if auth_resp.get("data", {}).get("status") != "authorized":
                        log.error(f"Trade stream auth failed for user {stream.user_id}: {auth_resp}")
                        await stream.broadcast(CredentialsErrorEvent(
                            reason=f"Trade stream auth failed: {auth_resp.get('data', {}).get('status', 'unknown')}",
                            stream_type="trade",
                        ))
                        return

                    # Subscribe to trade updates and account updates
                    listen_msg = json.dumps({
                        "action": "listen",
                        "data": {
                            "streams": ["trade_updates", "account_updates"]
                        },
                    })
                    await ws.send(listen_msg)
                    listen_resp = json.loads(await ws.recv())
                    log.info(f"Trade stream connected for user {stream.user_id}: {listen_resp}")

                    reconnect_attempts = 0  # Reset on successful connect

                    # Receive loop
                    async for raw in ws:
                        if not stream.running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle_trade_message(stream, msg)
                        except Exception as e:
                            log.warning(f"Error handling trade message: {e}")

            except Exception as e:
                reconnect_attempts += 1
                wait = base_backoff * (2 ** min(reconnect_attempts, 8))
                log.warning(
                    f"Trade stream disconnected for user {stream.user_id}: {e}. "
                    f"Reconnecting in {wait:.1f}s (attempt {reconnect_attempts})"
                )
                await asyncio.sleep(wait)

        log.info(f"Trade stream stopped for user {stream.user_id}")

    async def _handle_trade_message(self, stream: UserStream, msg: dict) -> None:
        """Handle a message from the Alpaca trade stream."""
        stream_type = msg.get("stream", "")
        data = msg.get("data", {})

        if stream_type == "trade_updates":
            event_type = data.get("event", "")

            if event_type == "fill" or event_type == "partial_fill":
                # A fill occurred — update position cache and broadcast
                symbol = data.get("symbol", data.get("order", {}).get("symbol", ""))
                if symbol:
                    # Refresh position from REST API
                    await self._refresh_position(stream, symbol)
                    # Signal data stream to re-subscribe if new symbol
                    stream.data_resubscribe_event.set()

            elif event_type == "closed":
                # Position closed entirely
                symbol = data.get("symbol", data.get("order", {}).get("symbol", ""))
                if symbol and symbol in stream.positions_cache:
                    del stream.positions_cache[symbol]
                    await stream.broadcast(PositionUpdateEvent(
                        symbol=symbol,
                        qty="0",
                        side="long",
                    ))

        elif stream_type == "account_updates":
            # Account equity/cash changed
            await stream.broadcast(AccountUpdateEvent(
                equity=data.get("equity", "0"),
                cash=data.get("cash", "0"),
                buying_power=data.get("buying_power", "0"),
                long_market_value=data.get("long_market_value", "0"),
                short_market_value=data.get("short_market_value", "0"),
                last_equity=data.get("last_equity", "0"),
            ))
            # Update cache
            stream.account_cache.update(data)

    async def _refresh_position(self, stream: UserStream, symbol: str) -> None:
        """Refresh a single position from Alpaca REST API (rate-throttled)."""
        try:
            import httpx
            from ..middleware.alpaca_throttle import get_alpaca_throttle

            url = f"{stream.base_url}/v2/positions/{symbol}"
            headers = {
                "APCA-API-KEY-ID": stream.api_key,
                "APCA-API-SECRET-KEY": stream.secret_key,
            }

            # Acquire throttle token before the API call
            throttle = get_alpaca_throttle()
            acquired = await throttle.acquire(timeout=5.0)
            if not acquired:
                log.debug(f"Position refresh skipped for {symbol}: throttle timeout")
                return

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    pos = resp.json()
                    stream.positions_cache[symbol] = pos
                    await stream.broadcast(PositionUpdateEvent(
                        symbol=symbol,
                        qty=pos.get("qty", "0"),
                        side="long" if int(pos.get("qty", "0")) > 0 else "short",
                        avg_entry_price=pos.get("avg_entry_price", "0"),
                        current_price=pos.get("current_price", "0"),
                        market_value=pos.get("market_value", "0"),
                        unrealized_pl=pos.get("unrealized_pl", "0"),
                        unrealized_plpc=pos.get("unrealized_plpc", "0"),
                    ))
                elif resp.status_code == 404:
                    # Position closed
                    if symbol in stream.positions_cache:
                        del stream.positions_cache[symbol]
        except Exception as e:
            log.debug(f"Position refresh failed for {symbol}: {e}")

    # ─── Data Stream (Quotes/Trades) ──────────────────────────────────────

    async def _run_data_stream(self, stream: UserStream) -> None:
        """
        Connect to Alpaca data WebSocket for real-time quotes.

        Subscribes to quotes for all currently held positions.
        Dynamically re-subscribes when new positions are opened.
        Uses IEX feed (free tier) or SIP (paid).
        """
        try:
            import websockets
        except ImportError:
            log.error("websockets not installed — data stream unavailable")
            return

        # Build WS URL — IEX feed is free, SIP requires subscription
        if "paper" in stream.base_url:
            ws_url = "wss://stream.data.alpaca.markets/v2/iex"
        else:
            ws_url = "wss://stream.data.alpaca.markets/v2/iex"  # Use IEX for now

        # Resolve WS connection params from runtime_config
        max_reconnects = await runtime_config.get_int("alpaca.ws_max_reconnects", default=20)
        base_backoff = await runtime_config.get_float("alpaca.ws_base_backoff", default=1.0)
        ping_interval = await runtime_config.get_int("alpaca.ws_ping_interval", default=30)
        ping_timeout = await runtime_config.get_int("alpaca.ws_ping_timeout", default=10)

        reconnect_attempts = 0

        while stream.running and reconnect_attempts < max_reconnects:
            # Only connect if we have positions to watch
            symbols = list(stream.positions_cache.keys())
            if not symbols:
                # No positions — wait for a fill or new position
                stream.data_resubscribe_event.clear()
                try:
                    await asyncio.wait_for(
                        stream.data_resubscribe_event.wait(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=ping_interval,
                    ping_timeout=ping_timeout,
                    close_timeout=5,
                ) as ws:
                    # Authenticate
                    auth_msg = json.dumps({
                        "action": "auth",
                        "key": stream.api_key,
                        "secret": stream.secret_key,
                    })
                    await ws.send(auth_msg)
                    auth_resp = json.loads(await ws.recv())
                    if auth_resp.get("data", {}).get("status") != "authorized":
                        log.error(f"Data stream auth failed for user {stream.user_id}: {auth_resp}")
                        await stream.broadcast(CredentialsErrorEvent(
                            reason=f"Data stream auth failed: {auth_resp.get('data', {}).get('status', 'unknown')}",
                            stream_type="data",
                        ))
                        return

                    # Subscribe to quotes for held positions
                    subscribe_msg = json.dumps({
                        "action": "listen",
                        "data": {
                            "streams": [f"q.{sym}" for sym in symbols]
                        },
                    })
                    await ws.send(subscribe_msg)
                    listen_resp = json.loads(await ws.recv())
                    stream.data_subscribed_symbols = set(symbols)
                    log.info(f"Data stream connected for user {stream.user_id}, watching {symbols}")

                    reconnect_attempts = 0

                    # Throttle: only send 1 quote per symbol per second
                    last_sent: dict[str, float] = {}
                    min_interval = 1.0  # seconds between ticks per symbol

                    # Re-subscribe check interval
                    last_resubscribe_check = time.time()
                    resubscribe_interval = 2.0  # check every 2s

                    async for raw in ws:
                        if not stream.running:
                            break

                        # Dynamic re-subscription: check if new symbols need watching
                        now = time.time()
                        if now - last_resubscribe_check >= resubscribe_interval:
                            last_resubscribe_check = now
                            current_symbols = set(stream.positions_cache.keys())
                            new_symbols = current_symbols - stream.data_subscribed_symbols
                            removed_symbols = stream.data_subscribed_symbols - current_symbols

                            if new_symbols:
                                # Subscribe to new symbols (Alpaca allows incremental listen)
                                all_streams = [f"q.{sym}" for sym in current_symbols]
                                resub_msg = json.dumps({
                                    "action": "listen",
                                    "data": {"streams": all_streams},
                                })
                                await ws.send(resub_msg)
                                stream.data_subscribed_symbols = current_symbols
                                log.info(
                                    f"Data stream re-subscribed for user {stream.user_id}: "
                                    f"+{new_symbols} -{removed_symbols}, total={len(current_symbols)}"
                                )
                            elif removed_symbols:
                                # Positions removed — re-subscribe with current set
                                all_streams = [f"q.{sym}" for sym in current_symbols]
                                resub_msg = json.dumps({
                                    "action": "listen",
                                    "data": {"streams": all_streams},
                                })
                                await ws.send(resub_msg)
                                stream.data_subscribed_symbols = current_symbols
                                log.debug(
                                    f"Data stream pruned for user {stream.user_id}: "
                                    f"-{removed_symbols}, total={len(current_symbols)}"
                                )

                        try:
                            msg = json.loads(raw)
                            stream_key = msg.get("stream", "")
                            data = msg.get("data", {})

                            if stream_key.startswith("q."):
                                symbol = stream_key[2:]
                                last = last_sent.get(symbol, 0)
                                if now - last < min_interval:
                                    continue  # throttle
                                last_sent[symbol] = now

                                await stream.broadcast(PriceTickEvent(
                                    symbol=symbol,
                                    price=data.get("ap", data.get("bp", "0")),  # ask price or bid
                                    bid=data.get("bp", "0"),
                                    ask=data.get("ap", "0"),
                                    volume=0,  # quotes don't have volume
                                ))

                        except Exception as e:
                            log.debug(f"Error handling data message: {e}")

            except Exception as e:
                reconnect_attempts += 1
                wait = base_backoff * (2 ** min(reconnect_attempts, 8))
                log.warning(
                    f"Data stream disconnected for user {stream.user_id}: {e}. "
                    f"Reconnecting in {wait:.1f}s (attempt {reconnect_attempts})"
                )
                await asyncio.sleep(wait)

        log.info(f"Data stream stopped for user {stream.user_id}")

    # ─── Periodic Snapshot ────────────────────────────────────────────────

    async def _run_snapshot_loop(self, stream: UserStream) -> None:
        """
        Periodically compute and broadcast an aggregate P&L snapshot.

        Runs every 5 seconds while the stream is active.
        Provides a consistent aggregate view even if individual events
        are missed.

        Also evaluates P&L alert thresholds and broadcasts triggered alerts.
        """
        # Resolve snapshot interval from runtime_config
        snapshot_interval = await runtime_config.get_int("alpaca.snapshot_interval_sec", default=5)

        while stream.running:
            try:
                await asyncio.sleep(snapshot_interval)
                if not stream.running:
                    break

                # Compute from positions cache
                total_unrealized_pnl = 0.0
                total_market_value = 0.0
                positions_count = len(stream.positions_cache)

                for pos in stream.positions_cache.values():
                    total_unrealized_pnl += float(pos.get("unrealized_pl", 0))
                    total_market_value += float(pos.get("market_value", 0))

                total_unrealized_pnl_pc = (
                    (total_unrealized_pnl / (total_market_value - total_unrealized_pnl)) * 100
                    if total_market_value > 0
                    else 0.0
                )

                # Day P&L from account cache
                equity = float(stream.account_cache.get("equity", 0))
                last_equity = float(stream.account_cache.get("last_equity", 0))
                day_pnl = equity - last_equity
                day_pnl_pc = (day_pnl / last_equity * 100) if last_equity > 0 else 0.0

                await stream.broadcast(PnlSnapshotEvent(
                    total_unrealized_pnl=total_unrealized_pnl,
                    total_unrealized_pnl_pc=total_unrealized_pnl_pc,
                    total_market_value=total_market_value,
                    day_pnl=day_pnl,
                    day_pnl_pc=day_pnl_pc,
                    positions_count=positions_count,
                ))

                # ── Evaluate P&L alert thresholds ──
                try:
                    from .pnl_service import pnl_service
                    positions_list = list(stream.positions_cache.values())
                    triggered = await pnl_service.evaluate_and_broadcast(
                        user_id=stream.user_id,
                        account=stream.account_cache,
                        positions=positions_list,
                    )
                    for alert in triggered:
                        alert_event = PnlAlertEvent(
                            alert_id=alert["id"],
                            metric=alert["metric"],
                            operator=alert["operator"],
                            threshold_value=alert["threshold_value"],
                            current_value=alert["current_value"],
                            severity=alert["severity"],
                            message=alert["message"],
                        )
                        await stream.broadcast(alert_event)
                        log.info(f"P&L alert triggered for user {stream.user_id}: {alert['message']}")
                except Exception as e:
                    log.debug(f"Alert evaluation error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"Snapshot loop error: {e}")

    # ─── Status ───────────────────────────────────────────────────────────

    @property
    def active_users(self) -> list[str]:
        """List of user_ids with active streams."""
        return [uid for uid, s in self._streams.items() if s.running]

    def status(self) -> dict:
        """Return manager status for health checks."""
        return {
            "active_users": len(self.active_users),
            "users": [
                {
                    "user_id": uid,
                    "positions": len(s.positions_cache),
                    "consumers": len(s.queues),
                    "running": s.running,
                }
                for uid, s in self._streams.items()
            ],
        }


# ─── Singleton ────────────────────────────────────────────────────────────────

alpaca_stream_manager = AlpacaStreamManager()
