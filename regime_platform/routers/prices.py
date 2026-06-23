"""
Price data proxy endpoint for the Lightning AI data proxy.

Provides candle price data to the Supabase Edge Functions (sweep), replacing
direct Yahoo Finance scraping. The backend fetches prices using yfinance (or
the Yahoo chart API fallback) and returns clean close-price arrays.

Edge functions call: GET /prices/{ticker}?range=90d&interval=1h

This endpoint is intentionally unauthenticated — it is called internally by
Supabase Edge Functions (service-to-service). In production, restrict via
network policy (VPC / firewall) rather than per-request auth.
"""

from fastapi import APIRouter, Query, HTTPException
from typing import Optional
import logging

router = APIRouter(prefix="/prices", tags=["Price Data Proxy"])
log = logging.getLogger("regime.prices")


@router.get("/{ticker}", summary="Fetch close prices for a ticker")
async def get_prices(
    ticker: str,
    range: str = Query("90d", description="Lookback range (e.g. 5d, 1mo, 3mo, 6mo, 1y, 2y, 90d)"),
    interval: str = Query("1h", description="Bar interval (1h, 1d, etc.)"),
):
    """
    Return a JSON array of close prices for the given ticker, sorted oldest → newest.

    Uses yfinance (or Yahoo chart API fallback) internally. This is a temporary
    backend-side proxy that can be swapped to an institutional data feed without
    changing the edge function caller.

    Response format: { "prices": [float, ...], "symbol": str, "count": int }
    or a raw JSON array: [float, float, ...]
    """
    try:
        import yfinance as yf
        import numpy as np

        # Map common range aliases
        period_map = {
            "5d": "5d", "1mo": "1mo", "3mo": "3mo",
            "6mo": "6mo", "1y": "1y", "2y": "2y",
            "90d": "3mo", "45d": "2mo",
        }
        period = period_map.get(range, range)

        # Determine interval for yfinance
        yf_interval = interval.replace("h", "h").replace("d", "d")
        if yf_interval == "1h":
            # yfinance 1h requires period <= 730d (2y)
            yf_interval = "1h"
        elif yf_interval == "1d":
            yf_interval = "1d"
        else:
            yf_interval = "1h"

        # ── Primary: yfinance ──
        try:
            ticker_obj = yf.Ticker(ticker)
            hist = ticker_obj.history(period=period, interval=yf_interval)
            if not hist.empty:
                prices = hist["Close"].dropna().tolist()
                log.info(
                    f"yfinance: fetched {len(prices)} prices for {ticker} "
                    f"(period={period}, interval={yf_interval})"
                )
                return prices
        except Exception as e:
            log.warning(f"yfinance failed for {ticker}: {e}")

        # ── Fallback: Yahoo v8 chart API ──
        import httpx

        # Yahoo chart API uses different range format
        yahoo_range = range
        if yahoo_range == "90d":
            yahoo_range = "3mo"

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?range={yahoo_range}&interval={yf_interval}"
        )
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Yahoo chart API returned {resp.status_code} for {ticker}",
                )
            data = resp.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            raise HTTPException(
                status_code=502,
                detail=f"No data returned from Yahoo for {ticker}",
            )

        quotes = result[0].get("indicators", {}).get("quote", [{}])
        closes = quotes[0].get("close", []) if quotes else []
        prices = [float(c) for c in closes if c is not None]

        if not prices:
            raise HTTPException(
                status_code=502,
                detail=f"No valid closing prices for {ticker}",
            )

        log.info(f"Yahoo chart API: fetched {len(prices)} prices for {ticker}")
        return prices

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to fetch prices for {ticker}: {e}")
        raise HTTPException(status_code=502, detail=str(e))
