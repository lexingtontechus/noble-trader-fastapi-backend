"""Tests for the long-term macro-forecast agent
(regime_platform/services/self_learning/macro_forecast_agent.py),
worklog 20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md §4.

All LLM/Supabase I/O is mocked. Covers: the trend/direction/strength
vocabulary validation (the actual safety mechanism — an out-of-vocab
label from the model gets discarded rather than stored, since a
downstream consumer of nt_macro_forecast would expect these exact
enum values), prompt building, and the best-effort per-symbol
orchestration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.services.self_learning import macro_forecast_agent as mfa


# ── _validate_classification ─────────────────────────────────────────

class TestValidateClassification:
    def test_valid_classification_passes_through(self):
        result = mfa._validate_classification({
            "narrative": "n", "trend": "trending_up", "direction": "bull", "strength": 0.7,
        })
        assert result == {
            "narrative": "n", "current_trend_label": "trending_up",
            "current_macro_label": "bull", "current_macro_strength": 0.7,
        }

    def test_strength_is_clamped_to_0_1(self):
        result = mfa._validate_classification({
            "narrative": "n", "trend": "consolidating", "direction": "neutral", "strength": 5.0,
        })
        assert result["current_macro_strength"] == 1.0

    def test_out_of_vocab_trend_is_discarded(self):
        result = mfa._validate_classification({
            "narrative": "n", "trend": "super_bullish", "direction": "bull", "strength": 0.5,
        })
        assert result is None

    def test_out_of_vocab_direction_is_discarded(self):
        result = mfa._validate_classification({
            "narrative": "n", "trend": "trending_up", "direction": "up", "strength": 0.5,
        })
        assert result is None

    def test_missing_narrative_is_discarded(self):
        result = mfa._validate_classification({
            "narrative": "", "trend": "trending_up", "direction": "bull", "strength": 0.5,
        })
        assert result is None

    def test_non_numeric_strength_is_discarded(self):
        result = mfa._validate_classification({
            "narrative": "n", "trend": "trending_up", "direction": "bull", "strength": "high",
        })
        assert result is None


# ── _build_prompt ─────────────────────────────────────────────────────

def test_build_prompt_includes_symbol_date_and_doc():
    today = datetime(2026, 9, 15, tzinfo=timezone.utc)
    prompt = mfa._build_prompt("HO", "some forecast doc text", today)
    assert "HO" in prompt
    assert "2026-09-15" in prompt
    assert "some forecast doc text" in prompt


# ── classify_symbol ───────────────────────────────────────────────────

class TestClassifySymbol:
    @pytest.mark.asyncio
    async def test_happy_path_includes_provider(self):
        with patch.object(mfa, "call_llm_with_provider", new=AsyncMock(return_value=(
            {"narrative": "n", "trend": "trending_up", "direction": "bull", "strength": 0.6}, "openrouter",
        ))):
            result = await mfa.classify_symbol("HO", "doc text")
        assert result["model_provider"] == "openrouter"
        assert result["current_trend_label"] == "trending_up"
        assert result["current_macro_label"] == "bull"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self):
        with patch.object(mfa, "call_llm_with_provider", new=AsyncMock(return_value=None)):
            result = await mfa.classify_symbol("HO", "doc text")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_classification_returns_none(self):
        with patch.object(mfa, "call_llm_with_provider", new=AsyncMock(return_value=(
            {"narrative": "n", "trend": "nonsense", "direction": "bull", "strength": 0.6}, "openrouter",
        ))):
            result = await mfa.classify_symbol("HO", "doc text")
        assert result is None


# ── run_macro_forecast ────────────────────────────────────────────────

class TestRunMacroForecast:
    @pytest.mark.asyncio
    async def test_aggregates_and_upserts(self):
        with (
            patch.object(mfa, "_load_forecast_doc", return_value="doc text"),
            patch.object(mfa, "classify_symbol", new=AsyncMock(side_effect=[
                {"narrative": "n1", "current_trend_label": "trending_up", "current_macro_label": "bull", "current_macro_strength": 0.6, "model_provider": "openrouter"},
                None,
            ])),
            patch.object(mfa, "upsert_macro_forecast", new=AsyncMock()) as mock_upsert,
        ):
            summary = await mfa.run_macro_forecast(symbols=("HO", "CL"))
        assert summary["symbols_seen"] == 2
        assert summary["classified"] == 1
        assert summary["skipped"] == 1
        assert summary["errors"] == 0
        mock_upsert.assert_awaited_once()
        rows = mock_upsert.await_args.args[0]
        assert len(rows) == 1
        assert rows[0]["symbol"] == "HO"

    @pytest.mark.asyncio
    async def test_one_symbol_failing_does_not_block_the_others(self):
        with (
            patch.object(mfa, "_load_forecast_doc", return_value="doc text"),
            patch.object(mfa, "classify_symbol", new=AsyncMock(side_effect=[
                RuntimeError("boom"),
                {"narrative": "n2", "current_trend_label": "consolidating", "current_macro_label": "neutral", "current_macro_strength": 0.1, "model_provider": "nvidia_nim"},
            ])),
            patch.object(mfa, "upsert_macro_forecast", new=AsyncMock()),
        ):
            summary = await mfa.run_macro_forecast(symbols=("HO", "CL"))  # must not raise
        assert summary["errors"] == 1
        assert summary["classified"] == 1

    @pytest.mark.asyncio
    async def test_doc_load_failure_returns_error_summary_without_raising(self):
        with patch.object(mfa, "_load_forecast_doc", side_effect=RuntimeError("boom")):
            summary = await mfa.run_macro_forecast(symbols=("HO", "CL"))  # must not raise
        assert summary["errors"] == 2

    @pytest.mark.asyncio
    async def test_no_classified_rows_does_not_call_upsert(self):
        with (
            patch.object(mfa, "_load_forecast_doc", return_value="doc text"),
            patch.object(mfa, "classify_symbol", new=AsyncMock(return_value=None)),
            patch.object(mfa, "upsert_macro_forecast", new=AsyncMock()) as mock_upsert,
        ):
            summary = await mfa.run_macro_forecast(symbols=("HO",))
        assert summary["classified"] == 0
        mock_upsert.assert_not_awaited()


# ── Orchestrator wiring ───────────────────────────────────────────────

class TestOrchestratorMacroForecastAgentWiring:
    def _orch(self):
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
        return object.__new__(SweepOrchestrator)

    @pytest.mark.asyncio
    async def test_calls_run_macro_forecast(self):
        run_mock = AsyncMock(return_value={"symbols_seen": 12})
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.macro_forecast_agent.run_macro_forecast",
            new=run_mock,
        ):
            await orch._run_macro_forecast_agent()
        run_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_is_best_effort_never_raises(self):
        run_mock = AsyncMock(side_effect=RuntimeError("boom"))
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.macro_forecast_agent.run_macro_forecast",
            new=run_mock,
        ):
            await orch._run_macro_forecast_agent()  # must not raise
