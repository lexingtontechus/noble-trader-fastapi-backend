"""Tests for the trend-edge-by-symbol diagnostic
(regime_platform/sweep_orchestrator/trend_edge.py), worklog
20260912_EOD_CORRECTION_INTRADAY_SCOPE_AND_SELF_LEARNING_FOLLOWUPS.md
§12 -- the "w/l is not p/l" fix: ties trend_label to real win_rate +
expectancy_r (an R-multiple), not just directional hit rate, and
additionally buckets by trend_strength tier.

All Supabase I/O is mocked. Covers: alignment/strength-tier
classification (the pure logic determining which bucket a row lands
in), the expectancy_r computation, and best-effort orchestration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator import trend_edge


# ── _alignment_for ───────────────────────────────────────────────────

class TestAlignmentFor:
    def test_trending_up_buy_is_aligned(self):
        assert trend_edge._alignment_for("buy", "trending_up") == "aligned"

    def test_trending_up_sell_is_opposed(self):
        assert trend_edge._alignment_for("sell", "trending_up") == "opposed"

    def test_reversal_down_sell_is_aligned(self):
        assert trend_edge._alignment_for("sell", "reversal_down") == "aligned"

    def test_reversal_down_buy_is_opposed(self):
        assert trend_edge._alignment_for("buy", "reversal_down") == "opposed"

    def test_consolidating_is_neutral_regardless_of_signal(self):
        assert trend_edge._alignment_for("buy", "consolidating") == "neutral"
        assert trend_edge._alignment_for("sell", "consolidating") == "neutral"

    def test_missing_label_returns_none(self):
        assert trend_edge._alignment_for("buy", None) is None

    def test_unknown_label_returns_none(self):
        assert trend_edge._alignment_for("buy", "something_made_up") is None


# ── _strength_tier_for ───────────────────────────────────────────────

class TestStrengthTierFor:
    def test_low_tier(self):
        assert trend_edge._strength_tier_for(0.0) == "low"
        assert trend_edge._strength_tier_for(0.32) == "low"

    def test_med_tier(self):
        assert trend_edge._strength_tier_for(0.33) == "med"
        assert trend_edge._strength_tier_for(0.65) == "med"

    def test_high_tier(self):
        assert trend_edge._strength_tier_for(0.66) == "high"
        assert trend_edge._strength_tier_for(1.0) == "high"

    def test_none_returns_none(self):
        assert trend_edge._strength_tier_for(None) is None


# ── build_trend_edge_table ────────────────────────────────────────────

def _row(symbol, signal, outcome, trend_label, trend_strength, tp_bricks=2, sl_bricks=1, ts="2026-09-01T00:00:00+00:00", resolved_at="2026-09-01T10:00:00+00:00"):
    return {
        "symbol": symbol, "signal": signal, "outcome": outcome,
        "trend_label": trend_label, "trend_strength": trend_strength,
        "tp_bricks": tp_bricks, "sl_bricks": sl_bricks,
        "ts": ts, "resolved_at": resolved_at,
    }


class TestBuildTrendEdgeTable:
    @pytest.mark.asyncio
    async def test_empty_result_returns_empty_list_without_upsert(self):
        with (
            patch.object(trend_edge, "fetch_all_rows", new=AsyncMock(return_value=[])),
            patch.object(trend_edge, "upsert_trend_edge_by_symbol", new=AsyncMock()) as mock_upsert,
        ):
            rows = await trend_edge.build_trend_edge_table()
        assert rows == []
        mock_upsert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_empty_list_without_raising(self):
        with patch.object(trend_edge, "fetch_all_rows", new=AsyncMock(side_effect=RuntimeError("boom"))):
            rows = await trend_edge.build_trend_edge_table()  # must not raise
        assert rows == []

    @pytest.mark.asyncio
    async def test_aggregates_into_correct_bucket_with_expectancy(self):
        raw_rows = [
            _row("AAPL", "buy", "tp_hit", "trending_up", 0.8, tp_bricks=3, sl_bricks=1),
            _row("AAPL", "buy", "sl_hit", "trending_up", 0.8),
            _row("AAPL", "sell", "tp_hit", "trending_up", 0.8),  # opposed bucket
        ]
        with (
            patch.object(trend_edge, "fetch_all_rows", new=AsyncMock(return_value=raw_rows)),
            patch.object(trend_edge, "upsert_trend_edge_by_symbol", new=AsyncMock()) as mock_upsert,
        ):
            rows = await trend_edge.build_trend_edge_table()

        mock_upsert.assert_awaited_once()
        aligned = next(r for r in rows if r["symbol"] == "AAPL" and r["alignment"] == "aligned")
        assert aligned["strength_tier"] == "high"
        assert aligned["n"] == 2
        assert aligned["win_rate"] == 0.5
        assert aligned["avg_rr_winners"] == 3.0
        # expectancy_r = 0.5*3.0 - 0.5*1.0 = 1.0
        assert aligned["expectancy_r"] == 1.0

        opposed = next(r for r in rows if r["symbol"] == "AAPL" and r["alignment"] == "opposed")
        assert opposed["n"] == 1
        assert opposed["win_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_rows_missing_trend_strength_are_excluded(self):
        # None of these rows survive bucketing (no strength tier) -- the
        # code still calls upsert_trend_edge_by_symbol([]) unconditionally,
        # same as macro_edge.py; that function's own internal empty-list
        # guard is what makes this a safe no-op, not a special case here.
        raw_rows = [_row("AAPL", "buy", "tp_hit", "trending_up", None)]
        with (
            patch.object(trend_edge, "fetch_all_rows", new=AsyncMock(return_value=raw_rows)),
            patch.object(trend_edge, "upsert_trend_edge_by_symbol", new=AsyncMock()) as mock_upsert,
        ):
            rows = await trend_edge.build_trend_edge_table()
        assert rows == []
        mock_upsert.assert_awaited_once_with([])

    @pytest.mark.asyncio
    async def test_upsert_failure_does_not_raise(self):
        raw_rows = [_row("AAPL", "buy", "tp_hit", "trending_up", 0.5)]
        with (
            patch.object(trend_edge, "fetch_all_rows", new=AsyncMock(return_value=raw_rows)),
            patch.object(trend_edge, "upsert_trend_edge_by_symbol", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            rows = await trend_edge.build_trend_edge_table()  # must not raise
        assert len(rows) == 1


# ── Orchestrator wiring ───────────────────────────────────────────────

class TestOrchestratorTrendEdgeWiring:
    @pytest.mark.asyncio
    async def test_bracket_reachability_job_calls_build_trend_edge_table_when_mode_not_off(self):
        from regime_platform.sweep_orchestrator.orchestrator import SweepOrchestrator
        orch = object.__new__(SweepOrchestrator)

        with (
            patch("regime_platform.core.feature_flags.get_trend_regime_mode", return_value="shadow"),
            patch(
                "regime_platform.sweep_orchestrator.trend_edge.build_trend_edge_table",
                new=AsyncMock(return_value=[]),
            ) as mock_build,
            patch("regime_platform.sweep_orchestrator.bracket_reachability.build_reachability_table", new=AsyncMock(return_value=[])),
            patch("regime_platform.sweep_orchestrator.calibration_bias_bucket.build_calibration_bias_by_bucket", new=AsyncMock(return_value=[])),
            patch("regime_platform.sweep_orchestrator.calibration_bias_bucket.build_entry_drift", new=AsyncMock(return_value=[])),
            patch("regime_platform.sweep_orchestrator.macro_edge.build_macro_edge_table", new=AsyncMock(return_value=[])),
            patch("regime_platform.sweep_orchestrator.hmm_shadow_accuracy.build_hmm_shadow_accuracy", new=AsyncMock(return_value=[])),
        ):
            await orch._run_bracket_reachability()
        mock_build.assert_awaited_once()
