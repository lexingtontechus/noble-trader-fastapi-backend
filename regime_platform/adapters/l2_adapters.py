"""
L2 Order Book Feed Adapters — Real-Time Depth Streams
═════════════════════════════════════════════════════════

Streams live Level-2 order book data from Alpaca (equities/crypto)
and Hyperliquid (crypto perps) into a unified ``L2Snapshot`` format.

Supported venues
────────────────
  • Alpaca       - IEX (5 levels, free) or SIP (10 levels, paid)
  • Hyperliquid  - Full order book depth (crypto perps)

Architecture
────────────
Each adapter is an async context manager that yields ``L2Snapshot``.
A shared ``L2FeedManager`` routes symbols to the correct adapter
based on the ``nt_symbol.data_source`` column.

Usage
─────
    # Alpaca equities
    async with AlpacaL2Adapter(["SPY", "QQQ"]) as feed:
        async for snap in feed.stream():
            print(snap.imbalance_ratio)

    # Hyperliquid crypto
    async with HyperliquidL2Adapter(["BTC", "ETH"]) as feed:
        async for snap in feed.stream():
            print(snap.imbalance_ratio)

Environment variables required
──────────────────────────────
  Alpaca:   ALPACA_API_KEY, ALPACA_SECRET_KEY (same as OHLCV feed)
  HL:       HL_WS_URL (default wss://api.hyperliquid.xyz/ws)

Install requirements
────────────────────
  pip install alpaca-py websockets
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator, List, Optional, Tuple

from regime_platform.core.settings import settings

log = logging.getLogger("regime.l2")


# ═══════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class L2Snapshot:
    """Unified order book depth snapshot from any venue.

    Attributes
    ----------
    symbol : str
        Trading symbol (e.g. "SPY", "BTC").
    timestamp : float
        Unix timestamp when the snapshot was captured.
    bids : list of (price, size) tuples
        Bid levels, sorted descending by price (best bid first).
    asks : list of (price, size) tuples
        Ask levels, sorted ascending by price (best ask first).
    source : str
        Venue identifier: ``"alpaca"`` or ``"hyperliquid"``.
    """
    symbol: str
    timestamp: float
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    source: str = ""

    @property
    def total_bid_volume(self) -> float:
        """Sum of all bid sizes."""
        return sum(s for _, s in self.bids)

    @property
    def total_ask_volume(self) -> float:
        """Sum of all ask sizes."""
        return sum(s for _, s in self.asks)

    @property
    def spread(self) -> float:
        """Best ask minus best bid."""
        if self.bids and self.asks:
            return self.asks[0][0] - self.bids[0][0]
        return 0.0

    @property
    def mid_price(self) -> float:
        """Midpoint of best bid and best ask."""
        if self.bids and self.asks:
            return (self.bids[0][0] + self.asks[0][0]) / 2.0
        return 0.0

    @property
    def spread_bps(self) -> float:
        """Spread in basis points of mid price."""
        mid = self.mid_price
        if mid > 0:
            return (self.spread / mid) * 10_000
        return 0.0

    @property
    def top_imbalance(self) -> float:
        """Imbalance at top-of-book only: (bid_vol - ask_vol) / (bid_vol + ask_vol)."""
        if not self.bids or not self.asks:
            return 0.0
        bv = self.bids[0][1]
        av = self.asks[0][1]
        total = bv + av
        if total == 0:
            return 0.0
        return (bv - av) / total

    def level_imbalance(self, level: int = 1) -> float:
        """Imbalance at a specific depth level (1-based)."""
        bid_idx = level - 1
        ask_idx = level - 1
        if bid_idx >= len(self.bids) or ask_idx >= len(self.asks):
            return 0.0
        bv = self.bids[bid_idx][1]
        av = self.asks[ask_idx][1]
        total = bv + av
        if total == 0:
            return 0.0
        return (bv - av) / total

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "source": self.source,
            "bids": [(round(p, 4), round(s, 4)) for p, s in self.bids[:5]],
            "asks": [(round(p, 4), round(s, 4)) for p, s in self.asks[:5]],
            "spread": round(self.spread, 4),
            "mid_price": round(self.mid_price, 4),
            "spread_bps": round(self.spread_bps, 2),
            "top_imbalance": round(self.top_imbalance, 4),
        }


# ═══════════════════════════════════════════════════════════════════════
# Abstract Base
# ═══════════════════════════════════════════════════════════════════════


class L2Adapter(ABC):
    """Async L2 order book feed adapter.

    Subclass and implement ``stream()`` — an async generator that yields
    ``L2Snapshot``. The base class provides reconnect logic.
    """

    MAX_RECONNECT_ATTEMPTS = 10
    BASE_BACKOFF_SECS = 1.0

    def __init__(self, symbols: List[str], reconnect: bool = True):
        self.symbols = symbols
        self.reconnect = reconnect
        self._running = False
        self._snapshot_count: int = 0

    @abstractmethod
    async def stream(self) -> AsyncGenerator[L2Snapshot, None]:
        """Yield live ``L2Snapshot`` objects. Raise to trigger reconnect."""
        ...

    async def run_with_reconnect(self) -> AsyncGenerator[L2Snapshot, None]:
        """Wrapper that retries ``stream()`` on exception with exponential backoff."""
        attempts = 0
        while self._running:
            try:
                attempts = 0
                async for snap in self.stream():
                    self._snapshot_count += 1
                    yield snap
            except Exception as exc:
                attempts += 1
                if attempts > self.MAX_RECONNECT_ATTEMPTS:
                    log.error(
                        "%s: max reconnect attempts reached. Stopping.",
                        self.__class__.__name__,
                    )
                    return
                wait = self.BASE_BACKOFF_SECS * (2 ** min(attempts, 8))
                log.warning(
                    "%s: disconnected (%s). Reconnecting in %.1fs (attempt %d)",
                    self.__class__.__name__, exc, wait, attempts,
                )
                await asyncio.sleep(wait)

    @property
    def stats(self) -> dict:
        return {
            "adapter": self.__class__.__name__,
            "symbols": self.symbols,
            "snapshots": self._snapshot_count,
        }


# ═══════════════════════════════════════════════════════════════════════
# Alpaca L2 Adapter
# ═══════════════════════════════════════════════════════════════════════


class AlpacaL2Adapter(L2Adapter):
    """Streams L2 order book snapshots from Alpaca Markets.

    Requires ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY`` in environment.

    Feed options:
      - ``"iex"`` (default) — 5 levels, free, ~100ms updates
      - ``"sip"`` — 10 levels, paid subscription, ~250ms updates

    Works for: US equities (SPY, AAPL, etc.) and Alpaca crypto pairs.

    Notes
    -----
    Alpaca's WebSocket data stream sends order book *snapshots* (full
    depth), not individual changes. Each message replaces the previous
    state — no order book reconstruction needed.
    """

    def __init__(
        self,
        symbols: list[str],
        feed: str = "iex",
        reconnect: bool = True,
    ):
        super().__init__(symbols, reconnect)
        self.feed = feed
        self._api_key = settings.ALPACA_API_KEY
        self._secret_key = settings.ALPACA_SECRET_KEY

    async def stream(self) -> AsyncGenerator[L2Snapshot, None]:
        """Stream order book snapshots from Alpaca WebSocket."""
        try:
            from alpaca.data.live import StockDataStream
            from alpaca.data.enums import DataFeed
        except ImportError:
            raise RuntimeError("alpaca-py not installed: pip install alpaca-py")

        if not self._api_key or not self._secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY env vars required for AlpacaL2Adapter"
            )

        queue: asyncio.Queue[L2Snapshot] = asyncio.Queue()

        wss = StockDataStream(
            api_key=self._api_key,
            secret_key=self._secret_key,
            feed=DataFeed.SIP if self.feed == "sip" else DataFeed.IEX,
        )

        async def _book_handler(book):
            """Convert alpaca-py order book → L2Snapshot."""
            try:
                bids = [(float(b.p), float(b.s)) for b in getattr(book, "bids", [])]
                asks = [(float(a.p), float(a.s)) for a in getattr(book, "asks", [])]
                # Alpaca sends bids descending, asks ascending — matches our model
                await queue.put(L2Snapshot(
                    symbol=book.symbol,
                    timestamp=book.timestamp.timestamp(),
                    bids=bids,
                    asks=asks,
                    source="alpaca",
                ))
            except Exception as e:
                log.warning("AlpacaL2Adapter: failed to parse order book: %s", e)

        for sym in self.symbols:
            wss.subscribe_orderbooks(_book_handler, sym)

        ws_task = asyncio.create_task(wss.run())
        try:
            while self._running:
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=60.0)
                    yield snap
                except asyncio.TimeoutError:
                    log.debug("AlpacaL2Adapter: heartbeat timeout, still connected")
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass


# ═══════════════════════════════════════════════════════════════════════
# Hyperliquid L2 Adapter
# ═══════════════════════════════════════════════════════════════════════


class HyperliquidL2Adapter(L2Adapter):
    """Streams L2 order book snapshots from Hyperliquid (crypto perps).

    Hyperliquid's WebSocket sends full order book snapshots on each
    update. Updates are ~200ms for active coins.

    Works for: BTC, ETH, SOL, AVAX, ARB, DOGE, LINK, XRP, WIF (and any
    other coin listed on Hyperliquid).

    Notes
    -----
    Hyperliquid L2 levels have two sides: "B" (bid) and "A" (ask).
    Each level has ``px`` (price) and ``sz`` (size).
    No API key required — the WebSocket is public.
    """

    def __init__(
        self,
        coins: list[str],
        ws_url: Optional[str] = None,
        reconnect: bool = True,
    ):
        super().__init__(coins, reconnect)
        self.coins = coins
        self._ws_url = ws_url or settings.HL_WS_URL or "wss://api.hyperliquid.xyz/ws"

    async def stream(self) -> AsyncGenerator[L2Snapshot, None]:
        """Stream order book snapshots from Hyperliquid WebSocket."""
        try:
            import websockets
        except ImportError:
            raise RuntimeError("websockets not installed: pip install websockets")

        async with websockets.connect(self._ws_url, ping_interval=30) as ws:
            # Subscribe to each coin
            for coin in self.coins:
                sub_msg = json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": coin},
                })
                await ws.send(sub_msg)
                log.debug("HyperliquidL2Adapter: subscribed to %s", coin)

            # Wait for subscription confirmations
            for _ in range(len(self.coins) * 2):
                try:
                    resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(resp)
                    if data.get("channel") == "subscriptionResponse":
                        log.info(
                            "HyperliquidL2Adapter: subscribed to %s",
                            data.get("data", {}).get("coin", "?"),
                        )
                except asyncio.TimeoutError:
                    log.warning("HyperliquidL2Adapter: timeout waiting for subscription ack")
                    break

            # Stream L2 snapshots
            async for raw in ws:
                if not self._running:
                    break
                try:
                    data = json.loads(raw)
                    if data.get("channel") != "l2Book":
                        continue

                    book = data.get("data", {})
                    coin = book.get("coin", "UNKNOWN")
                    levels = book.get("levels", [])

                    bids: List[Tuple[float, float]] = []
                    asks: List[Tuple[float, float]] = []

                    for level in levels:
                        side = level.get("side", "")
                        px = float(level.get("px", 0))
                        sz = float(level.get("sz", 0))
                        if side == "B":
                            bids.append((px, sz))
                        elif side == "A":
                            asks.append((px, sz))

                    # Sort bids descending (best first), asks ascending
                    bids.sort(key=lambda x: -x[0])
                    asks.sort(key=lambda x: x[0])

                    yield L2Snapshot(
                        symbol=coin,
                        timestamp=time.time(),
                        bids=bids,
                        asks=asks,
                        source="hyperliquid",
                    )
                except Exception as e:
                    log.warning("HyperliquidL2Adapter: failed to parse message: %s", e)
