"""Tests for supabase_client.fetch_last_full_heavy_sweep_ts — the
force-refresh override's data source. Worklog
20260909_CALIBRATION_AND_SCORING_METHODOLOGY_REFRAME.md, P0 fix.
"""

import asyncio
from unittest.mock import patch

from regime_platform.sweep_orchestrator.supabase_client import (
    fetch_last_full_heavy_sweep_ts,
)


class FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class FakeClient:
    def __init__(self, rows=None, raise_exc=None):
        self.rows = rows if rows is not None else []
        self.raise_exc = raise_exc
        self.captured = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.captured = params
        if self.raise_exc:
            raise self.raise_exc
        return FakeResp(self.rows)


def _run(rows=None, raise_exc=None, symbol="EURUSD"):
    client = FakeClient(rows=rows, raise_exc=raise_exc)
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._url",
            return_value="https://x.supabase.co/rest/v1/nt_sweep_result",
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._headers",
            return_value={"apikey": "k", "Authorization": "Bearer k"},
        ),
        patch(
            "regime_platform.sweep_orchestrator.supabase_client.httpx.AsyncClient",
            return_value=client,
        ),
    ):
        result = asyncio.run(fetch_last_full_heavy_sweep_ts(symbol))
    return result, client


def test_returns_parsed_timestamp_on_success():
    result, client = _run(rows=[{"sweep_timestamp": "2026-09-06T20:00:29.579947+00:00"}])
    assert result is not None
    assert result.year == 2026 and result.month == 9 and result.day == 6
    # Confirms it filters to completed heavy sweeps specifically.
    assert client.captured["source"] == "eq.sweep-heavy"


def test_returns_none_when_no_rows():
    result, _ = _run(rows=[])
    assert result is None


def test_returns_none_on_exception_never_raises():
    result, _ = _run(raise_exc=RuntimeError("network down"))
    assert result is None
