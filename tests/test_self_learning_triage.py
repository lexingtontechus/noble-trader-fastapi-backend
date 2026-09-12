"""Tests for the Phase 3 MVP LLM-triage module
(regime_platform/services/self_learning/triage.py), worklog
20260911_SELF_LEARNING_PHASE_3_4_FORECASTING_RESCOPE.md, RM-50.

All LLM/Supabase/embedding I/O is mocked — no real network calls. These
tests cover: JSON extraction from a raw model response, the Tier D
allowlist validation/clamping (the actual safety mechanism, since
nothing downstream reads this table to make a live decision but a
stored proposal should still be honest about bounds), and the
best-effort orchestration (one finding failing doesn't block the
rest, a finding is only marked triaged once a report was actually
produced).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.services.self_learning import triage


# ── _validate_proposal ───────────────────────────────────────────────

class TestValidateProposal:
    def test_valid_param_in_bounds_passes_through(self):
        param, value = triage._validate_proposal("KELLY_REDUCTION", 0.5)
        assert param == "KELLY_REDUCTION"
        assert value == 0.5

    def test_valid_param_out_of_bounds_gets_clamped(self):
        # KELLY_REDUCTION bounds are (0.10, 0.75) per tuning_registry.PARAM_BOUNDS
        param, value = triage._validate_proposal("KELLY_REDUCTION", 999.0)
        assert param == "KELLY_REDUCTION"
        assert value == 0.75

    def test_out_of_scope_param_is_discarded(self):
        param, value = triage._validate_proposal("MIN_RR", 2.0)  # Tier C, not in TIER_D_PILOT_PARAMS
        assert param is None
        assert value is None

    def test_unknown_param_name_is_discarded(self):
        param, value = triage._validate_proposal("SOMETHING_MADE_UP", 1.0)
        assert param is None
        assert value is None

    def test_none_param_passes_through_as_none(self):
        param, value = triage._validate_proposal(None, None)
        assert param is None
        assert value is None

    def test_non_numeric_value_is_discarded(self):
        param, value = triage._validate_proposal("KELLY_REDUCTION", "not a number")
        assert param is None
        assert value is None


# ── _fetch_timesfm_context ────────────────────────────────────────────

class TestFetchTimesfmContext:
    @pytest.mark.asyncio
    async def test_no_symbol_returns_none_without_network_call(self):
        with patch.object(triage, "fetch_latest_sweep", new=AsyncMock()) as mock_fetch:
            result = await triage._fetch_timesfm_context(None)
        assert result is None
        mock_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_row_returns_none(self):
        with patch.object(triage, "fetch_latest_sweep", new=AsyncMock(return_value=None)):
            result = await triage._fetch_timesfm_context("AAPL")
        assert result is None

    @pytest.mark.asyncio
    async def test_row_with_null_p_timesfm_returns_none(self):
        with patch.object(triage, "fetch_latest_sweep", new=AsyncMock(return_value={
            "symbol": "AAPL", "sweep_timestamp": "2026-09-12T10:00:00+00:00", "p_timesfm": None,
        })):
            result = await triage._fetch_timesfm_context("AAPL")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none(self):
        with patch.object(triage, "fetch_latest_sweep", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await triage._fetch_timesfm_context("AAPL")
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_row_computes_confidence_and_age(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with patch.object(triage, "fetch_latest_sweep", new=AsyncMock(return_value={
            "symbol": "AAPL", "sweep_timestamp": ts, "p_timesfm": 0.62,
        })):
            result = await triage._fetch_timesfm_context("AAPL")
        assert result["p_timesfm"] == 0.62
        assert result["confidence"] == pytest.approx(0.12)
        assert result["age_hours"] == pytest.approx(2.0, abs=0.05)

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_leaves_age_none(self):
        with patch.object(triage, "fetch_latest_sweep", new=AsyncMock(return_value={
            "symbol": "AAPL", "sweep_timestamp": "not-a-timestamp", "p_timesfm": 0.5,
        })):
            result = await triage._fetch_timesfm_context("AAPL")
        assert result["age_hours"] is None


# ── _format_timesfm_context ───────────────────────────────────────────

class TestFormatTimesfmContext:
    def test_none_renders_no_data_message(self):
        assert "no TimesFM forecast" in triage._format_timesfm_context(None)

    def test_present_renders_p_timesfm_and_age(self):
        text = triage._format_timesfm_context({
            "p_timesfm": 0.62, "confidence": 0.12, "age_hours": 2.0, "sweep_timestamp": "2026-09-12T10:00:00+00:00",
        })
        assert "0.6200" in text
        assert "2.0h old" in text

    def test_missing_age_renders_unknown(self):
        text = triage._format_timesfm_context({
            "p_timesfm": 0.5, "confidence": 0.0, "age_hours": None, "sweep_timestamp": None,
        })
        assert "age unknown" in text


# ── _build_prompt ─────────────────────────────────────────────────────

def test_build_prompt_includes_finding_and_allowed_params():
    finding = {"detector": "dead_mechanism", "symbol": None, "summary": "X is stale", "details": {"a": 1}}
    prompt = triage._build_prompt(finding, ["some code excerpt"], {"KELLY_REDUCTION": {"min": 0.1, "max": 0.75, "current": 0.3}})
    assert "X is stale" in prompt
    assert "some code excerpt" in prompt
    assert "KELLY_REDUCTION" in prompt
    assert "no TimesFM forecast" in prompt  # default timesfm_context=None


def test_build_prompt_includes_timesfm_context_when_given():
    finding = {"detector": "regime_price_mismatch", "symbol": "AAPL", "summary": "s", "details": {}}
    prompt = triage._build_prompt(
        finding, [], {},
        timesfm_context={"p_timesfm": 0.71, "confidence": 0.21, "age_hours": 1.5, "sweep_timestamp": "2026-09-12T10:00:00+00:00"},
    )
    assert "0.7100" in prompt
    assert "1.5h old" in prompt


# ── _call_llm (thin wrapper around llm_client.call_llm, tested there) ─

class TestCallLlm:
    @pytest.mark.asyncio
    async def test_delegates_to_shared_llm_client(self):
        with patch.object(triage, "call_llm", new=AsyncMock(return_value={"narrative": "n"})) as mock_call:
            result = await triage._call_llm("some prompt")
        assert result == {"narrative": "n"}
        mock_call.assert_awaited_once()
        args, kwargs = mock_call.await_args
        assert args[1] == "some prompt"  # (system_prompt, user_prompt)


# ── triage_finding ────────────────────────────────────────────────────

class TestTriageFinding:
    @pytest.mark.asyncio
    async def test_happy_path_returns_report_dict(self):
        finding = {"id": 1, "detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}}
        with (
            patch.object(triage, "semantic_search", return_value=[]),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={"KELLY_REDUCTION": {"min": 0.1, "max": 0.75, "current": 0.3}})),
            patch.object(triage, "_call_llm", new=AsyncMock(return_value={
                "narrative": "root cause narrative",
                "proposed_param": "KELLY_REDUCTION",
                "proposed_value": 0.4,
            })),
        ):
            report = await triage.triage_finding(finding)
        assert report["narrative"] == "root cause narrative"
        assert report["proposed_param"] == "KELLY_REDUCTION"
        assert report["proposed_value"] == 0.4
        assert report["current_value"] == 0.3

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self):
        finding = {"id": 1, "detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}}
        with (
            patch.object(triage, "semantic_search", return_value=[]),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={})),
            patch.object(triage, "_call_llm", new=AsyncMock(return_value=None)),
        ):
            report = await triage.triage_finding(finding)
        assert report is None

    @pytest.mark.asyncio
    async def test_missing_narrative_returns_none(self):
        finding = {"id": 1, "detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}}
        with (
            patch.object(triage, "semantic_search", return_value=[]),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={})),
            patch.object(triage, "_call_llm", new=AsyncMock(return_value={"narrative": "", "proposed_param": None, "proposed_value": None})),
        ):
            report = await triage.triage_finding(finding)
        assert report is None

    @pytest.mark.asyncio
    async def test_out_of_scope_param_dropped_from_final_report(self):
        finding = {"id": 1, "detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}}
        with (
            patch.object(triage, "semantic_search", return_value=[]),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={})),
            patch.object(triage, "_call_llm", new=AsyncMock(return_value={
                "narrative": "n", "proposed_param": "MIN_RR", "proposed_value": 2.0,
            })),
        ):
            report = await triage.triage_finding(finding)
        assert report["proposed_param"] is None
        assert report["proposed_value"] is None

    @pytest.mark.asyncio
    async def test_semantic_search_failure_still_proceeds(self):
        finding = {"id": 1, "detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}}
        with (
            patch.object(triage, "semantic_search", side_effect=RuntimeError("index missing")),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={})),
            patch.object(triage, "_call_llm", new=AsyncMock(return_value={"narrative": "n", "proposed_param": None, "proposed_value": None})),
        ):
            report = await triage.triage_finding(finding)
        assert report["narrative"] == "n"

    @pytest.mark.asyncio
    async def test_fetches_timesfm_context_for_the_findings_symbol_and_includes_it_in_prompt(self):
        finding = {"id": 1, "detector": "regime_price_mismatch", "symbol": "AAPL", "summary": "s", "details": {}}
        captured_prompt = {}

        async def _fake_call_llm(system_prompt, user_prompt, max_tokens=512):
            captured_prompt["text"] = user_prompt
            return {"narrative": "n", "proposed_param": None, "proposed_value": None}

        with (
            patch.object(triage, "semantic_search", return_value=[]),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={})),
            patch.object(triage, "_fetch_timesfm_context", new=AsyncMock(return_value={
                "p_timesfm": 0.8, "confidence": 0.3, "age_hours": 0.5, "sweep_timestamp": "2026-09-12T10:00:00+00:00",
            })) as mock_fetch,
            patch.object(triage, "call_llm", new=_fake_call_llm),
        ):
            await triage.triage_finding(finding)
        mock_fetch.assert_awaited_once_with("AAPL")
        assert "0.8000" in captured_prompt["text"]

    @pytest.mark.asyncio
    async def test_no_symbol_finding_still_proceeds_with_no_timesfm_context(self):
        finding = {"id": 1, "detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}}
        with (
            patch.object(triage, "semantic_search", return_value=[]),
            patch.object(triage, "_allowed_params_context", new=AsyncMock(return_value={})),
            patch.object(triage, "_call_llm", new=AsyncMock(return_value={"narrative": "n", "proposed_param": None, "proposed_value": None})),
        ):
            report = await triage.triage_finding(finding)
        assert report["narrative"] == "n"  # _fetch_timesfm_context(None) short-circuits to None, no crash


# ── run_triage ────────────────────────────────────────────────────────

class TestRunTriage:
    @pytest.mark.asyncio
    async def test_aggregates_counts_and_writes_reports(self):
        findings = [{"id": 1, "summary": "a"}, {"id": 2, "summary": "b"}]
        with (
            patch.object(triage, "fetch_untriaged_findings", new=AsyncMock(return_value=findings)),
            patch.object(triage, "triage_finding", new=AsyncMock(side_effect=[
                {"narrative": "n1", "proposed_param": None, "proposed_value": None, "current_value": None},
                {"narrative": "n2", "proposed_param": None, "proposed_value": None, "current_value": None},
            ])),
            patch.object(triage, "insert_self_learning_report", new=AsyncMock(return_value=True)) as mock_insert,
            patch.object(triage, "mark_finding_triaged", new=AsyncMock(return_value=True)) as mock_mark,
        ):
            summary = await triage.run_triage()
        assert summary["findings_seen"] == 2
        assert summary["reports_written"] == 2
        assert summary["skipped"] == 0
        assert summary["errors"] == 0
        assert mock_insert.await_count == 2
        assert mock_mark.await_count == 2

    @pytest.mark.asyncio
    async def test_skipped_finding_is_not_marked_triaged(self):
        findings = [{"id": 1, "summary": "a"}]
        with (
            patch.object(triage, "fetch_untriaged_findings", new=AsyncMock(return_value=findings)),
            patch.object(triage, "triage_finding", new=AsyncMock(return_value=None)),
            patch.object(triage, "insert_self_learning_report", new=AsyncMock(return_value=True)) as mock_insert,
            patch.object(triage, "mark_finding_triaged", new=AsyncMock(return_value=True)) as mock_mark,
        ):
            summary = await triage.run_triage()
        assert summary["skipped"] == 1
        assert summary["reports_written"] == 0
        mock_insert.assert_not_awaited()
        mock_mark.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_finding_failing_does_not_block_the_others(self):
        findings = [{"id": 1, "summary": "a"}, {"id": 2, "summary": "b"}]
        with (
            patch.object(triage, "fetch_untriaged_findings", new=AsyncMock(return_value=findings)),
            patch.object(triage, "triage_finding", new=AsyncMock(side_effect=[
                RuntimeError("boom"),
                {"narrative": "n2", "proposed_param": None, "proposed_value": None, "current_value": None},
            ])),
            patch.object(triage, "insert_self_learning_report", new=AsyncMock(return_value=True)),
            patch.object(triage, "mark_finding_triaged", new=AsyncMock(return_value=True)),
        ):
            summary = await triage.run_triage()  # must not raise
        assert summary["errors"] == 1
        assert summary["reports_written"] == 1

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_error_summary_without_raising(self):
        with patch.object(triage, "fetch_untriaged_findings", new=AsyncMock(side_effect=RuntimeError("boom"))):
            summary = await triage.run_triage()  # must not raise
        assert summary["errors"] == 1
        assert summary["findings_seen"] == 0


# ── Orchestrator wiring ───────────────────────────────────────────────

class TestOrchestratorSelfLearningTriageWiring:
    """_run_self_learning_triage is a thin, best-effort wrapper around
    run_triage -- same shape as _run_self_learning_detectors's own
    wrapper around run_all_detectors."""

    def _orch(self):
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
        return object.__new__(SweepOrchestrator)

    @pytest.mark.asyncio
    async def test_calls_run_triage(self):
        run_mock = AsyncMock(return_value={"findings_seen": 1})
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.triage.run_triage",
            new=run_mock,
        ):
            await orch._run_self_learning_triage()
        run_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_is_best_effort_never_raises(self):
        run_mock = AsyncMock(side_effect=RuntimeError("boom"))
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.triage.run_triage",
            new=run_mock,
        ):
            await orch._run_self_learning_triage()  # must not raise
