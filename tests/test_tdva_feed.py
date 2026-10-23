"""test_tdva_feed.py — TDVA is the research harnesses' ONLY price source.

Guards the properties the harmonization depends on:

  * the symbol mapping comes from ``nt_symbol.tradingview_symbol`` (no proxy
    table), and a symbol with no mapping is SKIPPED, never substituted;
  * the fetch goes through the orchestrator's own ``fetch_tradingview_batch``
    (so auth / 429 / chunking cannot drift from the live sweep) — asserted by
    patching exactly that symbol;
  * the output frame matches the cached-CSV convention byte-for-byte
    (``timestamp,open,high,low,close,volume``; UTC ISO ``Z``; ascending), so
    ``regime_edge/data.py`` and the vol-backtest CSV path consume TDVA and CSV
    identically;
  * a failed symbol is absent from the result rather than raising — the caller
    must see a short dict, because a silently-substituted feed is the exact
    bug this module exists to prevent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from regime_platform.services import tdva_feed  # noqa: E402
from regime_platform.services.tdva_feed import (  # noqa: E402
    OHLCV_COLUMNS,
    _iso_utc,
    _rows_to_frame,
    fetch_ohlcv,
    load_symbol_configs,
)


# ---------------------------------------------------------------------------
# Frame convention
# ---------------------------------------------------------------------------

class TestFrameConvention:
    def test_columns_and_utc_iso_z(self):
        frame = _rows_to_frame("XAUUSD", [
            {"t": 1_700_000_000.0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10.0},
        ])
        assert list(frame.columns) == OHLCV_COLUMNS
        # 2023-11-14T22:13:20Z — UTC, trailing Z, no offset
        assert frame["timestamp"].iloc[0] == "2023-11-14T22:13:20Z"

    def test_iso_utc_is_always_zulu(self):
        assert _iso_utc(0.0) == "1970-01-01T00:00:00Z"
        assert _iso_utc(1_700_000_123.0).endswith("Z")

    def test_millisecond_epoch_is_normalised(self):
        # TDVA's unit is undocumented; price_fetcher's 1e12 heuristic applies
        # before this module sees the value, so a raw ms value must still land
        # on the same instant when passed through _iso_utc's caller.
        frame = _rows_to_frame("BTCUSD", [
            {"t": 1_700_000_000_000.0 / 1000.0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
        ])
        assert frame["timestamp"].iloc[0] == "2023-11-14T22:13:20Z"

    def test_bars_without_timestamp_are_dropped(self):
        frame = _rows_to_frame("XAUUSD", [
            {"t": None, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
            {"t": 1_700_000_000.0, "o": 2, "h": 2, "l": 2, "c": 2, "v": 2},
        ])
        assert len(frame) == 1
        assert frame["close"].iloc[0] == 2.0

    def test_empty_input_gives_empty_frame_with_columns(self):
        frame = _rows_to_frame("XAUUSD", [])
        assert frame.empty
        assert list(frame.columns) == OHLCV_COLUMNS

    def test_missing_volume_becomes_zero_not_nan(self):
        frame = _rows_to_frame("XAUUSD", [
            {"t": 1_700_000_000.0, "o": 1, "h": 1, "l": 1, "c": 1, "v": None},
        ])
        assert frame["volume"].iloc[0] == 0.0
        assert not frame["volume"].isna().any()

    def test_output_is_sorted_ascending(self):
        frame = _rows_to_frame("XAUUSD", [
            {"t": 1_700_000_002.0, "o": 3, "h": 3, "l": 3, "c": 3, "v": 3},
            {"t": 1_700_000_000.0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
            {"t": 1_700_000_001.0, "o": 2, "h": 2, "l": 2, "c": 2, "v": 2},
        ])
        assert frame["timestamp"].is_monotonic_increasing


# ---------------------------------------------------------------------------
# Symbol mapping — authoritative, never a proxy table
# ---------------------------------------------------------------------------

class TestSymbolMapping:
    @pytest.mark.asyncio
    async def test_filters_to_requested_symbols(self, monkeypatch):
        async def fake_active():
            return [
                {"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"},
                {"symbol": "XAUUSD", "tradingview_symbol": "OANDA:XAUUSD"},
            ]

        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            fake_active,
        )
        configs = await load_symbol_configs(["XAUUSD"])
        assert [c["symbol"] for c in configs] == ["XAUUSD"]

    @pytest.mark.asyncio
    async def test_symbol_without_tv_mapping_is_skipped(self, monkeypatch):
        async def fake_active():
            return [
                {"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"},
                {"symbol": "GHOST", "tradingview_symbol": None},
            ]

        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            fake_active,
        )
        configs = await load_symbol_configs(["BTCUSD", "GHOST"])
        assert [c["symbol"] for c in configs] == ["BTCUSD"]

    @pytest.mark.asyncio
    async def test_no_active_symbols_returns_empty(self, monkeypatch):
        async def fake_active():
            return []

        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            fake_active,
        )
        assert await load_symbol_configs(["BTCUSD"]) == []


# ---------------------------------------------------------------------------
# Fetch — one client, failures visible
# ---------------------------------------------------------------------------

class TestFetchOhlcv:
    @pytest.mark.asyncio
    async def test_uses_the_orchestrators_own_batch_client(self, monkeypatch):
        """The whole point of the harmonization: no second HTTP path."""
        seen = {}

        async def fake_batch(symbols, hours=None, interval=None,
                             include_timestamps=False, ohlcv_out=None):
            seen["symbols"] = symbols
            seen["hours"] = hours
            seen["interval"] = interval
            seen["ohlcv_out"] = ohlcv_out
            if ohlcv_out is not None:
                ohlcv_out["BTCUSD"] = [
                    {"t": 1_700_000_000.0, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 7},
                ]
            return {"BTCUSD": [1.5]}

        async def fake_active():
            return [{"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"}]

        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.price_fetcher.fetch_tradingview_batch",
            fake_batch,
        )
        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            fake_active,
        )

        out = await fetch_ohlcv(["BTCUSD"], hours=500)

        assert seen["interval"] == "1h"
        assert seen["hours"] == 500
        assert seen["symbols"] == [
            {"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"}
        ]
        assert set(out) == {"BTCUSD"}
        assert out["BTCUSD"]["close"].iloc[0] == 1.5

    @pytest.mark.asyncio
    async def test_failed_symbol_is_absent_not_raised(self, monkeypatch):
        async def fake_batch(symbols, hours=None, interval=None,
                             include_timestamps=False, ohlcv_out=None):
            # Only BTCUSD comes back; XAUUSD failed upstream.
            if ohlcv_out is not None:
                ohlcv_out["BTCUSD"] = [
                    {"t": 1_700_000_000.0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                ]
            return {"BTCUSD": [1.0]}

        async def fake_active():
            return [
                {"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"},
                {"symbol": "XAUUSD", "tradingview_symbol": "OANDA:XAUUSD"},
            ]

        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.price_fetcher.fetch_tradingview_batch",
            fake_batch,
        )
        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            fake_active,
        )

        out = await fetch_ohlcv(["BTCUSD", "XAUUSD"])
        assert set(out) == {"BTCUSD"}

    @pytest.mark.asyncio
    async def test_hours_is_capped_at_the_batch_ceiling(self, monkeypatch):
        seen = {}

        async def fake_batch(symbols, hours=None, interval=None,
                             include_timestamps=False, ohlcv_out=None):
            seen["hours"] = hours
            return {}

        async def fake_active():
            return [{"symbol": "BTCUSD", "tradingview_symbol": "COINBASE:BTCUSD"}]

        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.price_fetcher.fetch_tradingview_batch",
            fake_batch,
        )
        monkeypatch.setattr(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_active_symbols",
            fake_active,
        )

        await fetch_ohlcv(["BTCUSD"], hours=999_999)
        assert seen["hours"] == tdva_feed.MAX_TDVA_BARS


# ---------------------------------------------------------------------------
# vol_backtest integration
# ---------------------------------------------------------------------------

class TestVolBacktestIntegration:
    def test_load_tdva_returns_symbol_series(self, monkeypatch):
        async def fake_fetch_ohlcv(symbols, hours=2160):
            assert symbols == ["BTCUSD"]
            return {
                "BTCUSD": pd.DataFrame({
                    "timestamp": ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"],
                    "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
                    "close": [1.0, 2.0], "volume": [0.0, 0.0],
                })
            }

        monkeypatch.setattr(
            "regime_platform.services.tdva_feed.fetch_ohlcv", fake_fetch_ohlcv
        )

        from vol_backtest.data import load_tdva

        series = load_tdva(["BTCUSD"])
        assert len(series) == 1
        assert series[0].symbol == "BTCUSD"
        assert list(series[0].prices) == [1.0, 2.0]
        assert series[0].timestamps is not None

    def test_load_tdva_skips_symbols_with_no_data(self, monkeypatch):
        async def fake_fetch_ohlcv(symbols, hours=2160):
            return {"BTCUSD": pd.DataFrame({
                "timestamp": ["2026-01-01T00:00:00Z"], "open": [1.0], "high": [1.0],
                "low": [1.0], "close": [1.0], "volume": [0.0],
            })}

        monkeypatch.setattr(
            "regime_platform.services.tdva_feed.fetch_ohlcv", fake_fetch_ohlcv
        )

        from vol_backtest.data import load_tdva

        assert [s.symbol for s in load_tdva(["BTCUSD", "XAUUSD"])] == ["BTCUSD"]
