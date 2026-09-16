"""
Batch D — qualification/EV cleanup tests (Phase 4).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.sweep_orchestrator.orchestrator import (
    TIMESFM_SIGMOID_K,
    TIMESFM_CLAMP_MIN,
    TIMESFM_CLAMP_MAX,
    MARKOV_HOLD_STEPS,
    resolve_markov_n_steps,
)
from regime_platform.sweep_orchestrator.calibration_enforcement import (
    apply_calibration_adjustment,
)


class TestTimesfmDefaults:
    def test_code_defaults_match_production_intent(self):
        assert TIMESFM_SIGMOID_K == 10.0
        assert TIMESFM_CLAMP_MIN == 0.05
        assert TIMESFM_CLAMP_MAX == 0.95


class TestMarkovNSteps:
    def test_prefers_tp_bricks(self):
        assert resolve_markov_n_steps(6) == 6
        assert resolve_markov_n_steps(5.0) == 5

    def test_fallback_when_invalid(self):
        assert resolve_markov_n_steps(None) == MARKOV_HOLD_STEPS
        assert resolve_markov_n_steps(0) == MARKOV_HOLD_STEPS
        assert resolve_markov_n_steps(-1) == MARKOV_HOLD_STEPS

    def test_custom_fallback(self):
        assert resolve_markov_n_steps(None, fallback=3) == 3


async def _run_enforce(status: str, p_win: float = 0.70, kelly: float = 0.20, bias: float = 0.25):
    state = {
        "action": "kelly_reduction",
        "bias": bias,
        "status": status,
        "n_records": 30,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "detail": {"n": 30},
    }
    with patch(
        "regime_platform.sweep_orchestrator.calibration_enforcement.settings"
    ) as mock_settings:
        mock_settings.SUPABASE_URL = "https://x.supabase.co"
        mock_settings.SUPABASE_KEY = "k"
        mock_settings.EOD_CALIBRATION_ENFORCE = True
        with patch(
            "regime_platform.sweep_orchestrator.supabase_client.fetch_calibration_state",
            new=AsyncMock(return_value=state),
        ):
            return await apply_calibration_adjustment("EURUSD", p_win=p_win, kelly_f=kelly)


class TestUnderconfidenceNoKellyCut:
    def test_underconfident_keeps_kelly(self):
        import asyncio
        r = asyncio.run(_run_enforce("UNDERCONFIDENT", p_win=0.70, kelly=0.20, bias=-0.25))
        assert r["applied"] is True
        assert r["kelly_f"] == pytest.approx(0.20)
        assert r["reason"] == "underconfidence_no_kelly_cut"

    def test_overconfident_still_cuts_kelly(self):
        import asyncio
        r = asyncio.run(_run_enforce("OVERCONFIDENT", p_win=0.70, kelly=0.20, bias=0.25))
        assert r["applied"] is True
        assert r["kelly_f"] == pytest.approx(0.10)
