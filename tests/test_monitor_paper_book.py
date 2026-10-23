"""Tests for the paper book health monitor script.

Verifies:
  1. _sb_headers returns correct format
  2. _fetch_json returns (status, body) correctly
  3. _fetch_count parses X-Total-Count header
  4. check_paper_book_health produces correct metrics dict shape
  5. format_alert produces proper output with/without alerts
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import monitor_paper_book


def test_sb_headers_format():
    """_sb_headers should return dict with apikey + Authorization."""
    with patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SECRET_KEY": "secret_key_123",
    }):
        headers = monitor_paper_book._sb_headers()
        assert "apikey" in headers
        assert headers["apikey"] == "secret_key_123"
        assert headers["Authorization"].startswith("Bearer ")
        assert "secret_key_123" in headers["Authorization"]


@pytest.mark.asyncio
async def test_fetch_json_200():
    """_fetch_json should return (200, parsed_json_list) on success."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[{"signal_id": "abc"}])

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(return_value=mock_resp)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_instance):
        status, body = await monitor_paper_book._fetch_json("https://test.url", {"apikey": "key"})
        assert status == 200
        assert body == [{"signal_id": "abc"}]


@pytest.mark.asyncio
async def test_fetch_json_400():
    """_fetch_json should return (400, error_body) on failure."""
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json = MagicMock(return_value={"message": "No API key found"})

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(return_value=mock_resp)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_instance):
        status, body = await monitor_paper_book._fetch_json("https://test.url", {"apikey": "key"})
        assert status == 400
        assert body == {"message": "No API key found"}


@pytest.mark.asyncio
async def test_fetch_count_parses_header():
    """_fetch_count should extract X-Total-Count from headers."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = MagicMock()
    mock_resp.headers.get = MagicMock(return_value="42")

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(return_value=mock_resp)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_instance):
        status, count = await monitor_paper_book._fetch_count("https://test.url", {"apikey": "key"})
        assert status == 200
        assert count == 42


@pytest.mark.asyncio
async def test_fetch_count_missing_header():
    """_fetch_count should default to 0 when X-Total-Count is missing."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = MagicMock()
    mock_resp.headers.get = MagicMock(return_value=None)

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(return_value=mock_resp)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=client_instance):
        status, count = await monitor_paper_book._fetch_count("https://test.url", {"apikey": "key"})
        assert status == 200
        assert count == 0


@pytest.mark.asyncio
async def test_check_paper_book_health_healthy():
    """Health check with all 200s and overlaps = healthy-ish metrics."""
    mock_resp_count_5 = MagicMock()
    mock_resp_count_5.status_code = 200
    mock_resp_count_5.headers = MagicMock()
    mock_resp_count_5.headers.get = MagicMock(return_value="5")

    mock_resp_count_3 = MagicMock()
    mock_resp_count_3.status_code = 200
    mock_resp_count_3.headers = MagicMock()
    mock_resp_count_3.headers.get = MagicMock(return_value="3")

    mock_resp_open = MagicMock()
    mock_resp_open.status_code = 200
    mock_resp_open.json = MagicMock(return_value=[{"position_id": "p1", "signal_id": "s1"}])

    mock_resp_resolved = MagicMock()
    mock_resp_resolved.status_code = 200
    mock_resp_resolved.json = MagicMock(return_value=[{"signal_id": "s1", "outcome": "tp_hit"}])

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(side_effect=[
        mock_resp_count_5,      # _fetch_count open positions
        mock_resp_count_3,      # _fetch_count closed positions
        mock_resp_open,         # _fetch_json open positions page 1
        mock_resp_resolved,     # _fetch_json resolved signals page 1
    ])
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SECRET_KEY": "test_key",
        "MONITOR_OPEN_THRESHOLD": "2000",
        "MONITOR_ORPHAN_THRESHOLD": "0",
        "PAPER_BOOK_METRICS_PATH": "/tmp/test_paper_book_metrics.json",
    }):
        with patch("httpx.AsyncClient", return_value=client_instance):
            metrics = await monitor_paper_book.check_paper_book_health()

    assert metrics["supabase_ok"] is True
    assert metrics["open_positions"] == 5
    assert metrics["closed_24h"] == 3
    assert metrics["orphan_count"] == 1  # s1 overlaps
    assert len(metrics["alerts"]) == 1  # orphan_count > 0 threshold


@pytest.mark.asyncio
async def test_check_paper_book_health_supabase_failure():
    """Health check with 400 on open positions = supabase_ok=False."""
    mock_resp_400 = MagicMock()
    mock_resp_400.status_code = 400
    mock_resp_400.headers = MagicMock()
    mock_resp_400.headers.get = MagicMock(return_value="0")

    client_instance = AsyncMock()
    client_instance.get = AsyncMock(return_value=mock_resp_400)
    client_instance.__aenter__ = AsyncMock(return_value=client_instance)
    client_instance.__aexit__ = AsyncMock(return_value=None)

    with patch.dict(os.environ, {
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SECRET_KEY": "test_key",
        "PAPER_BOOK_METRICS_PATH": "/tmp/test_paper_book_metrics_fail.json",
    }):
        with patch("httpx.AsyncClient", return_value=client_instance):
            metrics = await monitor_paper_book.check_paper_book_health()

    assert metrics["supabase_ok"] is False
    assert metrics["open_positions"] == 0
    assert any("FAILED" in a for a in metrics["alerts"])


def test_format_alert_healthy():
    """format_alert should show 'All metrics within thresholds' when no alerts."""
    metrics = {
        "timestamp": "2026-08-15T06:00:00+00:00",
        "supabase_ok": True,
        "open_positions": 500,
        "closed_24h": 12,
        "orphan_count": 0,
        "alerts": [],
    }
    report = monitor_paper_book.format_alert(metrics)
    assert "All metrics within thresholds" in report
    assert "Open positions: 500" in report


def test_format_alert_with_alerts():
    """format_alert should list alerts when present, with FAILED icon."""
    metrics = {
        "timestamp": "2026-08-15T06:00:00+00:00",
        "supabase_ok": False,
        "open_positions": 5000,
        "closed_24h": 0,
        "orphan_count": 5,
        "alerts": [
            "Open positions (5000) exceeds threshold (2000)",
            "Orphan count (5) exceeds threshold (0)",
        ],
    }
    report = monitor_paper_book.format_alert(metrics)
    assert "FAILED" in report
    assert "Open positions (5000) exceeds threshold (2000)" in report
    assert "Orphan count (5) exceeds threshold (0)" in report
    assert "All metrics within thresholds" not in report
