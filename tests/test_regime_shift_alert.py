"""
Tests for Layer 2's regime-shift alerting (worklog/PARAMETER_TUNING_PLAYBOOK.md
§2.5.3): wires the previously-unwired EVEngine.detect_regime_shift() into
the sweep, and writes a 'regime_shift_alert' row (nt_eod_actions,
migration 118) when the shift magnitude crosses
settings.REGIME_SHIFT_ALERT_THRESHOLD. Detection-only -- it flags Tier B/C
parameters for review, it does not itself change any parameter.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.services.statistics.ev_engine import EVEngine
from regime_platform.sweep_orchestrator.orchestrator import (
    SweepOrchestrator,
    _evaluate_regime_shift,
)


# ─── _evaluate_regime_shift — pure decision logic ──────────────────────

class TestEvaluateRegimeShift:
    def test_cold_start_no_prior_snapshot_returns_none(self):
        """First tick for a symbol -- nothing to compare against yet."""
        engine = EVEngine()
        result = _evaluate_regime_shift(
            engine, None, None, "high_vol_bull", {"bull": 0.8, "bear": 0.2}, 0.35,
        )
        assert result is None

    def test_no_label_change_returns_none(self):
        engine = EVEngine()
        probs = {"bull": 0.8, "bear": 0.2}
        result = _evaluate_regime_shift(
            engine, "high_vol_bull", probs, "high_vol_bull", probs, 0.35,
        )
        assert result is None

    def test_large_shift_above_threshold_returns_shift_info(self):
        engine = EVEngine()
        prev_probs = {"bull": 0.9, "bear": 0.1}
        curr_probs = {"bull": 0.1, "bear": 0.9}
        result = _evaluate_regime_shift(
            engine, "high_vol_bull", prev_probs, "high_vol_bear", curr_probs, 0.35,
        )
        assert result is not None
        assert result["shifted"] is True
        assert result["direction"] == "bearish"
        assert result["magnitude"] >= 0.35

    def test_small_shift_below_threshold_returns_none(self):
        """A label change with only a tiny posterior movement shouldn't
        alert -- the whole point of gating on magnitude, not just the
        boolean label-change flag."""
        engine = EVEngine()
        prev_probs = {"bear": 0.49, "neutral": 0.02, "bull": 0.49}
        curr_probs = {"bear": 0.48, "neutral": 0.02, "bull": 0.50}
        result = _evaluate_regime_shift(
            engine, "bear", prev_probs, "bull", curr_probs, 0.35,
        )
        assert result is None

    def test_threshold_is_configurable(self):
        engine = EVEngine()
        prev_probs = {"bull": 0.7, "bear": 0.3}
        curr_probs = {"bull": 0.3, "bear": 0.7}
        # magnitude here is 0.4 -- passes a lenient threshold, fails a strict one.
        lenient = _evaluate_regime_shift(engine, "bull", prev_probs, "bear", curr_probs, 0.30)
        strict = _evaluate_regime_shift(engine, "bull", prev_probs, "bear", curr_probs, 0.90)
        assert lenient is not None
        assert strict is None

    def test_unknown_prev_label_never_alerts(self):
        """detect_regime_shift() itself treats 'unknown' prev_label as
        not-shifted -- confirm that propagates through cleanly."""
        engine = EVEngine()
        result = _evaluate_regime_shift(
            engine, "unknown", {}, "high_vol_bull", {"bull": 0.9}, 0.35,
        )
        assert result is None


# ─── _write_regime_shift_alert ──────────────────────────────────────────

def _fake_orchestrator() -> SimpleNamespace:
    return SimpleNamespace()


@pytest.mark.asyncio
class TestWriteRegimeShiftAlert:
    async def test_writes_expected_row_shape(self):
        fake_self = _fake_orchestrator()
        shift_info = {
            "shifted": True, "direction": "bearish", "magnitude": 0.42,
            "confidence": 0.84, "prev_label": "high_vol_bull", "curr_label": "high_vol_bear",
        }
        insert_mock = AsyncMock(return_value=True)
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client._insert_row",
            new=insert_mock,
        ):
            await SweepOrchestrator._write_regime_shift_alert(fake_self, "AAPL", shift_info)

        insert_mock.assert_awaited_once()
        table, row = insert_mock.call_args.args
        assert table == "nt_eod_actions"
        assert row["symbol"] == "AAPL"
        assert row["action"] == "regime_shift_alert"
        assert row["bias"] == pytest.approx(0.42)
        assert row["detail"] == shift_info

    async def test_insert_failure_does_not_raise(self):
        fake_self = _fake_orchestrator()
        shift_info = {"shifted": True, "direction": "bullish", "magnitude": 0.5,
                       "confidence": 1.0, "prev_label": "a", "curr_label": "b"}
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client._insert_row",
            new=AsyncMock(side_effect=Exception("network exploded")),
        ):
            await SweepOrchestrator._write_regime_shift_alert(fake_self, "AAPL", shift_info)
        # No exception propagated -- best-effort, matches the module's
        # other write paths.

    async def test_missing_magnitude_defaults_to_zero_bias(self):
        fake_self = _fake_orchestrator()
        insert_mock = AsyncMock(return_value=True)
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client._insert_row",
            new=insert_mock,
        ):
            await SweepOrchestrator._write_regime_shift_alert(fake_self, "AAPL", {"shifted": True})
        _, row = insert_mock.call_args.args
        assert row["bias"] == 0.0
