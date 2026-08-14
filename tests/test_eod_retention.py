"""
Tests for EOD window anchoring + retention (2026-08-12 DB-health build).

Covers the pure ``anchor_post_entry_closes`` helper (the root-cause fix for
backlogged signals being scored against pre-entry bars) and the
``eod_retention`` prune pass (delete resolved / expire stale pending) with
mocked HTTP.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.eod_resolver import (
    anchor_post_entry_closes,
)
from regime_platform.sweep_orchestrator.eod_retention import (
    _parse_content_range_total,
    run_eod_retention,
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
        self.calls = []  # (method, url, params, json)
        self.head_responses = []
        self.write_responses = []
        self.get_responses = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, method, url, params=None, json=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})

    async def get(self, url, headers=None, params=None):
        self._record("GET", url, params=params)
        resp = self.get_responses.pop(0) if self.get_responses else FakeResponse(json_data=[])
        return resp

    async def head(self, url, headers=None, params=None):
        self._record("HEAD", url, params=params)
        resp = self.head_responses.pop(0) if self.head_responses else FakeResponse(headers={"content-range": "0-0/0"})
        return resp

    async def delete(self, url, headers=None, params=None):
        self._record("DELETE", url, params=params)
        resp = self.write_responses.pop(0) if self.write_responses else FakeResponse(headers={"content-range": "0-0/0"})
        return resp

    async def patch(self, url, headers=None, params=None, json=None):
        self._record("PATCH", url, params=params, json=json)
        resp = self.write_responses.pop(0) if self.write_responses else FakeResponse(headers={"content-range": "0-0/0"})
        return resp


# ─── anchor_post_entry_closes ─────────────────────────────────────


class TestAnchorPostEntryCloses:
    FETCH = "2026-08-12T00:30:00+00:00"
    # 200 ascending 1h closes; closes[-1] is the newest (~ fetch time)
    CLOSES = [100.0 + i * 0.01 for i in range(200)]

    def test_entry_at_mid_window_slices_from_entry(self):
        # entry 100h before fetch → entry_idx = len-1-offset = 200-1-100 = 99
        ts = "2026-08-07T20:30:00+00:00"
        post = anchor_post_entry_closes(self.CLOSES, ts, self.FETCH)
        assert post is not None
        assert post[0] == pytest.approx(self.CLOSES[99])
        assert len(post) == 101  # entry bar + 100 post-entry bars

    def test_entry_at_newest_returns_tail(self):
        # entry 1h before fetch → entry_idx = 200-1-1 = 198
        ts = "2026-08-11T23:30:00+00:00"
        post = anchor_post_entry_closes(self.CLOSES, ts, self.FETCH)
        assert post is not None
        assert len(post) == 2
        assert post[0] == pytest.approx(self.CLOSES[198])

    def test_entry_older_than_window_returns_none(self):
        # entry 300h before fetch → offset >= len(closes) → cannot resolve
        ts = "2026-07-30T12:30:00+00:00"
        assert anchor_post_entry_closes(self.CLOSES, ts, self.FETCH) is None

    def test_empty_closes_returns_none(self):
        assert anchor_post_entry_closes([], "2026-08-10T00:00:00+00:00", self.FETCH) is None

    def test_unparseable_ts_returns_none(self):
        assert anchor_post_entry_closes(self.CLOSES, "not-a-date", self.FETCH) is None

    def test_fractional_offset_rounds_to_nearest_bar(self):
        # 100.6h → rounds to 101 → entry_idx = 200-1-101 = 98
        ts = "2026-08-07T19:54:00+00:00"
        post = anchor_post_entry_closes(self.CLOSES, ts, self.FETCH)
        assert post is not None
        assert post[0] == pytest.approx(self.CLOSES[98])

    def test_future_dated_signal_clamps_to_newest(self):
        ts = "2026-08-12T01:00:00+00:00"  # 30 min after fetch (clock skew)
        post = anchor_post_entry_closes(self.CLOSES, ts, self.FETCH)
        assert post is not None
        assert post[0] == pytest.approx(self.CLOSES[199])


# ─── Content-Range parsing ────────────────────────────────────────


class TestParseContentRangeTotal:
    def test_exact(self):
        assert _parse_content_range_total("0-0/42") == 42

    def test_capped_star(self):
        assert _parse_content_range_total("0-999/*") == 0

    def test_empty(self):
        assert _parse_content_range_total("*/0") == 0

    def test_none(self):
        assert _parse_content_range_total(None) == 0


# ─── run_eod_retention ────────────────────────────────────────────


class TestRunEodRetention:
    @pytest.fixture
    def fake_client(self):
        return FakeClient()

    async def _run(self, fake_client, settings_patch):
        with (
            patch(
                "regime_platform.sweep_orchestrator.eod_retention._url",
                return_value="https://x.supabase.co/rest/v1/nt_signal_sim",
            ),
            patch(
                "regime_platform.sweep_orchestrator.eod_retention._headers",
                return_value={"apikey": "k", "Authorization": "Bearer k"},
            ),
            patch(
                "regime_platform.sweep_orchestrator.eod_retention.httpx.AsyncClient",
                return_value=fake_client,
            ),
            settings_patch,
        ):
            return await run_eod_retention()

    def test_deletes_resolved_and_expires_stale(self):
        client = FakeClient()
        client.head_responses = [
            FakeResponse(headers={"content-range": "0-0/500"}),  # resolved count
            FakeResponse(headers={"content-range": "0-0/300"}),  # stale count
        ]
        client.write_responses = [
            FakeResponse(headers={"content-range": "0-0/500"}),  # DELETE result
            FakeResponse(headers={"content-range": "0-0/300"}),  # PATCH result
        ]
        summary = asyncio.run(self._run(client, patch(
            "regime_platform.sweep_orchestrator.eod_retention.settings",
            SUPABASE_URL="https://x.supabase.co",
            EOD_RETENTION_RESOLVED_DAYS=30,
            EOD_RETENTION_STALE_DAYS=7,
        )))

        assert summary["deleted_resolved"] == 500
        assert summary["expired_stale"] == 300
        assert summary["errors"] == 0

        methods = [c["method"] for c in client.calls]
        assert methods == ["GET", "HEAD", "DELETE", "HEAD", "PATCH"]  # pos-protect, resolved count, delete, stale count, expire

        delete_call = next(c for c in client.calls if c["method"] == "DELETE")
        assert delete_call["method"] == "DELETE"
        assert delete_call["params"]["outcome"] == "not.is.null"
        assert "resolved_at" in delete_call["params"]

        patch_call = next(c for c in client.calls if c["method"] == "PATCH")
        assert patch_call["method"] == "PATCH"
        assert patch_call["params"]["outcome"] == "is.null"
        assert "ts" in patch_call["params"]
        assert patch_call["json"] == {"outcome": "expired"}

    def test_no_rows_no_writes(self):
        client = FakeClient()
        client.head_responses = [
            FakeResponse(headers={"content-range": "0-0/0"}),  # no resolved
            FakeResponse(headers={"content-range": "0-0/0"}),  # no stale
        ]
        summary = asyncio.run(self._run(client, patch(
            "regime_platform.sweep_orchestrator.eod_retention.settings",
            SUPABASE_URL="https://x.supabase.co",
            EOD_RETENTION_RESOLVED_DAYS=30,
            EOD_RETENTION_STALE_DAYS=7,
        )))
        assert summary == {"deleted_resolved": 0, "expired_stale": 0, "errors": 0}
        assert [c["method"] for c in client.calls] == ["GET", "HEAD", "HEAD"]  # pos-protect, resolved count, stale count

    def test_never_raises_on_http_errors(self):
        client = FakeClient()
        client.head_responses = [
            FakeResponse(status_code=400, headers={"content-range": "*/0"}),
            FakeResponse(status_code=400, headers={"content-range": "*/0"}),
        ]
        summary = asyncio.run(self._run(client, patch(
            "regime_platform.sweep_orchestrator.eod_retention.settings",
            SUPABASE_URL="https://x.supabase.co",
            EOD_RETENTION_RESOLVED_DAYS=30,
            EOD_RETENTION_STALE_DAYS=7,
        )))
        assert summary["errors"] == 2
        assert summary["deleted_resolved"] == 0
        assert summary["expired_stale"] == 0

    def test_protected_delete_uses_chunked_pagination(self):
        """Open positions trigger the keyset+chunk path — protected ids never deleted.

        Regression for the nightly HTTP 400 caused by a single
        ``not.in.(1000 UUIDs)`` URL exceeding the Supabase gateway limit
        (2026-08-13).
        """
        client = FakeClient()
        client.get_responses = [
            FakeResponse(json_data=[  # nt_paper_positions (open)
                {"signal_id": "PROTECT-A"},
                {"signal_id": "PROTECT-B"},
            ]),
            FakeResponse(json_data=[  # keyset page of resolved rows
                {"signal_id": "id-0001"},
                {"signal_id": "PROTECT-A"},  # must be skipped
                {"signal_id": "id-0002"},
            ]),
        ]
        client.head_responses = [
            FakeResponse(headers={"content-range": "0-0/0"}),  # no stale pending
        ]
        client.write_responses = [
            FakeResponse(headers={"content-range": "0-0/2"}),  # chunked DELETE result
        ]
        summary = asyncio.run(self._run(client, patch(
            "regime_platform.sweep_orchestrator.eod_retention.settings",
            SUPABASE_URL="https://x.supabase.co",
            EOD_RETENTION_RESOLVED_DAYS=30,
            EOD_RETENTION_STALE_DAYS=7,
        )))
        assert summary["deleted_resolved"] == 2
        assert summary["expired_stale"] == 0
        assert summary["errors"] == 0

        methods = [c["method"] for c in client.calls]
        assert methods == ["GET", "GET", "DELETE", "HEAD"]  # pos, page, chunked delete, stale count

        delete_call = next(c for c in client.calls if c["method"] == "DELETE")
        assert "signal_id" in delete_call["params"]
        assert delete_call["params"]["signal_id"].startswith("in.(")
        # Protected ids must NOT appear in the delete filter.
        assert "PROTECT-A" not in delete_call["params"]["signal_id"]
        assert "PROTECT-B" not in delete_call["params"]["signal_id"]
        # And the non-protected ids are deleted.
        assert "id-0001" in delete_call["params"]["signal_id"]
        assert "id-0002" in delete_call["params"]["signal_id"]
