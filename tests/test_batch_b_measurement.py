"""
Batch B measurement-honesty tests.

Worklog: 20260916_STREAMLINE_AND_CALIBRATION_REACHABILITY_SCOPE.md Phase 2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from regime_platform.core.settings import settings
from regime_platform.sweep_orchestrator.supabase_client import (
    _episode_summaries,
    _decay_weighted_episode_stats,
    fetch_calibration_bias_raw,
)
from regime_platform.sweep_orchestrator.eod_resolver import (
    _write_calibration_record,
)


class TestRawCaptureDefaults:
    def test_capture_default_true(self):
        assert settings.NOBLE_TRADER_SIM_CAPTURE_P_WIN_RAW is True

    def test_raw_required_default_true(self):
        assert settings.NOBLE_TRADER_CALIBRATION_RAW_REQUIRED is True


class TestEpisodeSummariesNoForcedFallback:
    def test_mixed_raw_and_null(self):
        now = datetime.now(timezone.utc).isoformat()
        episode = [
            {"predicted_p_win": 0.6, "predicted_p_win_raw": 0.9, "outcome": "win", "entered_at": now},
            {"predicted_p_win": 0.5, "predicted_p_win_raw": None, "outcome": "loss", "entered_at": now},
        ]
        s = _episode_summaries([episode])[0]
        assert s["predicted_p_win_raw"] == pytest.approx(0.9)
        assert s["n_raw_present"] == 1
        assert s["n_rows"] == 2

    def test_all_null_raw_is_none(self):
        episode = [
            {"predicted_p_win": 0.8, "predicted_p_win_raw": None, "outcome": "win", "entered_at": "2026-01-01T00:00:00+00:00"},
        ]
        s = _episode_summaries([episode])[0]
        assert s["predicted_p_win_raw"] is None


class TestDecaySkipNull:
    def test_skip_null_excludes_episode(self):
        now = datetime.now(timezone.utc)
        eps = [
            {"entered_at": now.isoformat(), "predicted_p_win": 0.9, "predicted_p_win_raw": None, "win_fraction": 0.0},
            {"entered_at": now.isoformat(), "predicted_p_win": 0.5, "predicted_p_win_raw": 0.8, "win_fraction": 1.0},
        ]
        avg_raw, win_rate = _decay_weighted_episode_stats(
            eps, "predicted_p_win_raw", 3.0, now, skip_null_pred=True,
        )
        assert avg_raw == pytest.approx(0.8)
        assert win_rate == pytest.approx(1.0)


class TestBrierRawOnly:
    def _write(self, sig, outcome="tp_hit"):
        captured = {}

        async def fake_insert(table, payload):
            captured["payload"] = payload
            return True

        resolved = {"outcome": outcome, "exit_price": 1.0, "hold_bars": 1}
        pnl = {"pnl_dollars": 1.0}

        async def run():
            with patch(
                "regime_platform.sweep_orchestrator.eod_resolver._insert_row",
                new=AsyncMock(side_effect=fake_insert),
            ):
                await _write_calibration_record(sig, resolved, pnl)

        import asyncio
        asyncio.run(run())
        return captured["payload"]

    def test_brier_uses_raw(self):
        p = self._write({
            "symbol": "EURUSD", "venue": "OANDA",
            "p_win": 0.55, "predicted_p_win_raw": 0.90,
            "entry_price": 1.1, "ts": "2026-08-12T00:00:00Z",
        })
        assert p["calibration_brier"] == pytest.approx(0.01)
        assert p["error_source"] == "eod_resolver"

    def test_brier_null_when_raw_missing(self):
        p = self._write({
            "symbol": "EURUSD", "venue": "OANDA",
            "p_win": 0.90,
            "entry_price": 1.1, "ts": "2026-08-12T00:00:00Z",
        })
        assert p["calibration_brier"] is None
        assert p["error_source"] == "raw_missing"


class TestProxyAtrQuarantine:
    def test_calibration_bias_bucket_skips_proxy_rows(self):
        """Proxy-ATR rows must not enter expectancy/bias."""
        import asyncio
        from regime_platform.sweep_orchestrator import calibration_bias_bucket as mod

        rows = [
            # stored ATR path
            {
                "symbol": "XAUUSD", "entry_price": 2500.0, "take_profit": 2520.0,
                "atr": 10.0, "outcome": "tp_hit", "p_win": 0.7,
                "ts": "2026-09-01T00:00:00Z", "resolved_at": "2026-09-01T10:00:00Z",
                "tp_bricks": 2, "sl_bricks": 1, "regime": "med_low_vol_bull",
            },
            # would-be proxy (no stored atr) — must be excluded
            {
                "symbol": "XAUUSD", "entry_price": 2500.0, "take_profit": 2540.0,
                "atr": None, "outcome": "sl_hit", "p_win": 0.7,
                "ts": "2026-09-01T00:00:00Z", "resolved_at": "2026-09-01T05:00:00Z",
                "tp_bricks": 4, "sl_bricks": 1, "regime": "med_low_vol_bull",
            },
        ]

        async def fake_fetch_all(table, select, filters):
            if table == "nt_signal_sim":
                return rows
            return []

        async def fake_proxy(symbols):
            return {"XAUUSD": 10.0}

        captured = []

        async def fake_upsert(output_rows):
            captured.extend(output_rows)

        with (
            patch.object(mod, "_fetch_all", new=AsyncMock(side_effect=fake_fetch_all)),
            patch.object(mod, "_current_atr_by_symbol", new=AsyncMock(side_effect=fake_proxy)),
            patch.object(mod, "upsert_calibration_bias_by_bucket", new=AsyncMock(side_effect=fake_upsert)),
        ):
            out = asyncio.run(mod.build_calibration_bias_by_bucket(["XAUUSD"]))

        # Only the stored-ATR row contributes
        assert out
        assert all(r["n"] == 1 for r in out)
        assert all(r["atr_source"] == "stored" for r in out)
