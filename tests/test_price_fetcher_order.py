"""Tests for TDVA candle ordering normalisation (worklog [9.2.0]).

TDVA /api/price/batch returns ``history`` in DESCENDING time order
(newest first). ``fetch_tradingview_batch`` must reverse each symbol's
close list so all downstream consumers see ASCENDING (oldest → newest).

These tests mock the HTTP layer and assert the order contract:
  - prices[0]  == oldest bar
  - prices[-1] == newest bar (the live/current close)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regime_platform.sweep_orchestrator.price_fetcher import (
    fetch_tradingview_batch,
)


def _mock_resp(payload: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.raise_for_status = MagicMock()
    return resp


def _bar(time: int, close: float) -> dict:
    return {
        "time": time,
        "open": close - 0.1,
        "close": close,
        "max": close + 0.1,
        "min": close - 0.2,
        "volume": 100.0,
    }


@pytest.fixture
def tv_settings():
    """Point settings at a fake RapidAPI endpoint.

    price_fetcher imports ``settings`` lazily inside the function
    (``from regime_platform.core.settings import settings``), so we patch
    the source object's attributes on ``regime_platform.core.settings``.
    """
    with patch(
        "regime_platform.core.settings.settings"
    ) as mock_settings:
        mock_settings.TRADINGVIEW_API_KEY = "test-key"
        mock_settings.TRADINGVIEW_API_HOST = "test.host"
        mock_settings.TRADINGVIEW_BASE_URL = "https://test.host"
        yield mock_settings


@pytest.mark.asyncio
async def test_reverses_descending_history_dict_bars(tv_settings):
    """Dict-bar history (newest-first) is returned ascending (oldest-first)."""
    symbols = [{"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"}]
    payload = {
        "success": True,
        "data": {
            "total": 1,
            "successful": 1,
            "failed": 0,
            "data": [
                {
                    "success": True,
                    "symbol": "OANDA:USDJPY",
                    "current": _bar(3000, 159.35),
                    "history": [
                        _bar(3000, 159.35),   # newest
                        _bar(2000, 160.10),   # middle
                        _bar(1000, 162.87),   # oldest
                    ],
                }
            ],
        },
    }

    with patch(
        "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_resp(payload)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client_cls.return_value = mock_client

        result = await fetch_tradingview_batch(symbols, hours=5)

    prices = result["USDJPY"]
    assert prices == [162.87, 160.10, 159.35], (
        f"Expected ascending [162.87, 160.10, 159.35], got {prices}"
    )
    # Contract: [-1] is the newest/live close
    assert prices[-1] == pytest.approx(159.35)
    assert prices[0] == pytest.approx(162.87)


@pytest.mark.asyncio
async def test_reverses_descending_history_list_bars(tv_settings):
    """List-bar history (newest-first) is returned ascending (oldest-first)."""
    symbols = [{"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"}]
    payload = {
        "success": True,
        "data": {
            "total": 1,
            "successful": 1,
            "failed": 0,
            "data": [
                {
                    "success": True,
                    "symbol": "COINBASE:BTCUSD",
                    "current": {"time": 3000, "close": 70000.0},
                    "history": [
                        [3000, 69990.0, 70010.0, 69985.0, 70000.0, 2.0],  # newest
                        [2000, 69800.0, 69900.0, 69750.0, 69850.0, 3.0],  # middle
                        [1000, 69000.0, 69100.0, 68900.0, 69050.0, 5.0],  # oldest
                    ],
                }
            ],
        },
    }

    with patch(
        "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_resp(payload)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client_cls.return_value = mock_client

        result = await fetch_tradingview_batch(symbols, hours=5)

    prices = result["BTCUSD"]
    assert prices == [69050.0, 69850.0, 70000.0], (
        f"Expected ascending [69050.0, 69850.0, 70000.0], got {prices}"
    )
    assert prices[-1] == pytest.approx(70000.0)


@pytest.mark.asyncio
async def test_single_bar_no_reverse_needed(tv_settings):
    """A single-bar history is returned as-is (reverse is a no-op)."""
    symbols = [{"symbol": "XAUUSD", "tradingview_symbol": "OANDA:XAUUSD"}]
    payload = {
        "success": True,
        "data": {
            "data": [
                {
                    "success": True,
                    "symbol": "OANDA:XAUUSD",
                    "current": _bar(3000, 2400.5),
                    "history": [_bar(3000, 2400.5)],
                }
            ]
        },
    }

    with patch(
        "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_resp(payload)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client_cls.return_value = mock_client

        result = await fetch_tradingview_batch(symbols, hours=5)

    assert result["XAUUSD"] == [2400.5]


@pytest.mark.asyncio
async def test_multi_symbol_keys_and_order(tv_settings):
    """Multiple symbols are keyed correctly and each list is ascending."""
    symbols = [
        {"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"},
        {"symbol": "EURUSD", "tradingview_symbol": "OANDA:EURUSD"},
    ]
    payload = {
        "success": True,
        "data": {
            "data": [
                {
                    "success": True,
                    "symbol": "OANDA:USDJPY",
                    "current": _bar(3000, 159.35),
                    "history": [_bar(3000, 159.35), _bar(1000, 162.87)],
                },
                {
                    "success": True,
                    "symbol": "OANDA:EURUSD",
                    "current": _bar(3000, 1.0850),
                    "history": [_bar(3000, 1.0850), _bar(1000, 1.0900)],
                },
            ]
        },
    }

    with patch(
        "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_resp(payload)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client_cls.return_value = mock_client

        result = await fetch_tradingview_batch(symbols, hours=5)

    assert set(result.keys()) == {"USDJPY", "EURUSD"}
    assert result["USDJPY"] == [162.87, 159.35]
    assert result["EURUSD"] == [1.0900, 1.0850]
