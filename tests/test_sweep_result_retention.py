"""
Tests for sweep_result/regime_log retention
(regime_platform/sweep_orchestrator/sweep_result_retention.py).

Root cause (2026-10-01, worklog
20261001_NT_RENKO_BRICKS_TIMEOUT_AND_GROWTH.md §7/§10/§11): nt_sweep_result
(103.41 MB, 17.09% of the database) and nt_regime_log (79.47 MB, 13.13%)
had NO retention pass at all. This module is the nightly hygiene pass
(mirrors renko_retention.py's/eod_retention.py's design and test shape):
prune rows older than sweep_result_retention_days / regime_log_retention_days
from each table independently.

Covers, with mocked HTTP (never touches real Supabase):
  - the nt_sweep_result DELETE cutoff shape (sweep_timestamp column)
  - the nt_regime_log DELETE cutoff shape (detected_at column)
  - the full run_sweep_result_retention flow: summary counts, never-raises
    on missing config or per-table errors, one table's failure does not
    block the other
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from regime_platform.sweep_orchestrator.sweep_result_retention import (
    _delete_old_regime_log,
    _delete_old_sweep_results,
    _parse_content_range_total,
    run_sweep_result_retention,
)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class FakeClient:
    """Minimal httpx.AsyncClient stand-in recording calls."""

    def __init__(self):
        self.calls = []
        self.delete_responses = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _record(self, method, url, params=None):
        self.calls.append({"method": method, "url": url, "params": params})

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


# ─── config getters ─────────────────────────────────────────────────────


def test_retention_days_getters_default():
    from regime_platform.core import feature_flags as ff

    with patch.object(ff, "_load", return_value={}):
        assert ff.get_sweep_result_retention_days() == 30
        assert ff.get_regime_log_retention_days() == 30


def test_retention_days_getters_override():
    from regime_platform.core import feature_flags as ff

    with patch.object(ff, "_load", return_value={
        "sweep_result_retention_days": 14,
        "regime_log_retention_days": 7,
    }):
        assert ff.get_sweep_result_retention_days() == 14
        assert ff.get_regime_log_retention_days() == 7


def test_retention_days_getters_bad_value_falls_back_to_default():
    from regime_platform.core import feature_flags as ff

    with patch.object(ff, "_load", return_value={
        "sweep_result_retention_days": "not-a-number",
        "regime_log_retention_days": None,
    }):
        assert ff.get_sweep_result_retention_days() == 30
        assert ff.get_regime_log_retention_days() == 30


# ─── _delete_old_sweep_results ─────────────────────────────────────────


class TestDeleteOldSweepResults:
    @pytest.mark.asyncio
    async def test_filter_uses_lt_cutoff_on_sweep_timestamp(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(headers={"content-range": "0-2/3"}))

        deleted = await _delete_old_sweep_results(client, "2026-09-01T00:00:00+00:00")

        assert deleted == 3
        call = client.calls[-1]
        assert call["method"] == "DELETE"
        assert call["params"]["sweep_timestamp"] == "lt.2026-09-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_http_failure_returns_zero_not_raise(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(status_code=500, text="boom"))
        deleted = await _delete_old_sweep_results(client, "2026-09-01T00:00:00+00:00")
        assert deleted == 0


# ─── _delete_old_regime_log ────────────────────────────────────────────


class TestDeleteOldRegimeLog:
    @pytest.mark.asyncio
    async def test_filter_uses_lt_cutoff_on_detected_at(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(headers={"content-range": "0-9/10"}))

        deleted = await _delete_old_regime_log(client, "2026-09-01T00:00:00+00:00")

        assert deleted == 10
        call = client.calls[-1]
        assert call["method"] == "DELETE"
        assert call["params"]["detected_at"] == "lt.2026-09-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_http_failure_returns_zero_not_raise(self):
        client = FakeClient()
        client.delete_responses.append(FakeResponse(status_code=500, text="boom"))
        deleted = await _delete_old_regime_log(client, "2026-09-01T00:00:00+00:00")
        assert deleted == 0


# ─── run_sweep_result_retention (full flow) ────────────────────────────


class TestRunSweepResultRetention:
    @pytest.mark.asyncio
    async def test_missing_supabase_config_returns_zero_summary(self):
        with patch(
            "regime_platform.sweep_orchestrator.sweep_result_retention.settings.SUPABASE_URL", "",
        ):
            summary = await run_sweep_result_retention()
        assert summary == {
            "deleted_sweep_result": 0,
            "deleted_regime_log": 0,
            "errors": 0,
        }

    @pytest.mark.asyncio
    async def test_full_flow_counts_both_tables(self):
        client = FakeClient()
        client.delete_responses.extend([
            FakeResponse(headers={"content-range": "0-49/50"}),   # nt_sweep_result prune
            FakeResponse(headers={"content-range": "0-19/20"}),   # nt_regime_log prune
        ])

        with (
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.settings.SUPABASE_URL",
                "https://test.supabase.co",
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.get_sweep_result_retention_days",
                return_value=30,
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.get_regime_log_retention_days",
                return_value=30,
            ),
        ):
            summary = await run_sweep_result_retention()

        assert summary["deleted_sweep_result"] == 50
        assert summary["deleted_regime_log"] == 20
        assert summary["errors"] == 0

    @pytest.mark.asyncio
    async def test_one_table_failure_does_not_block_the_other(self):
        """nt_sweep_result's delete raising must not prevent nt_regime_log's
        delete from still running — each table's pass is independently
        try/excepted (mirrors renko_retention's stale-brick_size vs.
        old-sessions independence)."""
        client = FakeClient()
        client.delete_responses.extend([
            FakeResponse(headers={"content-range": "0-19/20"}),  # nt_regime_log prune
        ])

        async def raising_delete_sweep_results(*args, **kwargs):
            raise RuntimeError("connection reset")

        with (
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.settings.SUPABASE_URL",
                "https://test.supabase.co",
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.httpx.AsyncClient",
                return_value=client,
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention._delete_old_sweep_results",
                side_effect=raising_delete_sweep_results,
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.get_sweep_result_retention_days",
                return_value=30,
            ),
            patch(
                "regime_platform.sweep_orchestrator.sweep_result_retention.get_regime_log_retention_days",
                return_value=30,
            ),
        ):
            summary = await run_sweep_result_retention()

        assert summary["errors"] == 1
        assert summary["deleted_sweep_result"] == 0
        assert summary["deleted_regime_log"] == 20
