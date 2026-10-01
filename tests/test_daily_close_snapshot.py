"""
Tests for the daily-close snapshot module
(regime_platform/sweep_orchestrator/daily_close_snapshot.py).

Root cause (2026-10-01, worklog
20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §13.1/§13.6): nt_daily_close
exists so correlation's 120-day history survives nt_renko_bricks being
pruned to its ~2-day live-grid steady state. This module is the nightly
top-up that keeps it current.

Covers, with mocked HTTP (never touches real Supabase):
  - _fetch_one_daily_close: correct query shape (symbol eq, session_date
    eq, LIMIT 1 — the fix for the 1000-row GET cap bug), returns None on
    HTTP failure or empty result, never raises
  - _fetch_recent_daily_closes: fans out one query per (symbol, date) pair,
    concurrently, skips Nones
  - _upsert_daily_closes: correct on_conflict/Prefer header shape, count
    parsing, empty-input short-circuit
  - run_daily_close_snapshot: full-flow summary counts; missing config
    returns zeroed summary without raising; read failure doesn't prevent
    an (empty) upsert attempt from still being safely skipped
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.daily_close_snapshot import (
    SNAPSHOT_LOOKBACK_DAYS,
    _fetch_one_daily_close,
    _fetch_recent_daily_closes,
    _parse_content_range_total,
    _upsert_daily_closes,
    run_daily_close_snapshot,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text="", json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_body = json_body if json_body is not None else []

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json_body


class FakeClient:
    """Minimal httpx.AsyncClient stand-in recording calls."""

    def __init__(self):
        self.get_calls = []
        self.get_responses = []
        self.post_calls = []
        self.post_responses = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.get_calls.append({"url": url, "params": params})
        return self.get_responses.pop(0) if self.get_responses else FakeResponse(json_body=[])

    async def post(self, url, headers=None, json=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return self.post_responses.pop(0) if self.post_responses else FakeResponse(
            headers={"content-range": "0-0/0"}
        )


# ─── _parse_content_range_total (shared helper, sanity check) ─────────────


def test_parse_content_range_total():
    assert _parse_content_range_total("0-4/5") == 5
    assert _parse_content_range_total("0-999/*") == 0
    assert _parse_content_range_total(None) == 0


# ─── _fetch_one_daily_close ─────────────────────────────────────────────


class TestFetchOneDailyClose:
    @pytest.mark.asyncio
    async def test_query_shape_is_targeted_not_flat(self):
        """The whole point of this design is LIMIT 1 per (symbol, date) —
        never a flat multi-symbol/multi-date scan that could hit the
        project's 1000-row GET cap."""
        client = FakeClient()
        client.get_responses.append(FakeResponse(json_body=[
            {"symbol": "AAPL", "session_date": "2026-10-01", "close_price": 338.4, "ts": "2026-10-01T20:00:00+00:00"}
        ]))

        row = await _fetch_one_daily_close(client, "AAPL", "2026-10-01")

        assert row["close_price"] == 338.4
        call = client.get_calls[-1]
        assert call["params"]["symbol"] == "eq.AAPL"
        assert call["params"]["session_date"] == "eq.2026-10-01"
        assert call["params"]["limit"] == "1"

    @pytest.mark.asyncio
    async def test_empty_result_returns_none(self):
        client = FakeClient()
        client.get_responses.append(FakeResponse(json_body=[]))
        row = await _fetch_one_daily_close(client, "AAPL", "2026-10-01")
        assert row is None

    @pytest.mark.asyncio
    async def test_http_failure_returns_none_not_raise(self):
        client = FakeClient()
        client.get_responses.append(FakeResponse(status_code=500, text="boom"))
        row = await _fetch_one_daily_close(client, "AAPL", "2026-10-01")
        assert row is None


# ─── _fetch_recent_daily_closes ─────────────────────────────────────────


class TestFetchRecentDailyCloses:
    @pytest.mark.asyncio
    async def test_fans_out_one_query_per_symbol_date_pair(self):
        client = FakeClient()
        # 2 symbols x 2 dates = 4 calls expected
        client.get_responses.extend([
            FakeResponse(json_body=[{"symbol": "AAPL", "session_date": "2026-10-01", "close_price": 1, "ts": "x"}]),
            FakeResponse(json_body=[]),  # AAPL/2026-09-30 → no row
            FakeResponse(json_body=[{"symbol": "AUDUSD", "session_date": "2026-10-01", "close_price": 2, "ts": "x"}]),
            FakeResponse(json_body=[{"symbol": "AUDUSD", "session_date": "2026-09-30", "close_price": 3, "ts": "x"}]),
        ])

        rows = await _fetch_recent_daily_closes(
            client, ["AAPL", "AUDUSD"], ["2026-10-01", "2026-09-30"],
        )

        assert len(client.get_calls) == 4
        assert len(rows) == 3  # one None skipped


# ─── _upsert_daily_closes ────────────────────────────────────────────────


class TestUpsertDailyCloses:
    @pytest.mark.asyncio
    async def test_empty_input_short_circuits(self):
        client = FakeClient()
        result = await _upsert_daily_closes(client, [])
        assert result == 0
        assert client.post_calls == []

    @pytest.mark.asyncio
    async def test_on_conflict_and_prefer_header_shape(self):
        client = FakeClient()
        client.post_responses.append(FakeResponse(headers={"content-range": "0-1/2"}))
        rows = [
            {"symbol": "AAPL", "session_date": "2026-10-01", "close_price": 338.4, "ts": "x"},
            {"symbol": "AUDUSD", "session_date": "2026-10-01", "close_price": 0.65, "ts": "y"},
        ]

        result = await _upsert_daily_closes(client, rows)

        assert result == 2
        call = client.post_calls[-1]
        assert "on_conflict=symbol,session_date" in call["url"]
        assert call["headers"]["Prefer"] == "resolution=merge-duplicates,count=exact"
        assert len(call["json"]) == 2

    @pytest.mark.asyncio
    async def test_http_failure_returns_zero_not_raise(self):
        client = FakeClient()
        client.post_responses.append(FakeResponse(status_code=500, text="boom"))
        result = await _upsert_daily_closes(client, [
            {"symbol": "AAPL", "session_date": "2026-10-01", "close_price": 1, "ts": "x"},
        ])
        assert result == 0


# ─── run_daily_close_snapshot (full flow) ──────────────────────────────


class TestRunDailyCloseSnapshot:
    @pytest.mark.asyncio
    async def test_missing_supabase_config_returns_zero_summary(self):
        with patch(
            "regime_platform.sweep_orchestrator.daily_close_snapshot.settings.SUPABASE_URL", "",
        ):
            summary = await run_daily_close_snapshot()
        assert summary == {
            "rows_read": 0,
            "rows_upserted": 0,
            "symbols_checked": 0,
            "errors": 0,
        }

    @pytest.mark.asyncio
    async def test_full_flow_counts(self):
        client = FakeClient()

        with (
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot.settings.SUPABASE_URL",
                "https://test.supabase.co",
            ),
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot.fetch_active_symbols",
                new_callable=AsyncMock,
                return_value=[{"symbol": "AAPL"}, {"symbol": "AUDUSD"}],
            ),
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot._fetch_recent_daily_closes",
                new_callable=AsyncMock,
                return_value=[
                    {"symbol": "AAPL", "session_date": "2026-10-01", "close_price": 1, "ts": "x"},
                    {"symbol": "AUDUSD", "session_date": "2026-10-01", "close_price": 2, "ts": "y"},
                ],
            ),
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot._upsert_daily_closes",
                new_callable=AsyncMock,
                return_value=2,
            ),
        ):
            summary = await run_daily_close_snapshot()

        assert summary["symbols_checked"] == 2
        assert summary["rows_read"] == 2
        assert summary["rows_upserted"] == 2
        assert summary["errors"] == 0

    @pytest.mark.asyncio
    async def test_read_failure_still_attempts_upsert_safely(self):
        """A read-pass exception must not prevent the upsert pass from
        running (it just upserts nothing) — same independent try/except
        shape as every other retention module in this repo."""
        client = FakeClient()

        async def raising_fetch_active_symbols(*args, **kwargs):
            raise RuntimeError("connection reset")

        with (
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot.settings.SUPABASE_URL",
                "https://test.supabase.co",
            ),
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.daily_close_snapshot.fetch_active_symbols",
                side_effect=raising_fetch_active_symbols,
            ),
        ):
            summary = await run_daily_close_snapshot()

        assert summary["errors"] == 1
        assert summary["rows_read"] == 0
        assert summary["rows_upserted"] == 0


def test_snapshot_lookback_days_is_short_window():
    """Sanity check the design constant — this is a self-healing top-up,
    not the one-time 120-day backfill (migration 190 did that)."""
    assert SNAPSHOT_LOOKBACK_DAYS == 3
