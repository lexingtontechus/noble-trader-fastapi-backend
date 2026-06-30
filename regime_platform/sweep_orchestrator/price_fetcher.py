"""
Price Candle Fetcher
════════════════════

Ported from supabase/functions/_shared/utils.ts.

Fetches OHLC close-price arrays from the appropriate data source per symbol:
  • HyperLiquid — crypto perps (BTC, ETH, SOL via HL info API)
  • Alpaca      — stocks/ETFs and crypto spot (AAPL, BTCUSD, etc.)
  • Lightning AI data proxy fallback — everything else (via yfinance)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("regime.sweep.prices")

# ─── Constants ────────────────────────────────────────────────────

SWEEP_CANDLE_HOURS = 2160          # 90 days × 24h
SWEEP_CANDLE_INTERVAL = "1h"
LIGHT_CANDLE_BARS = 200

HL_API_URL = "https://api.hyperliquid.xyz"
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Map internal symbols to Alpaca trading symbols
ALPACA_SYMBOL_MAP: dict[str, str] = {
    "BTCUSD": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "SOLUSD": "SOL/USD",
    "AAPL": "AAPL",
    "AMZN": "AMZN",
    "GOOGL": "GOOGL",
    "META": "META",
    "MSFT": "MSFT",
    "NVDA": "NVDA",
    "TSLA": "TSLA",
    "SPY": "SPY",
    "QQQ": "QQQ",
}


# ─── HyperLiquid ──────────────────────────────────────────────────


async def fetch_hl_candles(coin: str, hours: int = SWEEP_CANDLE_HOURS) -> list[float]:
    """Fetch hourly candle closes from HyperLiquid mainnet."""
    start_ms = int((__import__("time").time() - hours * 3600) * 1000)
    end_ms = int(__import__("time").time() * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "1h",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{HL_API_URL}/info", json=payload)
            resp.raise_for_status()
            candles = resp.json()
    except Exception as exc:
        logger.warning("HL candle fetch failed for %s: %s", coin, exc)
        return []

    if not isinstance(candles, list) or not candles:
        logger.warning("HL returned no candles for %s", coin)
        return []

    prices = []
    for c in candles:
        try:
            prices.append(float(c["c"]))
        except (KeyError, TypeError, ValueError):
            continue

    logger.info("HL %s: %d candles", coin, len(prices))
    return prices


# ─── Alpaca ───────────────────────────────────────────────────────


async def fetch_alpaca_candles(
    symbol: str,
    alpaca_symbol: str,
    hours: int = SWEEP_CANDLE_HOURS,
    interval: str = "1h",
) -> list[float]:
    """Fetch hourly candle closes from Alpaca Markets.

    Requires ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY`` env vars
    (already configured in the backend settings).
    """
    from regime_platform.core.settings import settings

    api_key = settings.ALPACA_API_KEY
    secret_key = settings.ALPACA_SECRET_KEY
    if not api_key or not secret_key:
        logger.warning("Alpaca credentials not configured — cannot fetch %s", symbol)
        return []

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }

    _dt = __import__("datetime")
    _tz = _dt.timezone.utc
    start = _dt.datetime.fromtimestamp(
        __import__("time").time() - hours * 3600, tz=_tz
    ).isoformat()
    end = _dt.datetime.fromtimestamp(
        __import__("time").time() - 16 * 60, tz=_tz  # 16 min buffer for free tier
    ).isoformat()

    is_crypto = "/" in alpaca_symbol
    timeframe = "1Hour" if interval == "1h" else "1Day"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if is_crypto:
                url = f"{ALPACA_DATA_URL}/v1beta3/crypto/us/bars"
                params = {
                    "symbols": alpaca_symbol,
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                    "limit": 10000,
                }
            else:
                url = (
                    f"{ALPACA_DATA_URL}/v2/stocks/{alpaca_symbol}/bars"
                )
                params = {
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                    "limit": 10000,
                    "adjustment": "raw",
                }

            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        if is_crypto:
            bars = (data.get("bars") or {}).get(alpaca_symbol, [])
        else:
            bars = data.get("bars", [])

        if not bars:
            logger.warning("Alpaca returned no bars for %s", alpaca_symbol)
            return []

        prices = [float(b["c"]) for b in bars if b.get("c") is not None]
        logger.info("Alpaca %s (%s): %d candles", symbol, alpaca_symbol, len(prices))
        return prices

    except Exception as exc:
        logger.warning("Alpaca candle fetch failed for %s: %s", alpaca_symbol, exc)
        return []


# ─── Lightning AI / yfinance proxy ────────────────────────────────


async def fetch_proxy_candles(
    ticker: str,
    range_str: str = "90d",
    interval: str = "1h",
) -> list[float]:
    """Fetch price candles via the Noble Trader price proxy endpoint.

    Falls back to yfinance internally.  This is the same endpoint the
    old edge functions called via ``fetchLightningAICandles()``.
    """
    from regime_platform.core.settings import settings

    # Prefer the proxy URL, fall back to localhost
    proxy_base = (
        getattr(settings, "NOBLE_TRADER_URL", None)
        or "http://127.0.0.1:8001"
    )
    url = f"{proxy_base}/prices/{ticker}?range={range_str}&interval={interval}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, list):
            prices = [float(p) for p in data if isinstance(p, (int, float))]
        elif isinstance(data, dict):
            prices = [float(p) for p in data.get("prices", []) if isinstance(p, (int, float))]
        else:
            prices = []

        logger.info("Proxy %s: %d candles", ticker, len(prices))
        return prices

    except Exception as exc:
        logger.warning("Proxy candle fetch failed for %s: %s", ticker, exc)
        return []


# ─── Dispatcher ───────────────────────────────────────────────────


async def fetch_sweep_candles(sym: dict[str, Any]) -> list[float]:
    """Fetch a full sweep history (90d hourly) for a symbol.

    Routes to the appropriate data source based on ``sym["data_source"]``.
    """
    ds = sym.get("data_source", "lightningai")

    if ds == "hyperliquid":
        coin = sym.get("hyperliquid_coin") or sym["symbol"]
        return await fetch_hl_candles(coin, SWEEP_CANDLE_HOURS)

    if ds == "alpaca":
        alpaca_sym = (
            sym.get("alpaca_symbol")
            or ALPACA_SYMBOL_MAP.get(sym["symbol"])
            or sym["symbol"]
        )
        return await fetch_alpaca_candles(sym["symbol"], alpaca_sym, SWEEP_CANDLE_HOURS)

    # Fallback: Lightning AI data proxy (yfinance)
    ticker = sym.get("yahoo_ticker") or sym["symbol"]
    return await fetch_proxy_candles(ticker, "90d", "1h")


async def fetch_latest_candles(
    sym: dict[str, Any],
    n_bars: int = LIGHT_CANDLE_BARS,
) -> list[float]:
    """Fetch the latest ``n_bars`` hourly candles for a symbol."""
    ds = sym.get("data_source", "lightningai")

    if ds == "hyperliquid":
        coin = sym.get("hyperliquid_coin") or sym["symbol"]
        hours = min(n_bars, 500)
        return await fetch_hl_candles(coin, hours)

    if ds == "alpaca":
        alpaca_sym = (
            sym.get("alpaca_symbol")
            or ALPACA_SYMBOL_MAP.get(sym["symbol"])
            or sym["symbol"]
        )
        is_crypto = "/" in alpaca_sym
        adjusted_hours = (
            min(n_bars, 500)
            if is_crypto
            else min(n_bars * 4, 2000)
        )
        return await fetch_alpaca_candles(sym["symbol"], alpaca_sym, adjusted_hours)

    ticker = sym.get("yahoo_ticker") or sym["symbol"]
    range_str = "5d" if n_bars <= 390 else "90d"
    return await fetch_proxy_candles(ticker, range_str, "1h")
