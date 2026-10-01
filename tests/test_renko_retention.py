"""
Tests for Renko brick retention (regime_platform/sweep_orchestrator/renko_retention.py).

Root cause (2026-09-09, worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md §7 "Finding 2"):
nt_renko_bricks accumulates orphaned historical brick_size grids forever —
nothing ever deletes a symbol's stale grid once the heavy sweep re-optimizes
brick_size (now daily). This module is the nightly hygiene pass (mirrors
eod_retention.py's design and test shape): prune rows whose brick_size no
longer matches the symbol's live config/brick_params.yaml value, and prune
rows older than renko_brick_retention_days.

Covers, with mocked HTTP (never touches real Supabase):
  - distinct-symbol discovery
  - the stale-brick_size DELETE filter shape (tolerance band, not exact
    float equality, since brick_size round-trips through JSON)
  - the old-session DELETE cutoff shape
  - the full run_renko_brick_retention flow: summary counts, per-symbol
    load_brick_params lookups, never-raises on missing config or errors
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

import pytest

from regime_platform.sweep_orchestrator.renko_retention import (
    _delete_old_sessions,
    _delete_stale_brick_size,
    _distinct_symbols,
    _parse_content_range_total,
    run_renko_brick_retention,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text="", json_data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json = json_data

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


class FakeClient:
    """Minimal httpx.AsyncClient stand-in recording calls."""

    def __init__(self):
        self.calls = []
        self.get_responses = []
        self.delete_responses = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, method, url, params=None):
        self.calls.append({"method": method, "url": url, "params": params})

    async def get(self, url, headers=None, params=None):
        self._record("GET", url, params=params)
        return self.get_responses.pop(0) if self.get_responses else FakeResponse(json_data=[])

    async def delete(self, url, headers=None, params=None):
        self._record("DELETE", url, params=params)
        return self.delete_responses.pop(0) if self.delete_responses else FakeResponse(
            headers={"content-range": "0-0/0"}
        )


# ─── _parse_content_range_total (shared helper, sanity check) ─────────────


def test_parse_content_range_total():
    assert _parse_content_range_total("0-4/5") == 5
    assert _parse_content_range_total("0-999/*") == 0
    assert _parse_content_range_total(None) == 0
    assert _parse_content_range_total("garbage") == 0


# ─── _distinct_symbols ──────────────────────────────────────────────────


class TestDistinctSymbols:
    @pytest.mark.asyncio
    async def test_returns_sorted_unique_symbols_from_active_roster(self):
        """Sourced from fetch_active_symbols() (nt_symbol WHERE active =
        true) — NOT from scanning nt_renko_bricks itself. See this
        module's docstring §13.0: scanning the brick table directly hits
        this project's 1000-row PostgREST GET cap and silently truncates
        the roster."""
        with patch(
            "regime_platform.sweep_orchestrator.renko_retention.fetch_active_symbols",
            new_callable=AsyncMock,
            return_value=[
                {"symbol": "XAUUSD"}, {"symbol": "AAPL"}, {"symbol": "XAUUSD"},
            ],
        ):
            result = await _distinct_symbols()
        assert result == ["AAPL", "XAUUSD"]

    @pytest.mark.asyncio
    async def test_roster_fetch_failure_returns_empty_list_not_raise(self):
        async def raising_fetch_active_symbols():
            raise RuntimeError("connection reset")

        with patch(
            "regime_platform.sweep_orchestrator.renko_retention.fetch_active_symbols",
            side_effect=raising_fetch_active_symbols,
        ):
            result = await _distinct_symbols()
        assert result == []


# ─── _delete_stale_brick_size ───────────────────────────────────────────


class TestDeleteStaleBrickSize:
    @pytest.mark.asyncio
    async def test_filter_excludes_current_brick_size_band(self):
        """The DELETE must target rows OUTSIDE a tolerance band around the
        symbol's current brick_size — never the current grid itself."""
        client = FakeClient()
        client.delete_responses.append(FakeResponse(headers={"content-range": "0-9/10"}))

        deleted = await _delete_stale_brick_size(client, "XAUUSD", 16.2)

        assert deleted == 10
        call = client.calls[-1]
        assert call["method"] == "DELETE"
        assert call["params"]["symbol"] == "eq.XAUUSD"
        # tolerance band must straddle 16.2 (not an exact-equality filter,
        # since brick_size round-trips through JSON/PostgREST as a float)
        or_filter = call["params"]["or"]
        assert or_filter.startswith("(brick_size.lt.")
        assert "brick_size.gt." in or_filter

    @pytest.mark.asyncio
    async def test_near_zero_brick_size_uses_absolute_floor(self):
        """A symbol like AUDUSD has brick_size ~0.00004 — the tolerance
        must not collapse to zero and accidentally match everything."""
        client = FakeClient()
        client.delete_responses.append(FakeResponse(headers={"content-range": "0-0/0"}))

        await _delete_stale_brick_size(client, "AUDUSD", 0.00004)

        or_filter = client.calls[-1]["params"]["or"]
        # Must be fixed-point (no scientific notation — PostgREST's numeric
        # filter parser is not guaranteed to accept "3.996e-05" in a
        # query-string literal), and must bracket the real value.
        assert "e-" not in or_filter.lower()
        import re
        low = float(re.search(r"brick_size\.lt\.([\d.]+)", or_filter).group(1))
        high = float(re.search(r"brick_size\.gt\.([\d.]+)", or_filter).group(1))
        assert low < 0.00004 < high

    @pytest.mark.asyncio
    async def test_http_failure_returns_zero_not_raise(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(status_code=500, text="boom"))
        deleted = await _delete_stale_brick_size(client, "XAUUSD", 16.2)
        assert deleted == 0


# ─── _delete_old_sessions ───────────────────────────────────────────────


class TestDeleteOldSessions:
    @pytest.mark.asyncio
    async def test_filter_uses_lt_cutoff(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(headers={"content-range": "0-2/3"}))

        deleted = await _delete_old_sessions(client, "2026-09-07")

        assert deleted == 3
        call = client.calls[-1]
        assert call["params"]["session_date"] == "lt.2026-09-07"

    @pytest.mark.asyncio
    async def test_http_failure_returns_zero_not_raise(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(status_code=500, text="boom"))
        deleted = await _delete_old_sessions(client, "2026-09-07")
        assert deleted == 0


# ─── run_renko_brick_retention (full flow) ─────────────────────────────


class TestRunRenkoBrickRetention:
    @pytest.mark.asyncio
    async def test_missing_supabase_config_returns_zero_summary(self):
        with patch(
            "regime_platform.sweep_orchestrator.renko_retention.settings.SUPABASE_URL", "",
        ):
            summary = await run_renko_brick_retention()
        assert summary == {
            "deleted_stale_brick_size": 0,
            "deleted_old_sessions": 0,
            "symbols_checked": 0,
            "errors": 0,
        }

    @pytest.mark.asyncio
    async def test_full_flow_counts_deletes_per_symbol_and_old_sessions(self):
        client = FakeClient()
        # one stale-brick_size DELETE per symbol (2 symbols), then the
        # old-session DELETE
        client.delete_responses.extend([
            FakeResponse(headers={"content-range": "0-4/5"}),   # AAPL stale prune
            FakeResponse(headers={"content-range": "0-1/2"}),   # XAUUSD stale prune
            FakeResponse(headers={"content-range": "0-9/10"}),  # old-session prune
        ])

        def fake_load_brick_params(symbol):
            return ({"AAPL": 0.59, "XAUUSD": 16.2}[symbol], 3, 6)

        with (
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.settings.SUPABASE_URL",
                "https://test.supabase.co",
            ),
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.fetch_active_symbols",
                new_callable=AsyncMock,
                return_value=[{"symbol": "AAPL"}, {"symbol": "XAUUSD"}],
            ),
            patch(
                "regime_platform.config.brick_params.load_brick_params",
                side_effect=fake_load_brick_params,
            ),
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.get_renko_brick_retention_days",
                return_value=2,
            ),
        ):
            summary = await run_renko_brick_retention()

        assert summary["symbols_checked"] == 2
        assert summary["deleted_stale_brick_size"] == 5 + 2
        assert summary["deleted_old_sessions"] == 10
        assert summary["errors"] == 0

    @pytest.mark.asyncio
    async def test_per_symbol_error_is_counted_not_raised(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(headers={"content-range": "0-1/2"}))

        def raising_load_brick_params(symbol):
            raise RuntimeError("YAML corrupt")

        with (
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.settings.SUPABASE_URL",
                "https://test.supabase.co",
            ),
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.fetch_active_symbols",
                new_callable=AsyncMock,
                return_value=[{"symbol": "AAPL"}],
            ),
            patch(
                "regime_platform.config.brick_params.load_brick_params",
                side_effect=raising_load_brick_params,
            ),
            patch(
                "regime_platform.sweep_orchestrator.renko_retention.get_renko_brick_retention_days",
                return_value=2,
            ),
        ):
            summary = await run_renko_brick_retention()

        # The per-symbol failure must not raise out of the pass, and must
        # not block the old-session prune from still running.
        assert summary["errors"] == 1
        assert summary["deleted_stale_brick_size"] == 0
        assert summary["deleted_old_sessions"] == 2
