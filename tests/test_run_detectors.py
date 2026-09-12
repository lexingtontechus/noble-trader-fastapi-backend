"""Tests for the Phase 2 detector orchestration
(regime_platform/services/self_learning/run_detectors.py),
self_learning_phase_2_detectors (RM-37).

All Supabase I/O is mocked — this tests the WIRING (each detector's
findings get written, failures in one detector don't block the
others), not real data. Real-data validation for detector 1 already
happened by hand (worklog SS10); these tests are structural.
"""

from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.services.self_learning import run_detectors


@pytest.mark.asyncio
async def test_run_all_detectors_aggregates_counts_and_writes_findings():
    with (
        patch.object(
            run_detectors, "_run_regime_price_mismatch_detector",
            new=AsyncMock(return_value=[
                {"detector": "regime_price_mismatch", "symbol": "AAPL", "summary": "s1", "details": {}},
            ]),
        ),
        patch.object(
            run_detectors, "_run_dead_mechanism_detector",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            run_detectors, "_run_redundant_computation_detector",
            new=AsyncMock(return_value=[
                {"detector": "redundant_computation", "symbol": None, "summary": "s2", "details": {}},
            ]),
        ),
        patch.object(run_detectors, "insert_self_learning_finding", new=AsyncMock(return_value=True)) as mock_insert,
    ):
        summary = await run_detectors.run_all_detectors()

    assert summary["regime_price_mismatch"] == 1
    assert summary["dead_mechanism"] == 0
    assert summary["redundant_computation"] == 1
    assert summary["errors"] == 0
    assert mock_insert.await_count == 2


@pytest.mark.asyncio
async def test_one_detector_failing_does_not_block_the_others():
    with (
        patch.object(
            run_detectors, "_run_regime_price_mismatch_detector",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(
            run_detectors, "_run_dead_mechanism_detector",
            new=AsyncMock(return_value=[
                {"detector": "dead_mechanism", "symbol": None, "summary": "s", "details": {}},
            ]),
        ),
        patch.object(
            run_detectors, "_run_redundant_computation_detector",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(run_detectors, "insert_self_learning_finding", new=AsyncMock(return_value=True)),
    ):
        summary = await run_detectors.run_all_detectors()  # must not raise

    assert summary["errors"] == 1
    assert summary["dead_mechanism"] == 1


class TestOrchestratorSelfLearningDetectorsWiring:
    """_run_self_learning_detectors is a thin, best-effort wrapper
    around run_all_detectors -- same shape as _run_knowledge_reindex's
    own wrapper around build_index."""

    def _orch(self):
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
        return object.__new__(SweepOrchestrator)

    @pytest.mark.asyncio
    async def test_calls_run_all_detectors(self):
        run_mock = AsyncMock(return_value={"regime_price_mismatch": 1})
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.run_detectors.run_all_detectors",
            new=run_mock,
        ):
            await orch._run_self_learning_detectors()
        run_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failure_is_best_effort_never_raises(self):
        run_mock = AsyncMock(side_effect=RuntimeError("boom"))
        orch = self._orch()
        with patch(
            "regime_platform.services.self_learning.run_detectors.run_all_detectors",
            new=run_mock,
        ):
            await orch._run_self_learning_detectors()  # must not raise
