"""Tests for the rolling calibration-bias window (intraday cadence plan, C)."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.supabase_client import fetch_calibration_bias


class FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.captured = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.captured = params
        return FakeResp(self.rows)


def _run(symbol="EURUSD", rows=None, **kwargs):
    client = FakeClient(rows if rows is not None else [])
    with (
        patch(
            "regime_platform.sweep_orchestrator.supabase_client._url",
            return_value="https://x.supabase.co/rest/v1/nt_calibration_records",
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
        result = asyncio.run(fetch_calibration_bias(symbol, **kwargs))
    return result, client


def _rows(n, wins, pred=0.90):
    out = []
    for i in range(n):
        out.append({"predicted_p_win": pred, "outcome": "win" if i < wins else "loss"})
    return out


class TestRollingWindow:
    def test_default_window_adds_entered_at_filter(self):
        # 30 records, 15 wins → bias = 0.90 - 0.50 = 0.40
        result, client = _run(rows=_rows(30, 15))
        assert result is not None
        assert result["n"] == 30
        assert result["bias"] == pytest.approx(0.40, abs=1e-4)
        assert "entered_at" in client.captured
        assert client.captured["entered_at"].startswith("gte.")
        assert "symbol" in client.captured

    def test_window_days_zero_is_all_time(self):
        _, client = _run(rows=_rows(25, 20), window_days=0)
        assert "entered_at" not in client.captured

    def test_explicit_window_days_used(self):
        _, client = _run(rows=_rows(25, 20), window_days=3)
        assert client.captured["entered_at"].startswith("gte.")
        # cutoff should be ~3 days ago
        cutoff = client.captured["entered_at"][4:]
        from datetime import timedelta

        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cutoff)).total_seconds()
        assert 2.5 * 86400 < age < 3.5 * 86400

    def test_below_min_records_returns_none(self):
        result, _ = _run(rows=_rows(19, 10), min_records=20)
        assert result is None

    def test_default_min_records_uses_settings(self):
        # settings.EOD_BIAS_MIN_RECORDS default 20 — 19 rows → None
        result, _ = _run(rows=_rows(19, 10))
        assert result is None

    def test_venue_filter_preserved(self):
        _, client = _run(rows=_rows(25, 20), venue="OANDA")
        assert client.captured["venue"] == "eq.OANDA"

    def test_calibrated_status(self):
        # 25 records, 20 wins → win_rate 0.80 vs pred 0.82 → bias +0.02 → CALIBRATED
        result, _ = _run(rows=_rows(25, 20, pred=0.82))
        assert result["status"] == "CALIBRATED"

    def test_underconfident_status(self):
        # 25 records, 24 wins → win_rate 0.96 vs pred 0.80 → bias -0.16 → UNDERCONFIDENT
        result, _ = _run(rows=_rows(25, 24, pred=0.80))
        assert result["status"] == "UNDERCONFIDENT"
