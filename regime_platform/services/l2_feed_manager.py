"""
L2 Feed Manager — Symbol-to-Venue Routing & Lifecycle (v4)
═══════════════════════════════════════════════════════════════

Orchestrates the full L2 data pipeline:

  1. Maps each symbol to its venue (``nt_symbol.data_source``)
  2. Starts the correct WebSocket adapter (Alpaca / Hyperliquid)
  3. Feeds raw snapshots into the ``L2ImbalanceComputer``
  4. Exposes ``get_signal(symbol)`` for the EV engine to consume

Usage
─────
    mgr = L2FeedManager()

    # Start streaming L2 for a set of symbols
    await mgr.start(["SPY", "QQQ", "BTC", "ETH"])

    # EV engine calls this per-trade
    signal = mgr.get_signal("SPY")

    # Graceful shutdown
    await mgr.stop()

Symbol-to-Venue Resolution
───────────────────────────
Symbols are mapped to venues via the ``nt_symbol`` Supabase table:

    SPY  → data_source="alpaca"      → AlpacaL2Adapter
    BTC  → data_source="hyperliquid" → HyperliquidL2Adapter

If Supabase is not configured, the manager falls back to a
hardcoded mapping (see ``_DEFAULT_VENUE_MAP``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from regime_platform.adapters.l2_adapters import (
    AlpacaL2Adapter,
    HyperliquidL2Adapter,
    L2Adapter,
    L2Snapshot,
)
from regime_platform.services.l2_imbalance import L2ImbalanceComputer

log = logging.getLogger("regime.l2_feed_manager")

# ─── Default venue map (used when Supabase is unavailable) ───────────
# Maps symbol → venue for common symbols.
_DEFAULT_VENUE_MAP: Dict[str, str] = {
    # Alpaca equities
    "SPY": "alpaca", "QQQ": "alpaca", "AAPL": "alpaca", "AMZN": "alpaca",
    "GOOGL": "alpaca", "META": "alpaca", "MSFT": "alpaca", "NVDA": "alpaca",
    "TSLA": "alpaca", "GLD": "alpaca", "SLV": "alpaca", "TLT": "alpaca",
    # Hyperliquid crypto perps
    "BTC": "hyperliquid", "ETH": "hyperliquid", "SOL": "hyperliquid",
    "AVAX": "hyperliquid", "ARB": "hyperliquid", "DOGE": "hyperliquid",
    "LINK": "hyperliquid", "XRP": "hyperliquid", "WIF": "hyperliquid",
    # Alpaca crypto (BTC/USD, ETH/USD via Alpaca)
    "BTCUSD": "alpaca", "ETHUSD": "alpaca",
}


class L2FeedManager:
    """Manages L2 order book streams across venues.

    Parameters
    ----------
    enabled : bool
        Master switch. When ``False``, ``get_signal()`` returns ``None``
        and ``start()`` is a no-op.
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._computer = L2ImbalanceComputer()
        self._adapters: List[L2Adapter] = []
        self._tasks: List[asyncio.Task] = []
        self._symbol_map: Dict[str, str] = {}  # symbol → venue
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self, symbols: List[str]) -> None:
        """Start L2 streams for the given symbols.

        Each symbol is resolved to its venue, an adapter is created
        per venue, and background tasks begin streaming snapshots
        into the imbalance computer.

        Parameters
        ----------
        symbols : list of str
            Trading symbols to subscribe to (e.g. ``["SPY", "BTC"]``).
        """
        if not self._enabled or not symbols:
            log.info("L2FeedManager: disabled or no symbols — skipping start")
            return

        # Resolve venue for each symbol
        alpaca_symbols: List[str] = []
        hl_coins: List[str] = []
        hl_to_symbol: Dict[str, str] = {}  # HL coin name → trading symbol

        for sym in symbols:
            venue = await self._resolve_venue(sym)
            self._symbol_map[sym] = venue
            if venue == "alpaca":
                alpaca_symbols.append(sym)
            elif venue == "hyperliquid":
                # HL uses short coin names (BTC not BTCUSD)
                hl_coin = sym.replace("USD", "").replace("USDT", "")
                hl_coins.append(hl_coin)
                hl_to_symbol[hl_coin] = sym

        # Create adapters
        tasks = []
        if alpaca_symbols:
            log.info(
                "L2FeedManager: starting Alpaca adapter for %s",
                alpaca_symbols,
            )
            alpaca_adapter = AlpacaL2Adapter(alpaca_symbols)
            self._adapters.append(alpaca_adapter)
            tasks.append(asyncio.create_task(self._run_adapter(alpaca_adapter)))

        if hl_coins:
            log.info(
                "L2FeedManager: starting Hyperliquid adapter for %s",
                hl_coins,
            )
            hl_adapter = HyperliquidL2Adapter(hl_coins)
            self._adapters.append(hl_adapter)
            tasks.append(asyncio.create_task(self._run_hl_adapter(hl_adapter, hl_to_symbol)))

        if not tasks:
            log.warning("L2FeedManager: no adapters could be created for symbols: %s", symbols)
            return

        self._running = True
        self._tasks = tasks

    async def stop(self) -> None:
        """Gracefully stop all L2 adapters."""
        self._running = False
        for adapter in self._adapters:
            adapter._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._adapters.clear()
        log.info("L2FeedManager: stopped")

    # ── Signal consumer API (called by EV engine pipeline) ────────

    def get_signal(self, symbol: str) -> Optional[dict]:
        """Return the latest imbalance signal for a symbol.

        Called by the EV engine pipeline to get the ``external_signal``
        dict for a trade decision.

        Returns ``None`` (neutral) if:
          - L2 is disabled
          - No data has been received for this symbol yet
          - The symbol is not being tracked
        """
        if not self._enabled:
            return None
        return self._computer.get_latest_signal(symbol)

    def get_computer(self) -> L2ImbalanceComputer:
        """Return the underlying imbalance computer (for direct access)."""
        return self._computer

    # ── Internal ─────────────────────────────────────────────────

    async def _run_adapter(self, adapter: L2Adapter) -> None:
        """Run an adapter, feeding snapshots into the imbalance computer."""
        async with adapter:
            async for snap in adapter.run_with_reconnect():
                if not self._running:
                    break
                self._computer.compute(snap)

    async def _run_hl_adapter(
        self,
        adapter: HyperliquidL2Adapter,
        hl_to_symbol: Dict[str, str],
    ) -> None:
        """Run HL adapter with coin→symbol mapping.

        Hyperliquid uses short coin names (``BTC``) while the rest of
        the system uses trading symbols (``BTCUSD``). This method
        re-maps symbols before feeding them to the imbalance computer.
        """
        async with adapter:
            async for snap in adapter.run_with_reconnect():
                if not self._running:
                    break
                # Remap HL coin name → trading symbol
                if snap.symbol in hl_to_symbol:
                    snap.symbol = hl_to_symbol[snap.symbol]
                self._computer.compute(snap)

    async def _resolve_venue(self, symbol: str) -> str:
        """Resolve a symbol to its data source venue.

        Tries Supabase ``nt_symbol`` table first, falls back to
        hardcoded mapping.
        """
        # Try Supabase first
        try:
            from regime_platform.core.settings import settings as _settings
            if _settings.SUPABASE_URL and _settings.SUPABASE_KEY:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(
                        f"{_settings.SUPABASE_URL}/rest/v1/nt_symbol",
                        headers={
                            "apikey": _settings.SUPABASE_KEY,
                            "Authorization": f"Bearer {_settings.SUPABASE_KEY}",
                        },
                        params={
                            "symbol": f"eq.{symbol}",
                            "select": "data_source",
                            "limit": "1",
                        },
                    )
                    if resp.status_code == 200:
                        rows = resp.json()
                        if rows and rows[0].get("data_source"):
                            return str(rows[0]["data_source"])
        except Exception as e:
            log.debug("L2FeedManager: Supabase lookup failed for %s: %s", symbol, e)

        # Fallback to hardcoded map
        venue = _DEFAULT_VENUE_MAP.get(symbol, "alpaca")
        log.debug("L2FeedManager: %s → %s (fallback map)", symbol, venue)
        return venue

    # ── Status ───────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "symbols": dict(self._symbol_map),
            "adapters": [a.stats for a in self._adapters],
            "computer": self._computer.stats,
        }
