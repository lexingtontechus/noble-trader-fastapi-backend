"""Tests for real per-bar timestamp threading in the TDVA price fetcher.

Root cause (2026-09-09, worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7 "Finding 1"):
the price fetcher discarded every candle's real time field before it ever
reached the orchestrator, so the Renko brick engine had no real per-bar
timestamps to work with and orchestrator.py's brick-capture block fell back
to a fabricated hourly reconstruction. ``fetch_tradingview_batch``/
``fetch_candles_for_symbols`` now optionally return real timestamps
alongside prices (``include_timestamps=True``), index-aligned and reversed
in lockstep with the existing ascending-order normalisation.

Covers:
  - default (include_timestamps=False) return shape is completely
    unchanged — every pre-existing caller (eod_resolver, heavy_sweep,
    macro_filter, bracket_reachability, the backfill scripts) must see
    zero behavior change.
  - include_timestamps=True returns (prices, timestamps) index-aligned,
    for both dict-bar and array-bar TDVA response shapes.
  - reversal (ascending-order normalisation) applies to timestamps too,
    not just prices — a regression here would silently misalign the two
    lists.
  - a bar with no parseable time field yields None at that position rather
    than crashing or silently dropping the bar.
  - the ms-vs-seconds epoch normalisation heuristic.
  - fetch_candles_for_symbols forwards include_timestamps correctly.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regime_platform.sweep_orchestrator.price_fetcher import (
    _normalize_epoch_seconds,
    fetch_candles_for_symbols,
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
    with patch("regime_platform.core.settings.settings") as mock_settings:
        mock_settings.TRADINGVIEW_API_KEY = "test-key"
        mock_settings.TRADINGVIEW_API_HOST = "test.host"
        mock_settings.TRADINGVIEW_BASE_URL = "https://test.host"
        yield mock_settings


def _patched_client(resp):
    mock_client = AsyncMock()
    mock_client.post.return_value = resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    return mock_client


class TestNormalizeEpochSeconds:
    def test_none_is_none(self):
        assert _normalize_epoch_seconds(None) is None

    def test_non_numeric_is_none(self):
        assert _normalize_epoch_seconds("not-a-number") is None

    def test_seconds_pass_through(self):
        # A realistic 2026-era Unix-seconds epoch stays as-is.
        assert _normalize_epoch_seconds(1_799_000_000) == pytest.approx(1_799_000_000.0)

    def test_milliseconds_are_converted_to_seconds(self):
        # Same instant expressed in milliseconds must normalise identically.
        assert _normalize_epoch_seconds(1_799_000_000_000) == pytest.approx(1_799_000_000.0)


class TestIncludeTimestampsDefaultUnchanged:
    @pytest.mark.asyncio
    async def test_default_returns_plain_dict(self, tv_settings):
        """include_timestamps unset -> return shape identical to before
        this feature existed (every pre-existing caller depends on this)."""
        symbols = [{"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"}]
        payload = {
            "success": True,
            "data": {"data": [{
                "success": True,
                "symbol": "OANDA:USDJPY",
                "current": _bar(3000, 159.35),
                "history": [_bar(2000, 160.10), _bar(1000, 162.87)],
            }]},
        }
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _patched_client(_mock_resp(payload))
            result = await fetch_tradingview_batch(symbols, hours=5)

        assert isinstance(result, dict)
        assert result["USDJPY"] == [162.87, 160.10]


class TestIncludeTimestampsTrue:
    @pytest.mark.asyncio
    async def test_dict_bars_timestamps_aligned_and_reversed(self, tv_settings):
        symbols = [{"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"}]
        payload = {
            "success": True,
            "data": {"data": [{
                "success": True,
                "symbol": "OANDA:USDJPY",
                "current": _bar(3000, 159.35),
                "history": [
                    _bar(3000, 159.35),  # newest
                    _bar(2000, 160.10),  # middle
                    _bar(1000, 162.87),  # oldest
                ],
            }]},
        }
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _patched_client(_mock_resp(payload))
            prices, timestamps = await fetch_tradingview_batch(
                symbols, hours=5, include_timestamps=True,
            )

        assert prices["USDJPY"] == [162.87, 160.10, 159.35]
        # Ascending order (oldest -> newest), same reversal applied to prices.
        assert timestamps["USDJPY"] == [1000.0, 2000.0, 3000.0]
        assert len(timestamps["USDJPY"]) == len(prices["USDJPY"])

    @pytest.mark.asyncio
    async def test_list_bars_timestamps_from_index_zero(self, tv_settings):
        """Array-bar shape [time, open, high, low, close, volume]."""
        symbols = [{"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"}]
        payload = {
            "success": True,
            "data": {"data": [{
                "success": True,
                "symbol": "COINBASE:BTCUSD",
                "current": {"time": 3000, "close": 70000.0},
                "history": [
                    [3000, 69990.0, 70010.0, 69985.0, 70000.0, 2.0],  # newest
                    [1000, 69000.0, 69100.0, 68900.0, 69050.0, 5.0],  # oldest
                ],
            }]},
        }
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _patched_client(_mock_resp(payload))
            prices, timestamps = await fetch_tradingview_batch(
                symbols, hours=5, include_timestamps=True,
            )

        assert prices["BTCUSD"] == [69050.0, 70000.0]
        assert timestamps["BTCUSD"] == [1000.0, 3000.0]

    @pytest.mark.asyncio
    async def test_missing_time_field_yields_none_not_crash(self, tv_settings):
        symbols = [{"symbol": "XAUUSD", "tradingview_symbol": "OANDA:XAUUSD"}]
        bar_no_time = {"open": 2399.0, "close": 2400.5, "max": 2401.0, "min": 2398.5}
        payload = {
            "success": True,
            "data": {"data": [{
                "success": True,
                "symbol": "OANDA:XAUUSD",
                "current": _bar(3000, 2400.5),
                "history": [_bar(2000, 2399.0), bar_no_time],
            }]},
        }
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _patched_client(_mock_resp(payload))
            prices, timestamps = await fetch_tradingview_batch(
                symbols, hours=5, include_timestamps=True,
            )

        assert len(timestamps["XAUUSD"]) == len(prices["XAUUSD"]) == 2
        # bar_no_time was oldest in the payload (list index 1) and stays
        # oldest (index 0) after the ascending-order reverse.
        assert timestamps["XAUUSD"][0] is None
        assert timestamps["XAUUSD"][1] == pytest.approx(2000.0)


class TestFetchCandlesForSymbolsForwarding:
    @pytest.mark.asyncio
    async def test_forwards_include_timestamps(self, tv_settings):
        symbols = [{"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"}]
        payload = {
            "success": True,
            "data": {"data": [{
                "success": True,
                "symbol": "OANDA:USDJPY",
                "current": _bar(3000, 159.35),
                "history": [_bar(1000, 162.87)],
            }]},
        }
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _patched_client(_mock_resp(payload))
            result = await fetch_candles_for_symbols(
                symbols, n_bars=5, include_timestamps=True,
            )

        assert isinstance(result, tuple)
        prices, timestamps = result
        assert prices["USDJPY"] == [162.87]
        assert timestamps["USDJPY"] == [1000.0]

    @pytest.mark.asyncio
    async def test_default_shape_unchanged(self, tv_settings):
        symbols = [{"symbol": "USDJPY", "tradingview_symbol": "OANDA:USDJPY"}]
        payload = {
            "success": True,
            "data": {"data": [{
                "success": True,
                "symbol": "OANDA:USDJPY",
                "current": _bar(3000, 159.35),
                "history": [_bar(1000, 162.87)],
            }]},
        }
        with patch(
            "regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient"
        ) as mock_client_cls:
            mock_client_cls.return_value = _patched_client(_mock_resp(payload))
            result = await fetch_candles_for_symbols(symbols, n_bars=5)

        assert isinstance(result, dict)
        assert result["USDJPY"] == [162.87]
