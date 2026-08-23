"""
Tests for regime_platform.core.tuning_registry — Layer 0 of agent-driven
EV engine parameter tuning (worklog/PARAMETER_TUNING_PLAYBOOK.md §2).

Covers: bounds clamping, file read/write + hot-reload, rate limiting,
the TUNING_AUTONOMY_ENABLED kill switch, and the Supabase DB-fallback /
audit-write paths (mocked — no live network).
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.core import tuning_registry as reg
from regime_platform.core.settings import settings


@pytest.fixture(autouse=True)
def _isolated_registry_file(tmp_path, monkeypatch):
    """Point the registry at a throwaway file per test and clear the
    lru_cache before/after so tests never see another test's state."""
    yaml_path = tmp_path / "tuning_registry.yaml"
    monkeypatch.setenv("TUNING_REGISTRY_YAML", str(yaml_path))
    reg.reload()
    yield yaml_path
    reg.reload()


# ─── clamp() ────────────────────────────────────────────────────────────

class TestClamp:
    def test_within_bounds_unchanged(self):
        assert reg.clamp("MIN_RR", 1.5) == 1.5

    def test_below_lower_bound_clamped_up(self):
        lo, _ = reg.PARAM_BOUNDS["MIN_RR"]
        assert reg.clamp("MIN_RR", 0.1) == lo

    def test_above_upper_bound_clamped_down(self):
        _, hi = reg.PARAM_BOUNDS["MIN_RR"]
        assert reg.clamp("MIN_RR", 100.0) == hi

    def test_unbounded_param_passes_through(self):
        assert reg.clamp("SOME_UNKNOWN_PARAM_NOT_IN_BOUNDS", 999.0) == 999.0


# ─── read_param() — file + default, sync ───────────────────────────────

class TestReadParamSync:
    def test_no_file_falls_back_to_default(self):
        assert reg.read_param("MIN_RR", default=1.5) == 1.5

    def test_no_file_no_default_raises(self):
        with pytest.raises(KeyError):
            reg.read_param("MIN_RR")

    def test_default_is_clamped(self):
        lo, _ = reg.PARAM_BOUNDS["MIN_RR"]
        assert reg.read_param("MIN_RR", default=0.01) == lo

    @pytest.mark.asyncio
    async def test_written_value_is_read_back(self):
        await reg.write_param("MIN_RR", 1.8, reason="test")
        assert reg.read_param("MIN_RR", default=2.0) == 1.8

    @pytest.mark.asyncio
    async def test_hot_reload_without_restart(self):
        """The whole point of file-based config over settings.py: a write
        is visible on the VERY NEXT read, no process restart needed."""
        assert reg.read_param("MIN_RR", default=2.0) == 2.0
        await reg.write_param("MIN_RR", 1.7, reason="test")
        assert reg.read_param("MIN_RR", default=2.0) == 1.7


# ─── write_param() — bounds requirement ─────────────────────────────────

class TestWriteParamBoundsRequirement:
    @pytest.mark.asyncio
    async def test_unbounded_param_rejected_by_default(self):
        with pytest.raises(ValueError):
            await reg.write_param("SOME_NEW_PARAM_NOT_YET_BOUNDED", 5.0, reason="test")

    @pytest.mark.asyncio
    async def test_unbounded_param_allowed_with_explicit_opt_out(self):
        result = await reg.write_param(
            "SOME_NEW_PARAM_NOT_YET_BOUNDED", 5.0, reason="test", require_bounds=False,
        )
        assert result["applied"] is True
        assert result["value"] == 5.0


# ─── write_param() — clamping ───────────────────────────────────────────

class TestWriteParamClamping:
    @pytest.mark.asyncio
    async def test_out_of_bounds_value_is_clamped(self):
        _, hi = reg.PARAM_BOUNDS["MIN_RR"]
        result = await reg.write_param("MIN_RR", 999.0, reason="test")
        assert result["clamped"] is True
        assert result["value"] == hi


# ─── write_param() — rate limiting ──────────────────────────────────────

class TestWriteParamRateLimit:
    @pytest.mark.asyncio
    async def test_first_write_is_never_rate_limited(self):
        """No previous value -> nothing to rate-limit against."""
        result = await reg.write_param("MIN_RR", 1.5, reason="seed")
        assert result["rate_limited"] is False
        assert result["value"] == 1.5

    @pytest.mark.asyncio
    async def test_large_jump_is_rate_limited(self):
        await reg.write_param("MIN_RR", 1.5, reason="seed")
        # MIN_RR bounds are (1.2, 3.0); jumping straight to 3.0 from 1.5
        # is a huge relative move and must be capped, not applied in full.
        result = await reg.write_param("MIN_RR", 3.0, reason="big jump", max_step_fraction=0.20)
        assert result["rate_limited"] is True
        assert result["value"] < 3.0
        assert result["value"] > 1.5

    @pytest.mark.asyncio
    async def test_small_step_is_not_rate_limited(self):
        await reg.write_param("MIN_RR", 1.5, reason="seed")
        result = await reg.write_param("MIN_RR", 1.55, reason="small step", max_step_fraction=0.20)
        assert result["rate_limited"] is False
        assert result["value"] == 1.55

    @pytest.mark.asyncio
    async def test_previous_value_preserved_in_result(self):
        await reg.write_param("MIN_RR", 1.5, reason="seed")
        result = await reg.write_param("MIN_RR", 1.6, reason="step")
        assert result["previous_value"] == 1.5


# ─── TUNING_AUTONOMY_ENABLED kill switch ────────────────────────────────

class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_disabled_autonomy_blocks_write(self, monkeypatch):
        monkeypatch.setattr(settings, "TUNING_AUTONOMY_ENABLED", False)
        result = await reg.write_param("MIN_RR", 1.8, reason="test")
        assert result["applied"] is False
        assert result["reason"] == "tuning_autonomy_disabled"
        # Read must be unaffected -- kill switch blocks WRITES, not reads.
        assert reg.read_param("MIN_RR", default=2.0) == 2.0

    @pytest.mark.asyncio
    async def test_enabled_autonomy_allows_write(self, monkeypatch):
        monkeypatch.setattr(settings, "TUNING_AUTONOMY_ENABLED", True)
        result = await reg.write_param("MIN_RR", 1.8, reason="test")
        assert result["applied"] is True


# ─── DB fallback (read_param_async) — mocked, no live network ──────────

class TestReadParamAsyncDbFallback:
    @pytest.mark.asyncio
    async def test_db_fallback_used_when_file_empty(self):
        fake_row = {"value": 1.75}
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_latest_tuning_param",
            new=AsyncMock(return_value=fake_row),
        ):
            value = await reg.read_param_async("MIN_RR", default=2.0)
        assert value == 1.75

    @pytest.mark.asyncio
    async def test_file_takes_precedence_over_db(self):
        await reg.write_param("MIN_RR", 1.6, reason="file wins")
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_latest_tuning_param",
            new=AsyncMock(return_value={"value": 2.9}),
        ):
            value = await reg.read_param_async("MIN_RR", default=2.0)
        assert value == 1.6  # file value, not the DB's 2.9

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_neither_file_nor_db_has_it(self):
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_latest_tuning_param",
            new=AsyncMock(return_value=None),
        ):
            value = await reg.read_param_async("MIN_RR", default=2.0)
        assert value == 2.0


# ─── Audit write is best-effort ─────────────────────────────────────────

class TestAuditWriteBestEffort:
    @pytest.mark.asyncio
    async def test_db_audit_failure_does_not_break_the_write(self):
        """The file write is authoritative for this process -- a broken
        Supabase insert must not raise or roll back the file write."""
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.insert_tuning_param_row",
            new=AsyncMock(side_effect=Exception("network exploded")),
        ):
            result = await reg.write_param("MIN_RR", 1.8, reason="test")
        assert result["applied"] is True
        assert reg.read_param("MIN_RR", default=2.0) == 1.8


# ─── get_history() ──────────────────────────────────────────────────────

class TestGetHistory:
    @pytest.mark.asyncio
    async def test_delegates_to_supabase_client(self):
        fake_history = [{"value": 1.8}, {"value": 1.5}]
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_tuning_param_history",
            new=AsyncMock(return_value=fake_history),
        ) as mock_fn:
            history = await reg.get_history("MIN_RR", limit=5)
        assert history == fake_history
        mock_fn.assert_awaited_once_with("MIN_RR", limit=5)
