"""Kairos v3 P0 capture (docs/scopes/20260924_KAIROS_V3_SCOPE.md §6 P0).

Covers the frozen factor definitions, the drift horizons written by the
resolver, the run tracker, the calendar merge, the payload stamping, the
price_fetcher OHLCV out-parameter, the schema-lag insert fallback, and the
kairos_mode flag. Nothing here may change qualification or sizing.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from regime_platform.sweep_orchestrator import kairos as K


def _bar(t, o, h, low, c, v):
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v}


class TestFrozenDefinitions:
    def test_regime_cell(self):
        assert K.regime_cell("med_high_vol_strong_bear") == "med_high_vol|bear"
        assert K.regime_cell("low_vol_weak_bull") == "low_vol|bull"
        assert K.regime_cell("unknown") is None
        assert K.regime_cell(None) is None

    def test_clv_bounds_and_flat_bar(self):
        assert K.clv(_bar(0, 1, 2, 0, 2, 1)) == pytest.approx(1.0)
        assert K.clv(_bar(0, 1, 2, 0, 0, 1)) == pytest.approx(-1.0)
        assert K.clv(_bar(0, 1, 2, 0, 1, 1)) == pytest.approx(0.0)
        assert K.clv(_bar(0, 1, 1, 1, 1, 1)) == 0.0          # H == L

    def test_efficiency_ratio(self):
        straight = [float(i) for i in range(30)]
        assert K.efficiency_ratio(straight, 24) == pytest.approx(1.0)
        zigzag = [0.0, 1.0] * 15
        assert K.efficiency_ratio(zigzag, 24) == pytest.approx(0.0)
        assert K.efficiency_ratio([1.0] * 10, 24) is None

    def test_relative_volume_same_hour(self):
        # 5 days of hourly bars; the last bar (hour 10) has 3x its hour's median
        bars = []
        for day in range(5):
            for hr in range(24):
                t = day * 86400 + hr * 3600
                bars.append(_bar(t, 1, 1, 1, 1, 100.0 if hr == 10 else 1.0))
        last_t = 5 * 86400 + 10 * 3600
        bars.append(_bar(last_t, 1, 1, 1, 1, 300.0))
        assert K.relative_volume(bars) == pytest.approx(3.0)
        assert K.relative_volume(bars[:2]) is None            # < 3 same-hour samples

    def test_capture_factors_excludes_forming_bar(self):
        bars = [_bar(i * 3600, 1, 2, 0, 1 + i * 0.01, 10.0) for i in range(80)]
        forming = _bar(80 * 3600, 1, 999, -999, 999, 1e9)     # absurd — must be ignored
        out = K.capture_factors(bars + [forming], atr_now=2.0, atr_run_start=1.0,
                                spread_bps=1.5, forming_last=True)
        assert set(out) == set(K.KAIROS_CAPTURE_COLUMNS)
        assert out["atr_ratio_run"] == pytest.approx(2.0)
        assert out["spread_bps"] == pytest.approx(1.5)
        assert out["efficiency_24h"] == pytest.approx(1.0)
        assert -1.0 <= out["clv_1h"] <= 1.0
        assert out["rel_volume_1h"] == pytest.approx(1.0)

    def test_capture_factors_missing_inputs_are_none_not_zero(self):
        out = K.capture_factors(None, atr_now=None, atr_run_start=None, spread_bps=None)
        assert all(v is None for v in out.values())
        out = K.capture_factors([], atr_now=1.0, atr_run_start=0.0, spread_bps=float("nan"))
        assert out["atr_ratio_run"] is None and out["spread_bps"] is None

    def test_drift_fields_horizons_and_sign(self):
        closes = [100.0 + i for i in range(60)]
        buy = K.drift_fields(100.0, closes, True)
        sell = K.drift_fields(100.0, closes, False)
        assert set(buy) == {f"drift_{h}h_pct" for h in K.DRIFT_HORIZONS}
        assert buy["drift_12h_pct"] == pytest.approx(12.0)
        assert buy["drift_48h_pct"] == pytest.approx(48.0)
        assert sell["drift_12h_pct"] == pytest.approx(-12.0)
        short = K.drift_fields(100.0, closes[:13], True)
        assert short["drift_12h_pct"] is not None and short["drift_24h_pct"] is None


class TestResolverDrift:
    def test_resolver_emits_12h_and_keeps_existing(self):
        from regime_platform.sweep_orchestrator.eod_resolver import (
            EXPIRY_BARS,
            resolve_signal_outcome,
        )
        closes = [100.0] * (EXPIRY_BARS + 5)
        out = resolve_signal_outcome(100.0, 90.0, 200.0, closes, direction="buy")
        for k in ("drift_1h_pct", "drift_4h_pct", "drift_12h_pct", "drift_24h_pct"):
            assert out[k] == pytest.approx(0.0)
        # window is closes[:48] → index 48 unobservable at resolve time
        assert out["drift_48h_pct"] is None

    def test_resolver_whitelist_includes_new_drift(self):
        from regime_platform.sweep_orchestrator.eod_resolver import NT_SIGNAL_SIM_COLUMNS as R
        from regime_platform.sweep_orchestrator.supabase_client import NT_SIGNAL_SIM_COLUMNS as S
        for c in ("drift_12h_pct", "drift_48h_pct"):
            assert c in R and c in S
        for c in K.KAIROS_CAPTURE_COLUMNS:
            assert c in S


class TestRunTracker:
    def test_run_start_and_atr_at_start(self):
        tr = K.KairosRunTracker()
        age, a0 = tr.update("X", "low_vol_weak_bull", 0.0, 1.0)
        assert age == 0.0 and a0 == 1.0
        age, a0 = tr.update("X", "low_vol_strong_bull", 7200.0, 3.0)   # same cell
        assert age == pytest.approx(2.0) and a0 == 1.0
        age, a0 = tr.update("X", "high_vol_strong_bear", 10800.0, 5.0)  # new cell
        assert age == 0.0 and a0 == 5.0
        assert tr.update("X", "garbage", 0.0, 1.0) == (None, None)


class TestCalendarMerge:
    def test_merge_filters_and_newer_wins(self):
        old = [{"id": "1", "date": "2026-09-16T18:00:00Z", "currency": "USD", "importance": 1, "actual": None}]
        new = [
            {"id": "1", "date": "2026-09-16T18:00:00Z", "currency": "USD", "importance": 1, "actual": 4.5},
            {"id": "2", "date": "2026-09-17T06:00:00Z", "currency": "SKK", "importance": 1},   # not our ccy
            {"id": "3", "date": "2026-09-17T07:00:00Z", "currency": "EUR", "importance": -1},  # too low
            {"id": "4", "date": "2026-09-15T12:30:00Z", "currency": "EUR", "importance": 0},
        ]
        out = K.merge_calendar(old, new)
        assert [e["id"] for e in out] == ["4", "1"]
        assert out[1]["actual"] == 4.5


class TestAnchor:
    def test_anchor_by_timestamp_handles_weekend_gap(self):
        ts = [0.0, 3600.0, 7200.0, 7200.0 + 48 * 3600, 7200.0 + 49 * 3600]
        assert K.anchor_by_timestamp(ts, 3600.0 + 1800) == 1
        assert K.anchor_by_timestamp(ts, 7200.0 + 48 * 3600 + 10) == 3
        assert K.anchor_by_timestamp(ts, 7200.0 + 10 * 3600) is None   # inside the gap
        assert K.anchor_by_timestamp(ts, -100.0) is None


class TestPayloadStamp:
    def _payload(self, kairos):
        from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
        return _build_signal_sim_payload(
            symbol="EURUSD", venue="OANDA", ts="2026-09-24T00:00:00Z", signal="buy",
            entry_price=1.1, stop_loss=1.09, take_profit=1.12, brick_size=0.001,
            sl_bricks=3, tp_bricks=6, p_win=0.5, ev=0.0, regime="low_vol_weak_bull",
            regime_conf=0.8, source="sweep-light", qualified=False, kairos=kairos,
        )

    def test_stamps_only_non_none(self):
        p = self._payload({"clv_1h": 0.5, "spread_bps": None, "efficiency_24h": 0.3})
        assert p["clv_1h"] == 0.5 and p["efficiency_24h"] == 0.3
        assert "spread_bps" not in p

    def test_no_kairos_is_byte_identical(self):
        p = self._payload(None)
        assert not any(k in p for k in K.KAIROS_CAPTURE_COLUMNS)
        assert not any(k in p for k in ("gate_outcome", "suppress_reason", "failed_gates"))

    def test_gate_outcome_stamps(self):
        from regime_platform.sweep_orchestrator.orchestrator import _build_signal_sim_payload
        from regime_platform.sweep_orchestrator.supabase_client import NT_SIGNAL_SIM_COLUMNS
        p = _build_signal_sim_payload(
            symbol="EURUSD", venue="OANDA", ts="2026-09-24T00:00:00Z", signal="buy",
            entry_price=1.1, stop_loss=1.09, take_profit=1.12, brick_size=0.001,
            sl_bricks=3, tp_bricks=6, p_win=0.6, ev=0.1, regime="low_vol_weak_bull",
            regime_conf=0.8, source="sweep-light", qualified=False,
            gate_outcome="suppressed", suppress_reason="cooldown_time", failed_gates=None,
        )
        assert p["gate_outcome"] == "suppressed" and p["suppress_reason"] == "cooldown_time"
        assert "failed_gates" not in p
        q = _build_signal_sim_payload(
            symbol="EURUSD", venue="OANDA", ts="2026-09-24T00:00:00Z", signal="sell",
            entry_price=1.1, stop_loss=1.11, take_profit=1.08, brick_size=0.001,
            sl_bricks=3, tp_bricks=6, p_win=0.4, ev=-0.1, regime="low_vol_weak_bear",
            regime_conf=0.5, source="sweep-light", qualified=False,
            gate_outcome="gate_fail", failed_gates=["kelly_min_ok", "p_win_ok"],
        )
        assert q["failed_gates"] == "kelly_min_ok,p_win_ok" and "suppress_reason" not in q
        assert {"gate_outcome", "suppress_reason", "failed_gates"} <= NT_SIGNAL_SIM_COLUMNS

    def test_gate_outcome_wired_in_orchestrator(self):
        """Every suppression path that flips a gate-passing signal to
        qualified=False must set a suppress_reason (static guard)."""
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath(
            "regime_platform/sweep_orchestrator/orchestrator.py").read_text(encoding="utf-8")
        for reason in ("cooldown_open", "cooldown_time", "content_dedup", "bracket_gate", "geometry_gate"):
            assert f'_suppress_reason = "{reason}"' in src
        assert "_gates_pass = bool(qualified)" in src and "gate_outcome=(" in src


class TestInsertFallback:
    def test_retry_without_kairos_on_failure(self, monkeypatch):
        from regime_platform.sweep_orchestrator.supabase_client import log_signal_sim
        calls = []

        async def fake_insert(table, payload):
            calls.append(dict(payload))
            return len(calls) > 1          # first attempt fails (schema lag)

        monkeypatch.setattr("regime_platform.sweep_orchestrator.supabase_client._insert_row", fake_insert)
        monkeypatch.setattr("regime_platform.core.settings.settings.SUPABASE_URL", "https://x.supabase.co")
        asyncio.run(log_signal_sim({"symbol": "EURUSD", "clv_1h": 0.2, "spread_bps": 1.0}))
        assert len(calls) == 2
        assert "clv_1h" in calls[0] and "clv_1h" not in calls[1] and "spread_bps" not in calls[1]
        assert calls[0]["signal_id"] == calls[1]["signal_id"]

    def test_no_retry_when_no_kairos_keys(self, monkeypatch):
        from regime_platform.sweep_orchestrator.supabase_client import log_signal_sim
        calls = []

        async def fake_insert(table, payload):
            calls.append(payload)
            return False

        monkeypatch.setattr("regime_platform.sweep_orchestrator.supabase_client._insert_row", fake_insert)
        monkeypatch.setattr("regime_platform.core.settings.settings.SUPABASE_URL", "https://x.supabase.co")
        asyncio.run(log_signal_sim({"symbol": "EURUSD"}))
        assert len(calls) == 1


class TestPriceFetcherOhlcvOut:
    @pytest.mark.asyncio
    async def test_ohlcv_out_filled_ascending_and_return_unchanged(self):
        from regime_platform.sweep_orchestrator.price_fetcher import fetch_tradingview_batch

        def bar(t, c, v):
            return {"time": t, "open": c - 0.1, "close": c, "max": c + 0.2, "min": c - 0.3, "volume": v}

        payload = {"success": True, "data": {"data": [{
            "success": True, "symbol": "OANDA:EURUSD",
            "history": [bar(3000, 1.3, 30.0), bar(2000, 1.2, 20.0), bar(1000, 1.1, 10.0)],
        }]}}
        resp = MagicMock(status_code=200, headers={}, text=json.dumps(payload))
        resp.json.return_value = payload
        client = AsyncMock()
        client.post.return_value = resp
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        with patch("regime_platform.core.settings.settings") as s, \
             patch("regime_platform.sweep_orchestrator.price_fetcher.httpx.AsyncClient", return_value=client):
            s.TRADINGVIEW_API_KEY, s.TRADINGVIEW_API_HOST, s.TRADINGVIEW_BASE_URL = "k", "h", "https://h"
            out: dict = {}
            prices = await fetch_tradingview_batch(
                [{"symbol": "EURUSD", "tradingview_symbol": "OANDA:EURUSD"}], hours=3, ohlcv_out=out)
        assert prices == {"EURUSD": [1.1, 1.2, 1.3]}                 # default shape untouched
        rows = out["EURUSD"]
        assert [r["t"] for r in rows] == [1000.0, 2000.0, 3000.0]
        assert rows[0]["v"] == 10.0 and rows[0]["h"] == pytest.approx(1.3) and rows[0]["l"] == pytest.approx(0.8)


class TestFlag:
    def test_kairos_mode_default_and_fallback(self, monkeypatch):
        from regime_platform.core import feature_flags as ff
        monkeypatch.setattr(ff, "_load", lambda: {})
        assert ff.get_kairos_mode() == "shadow"
        monkeypatch.setattr(ff, "_load", lambda: {"kairos_mode": "OFF"})
        assert ff.get_kairos_mode() == "off"
        monkeypatch.setattr(ff, "_load", lambda: {"kairos_mode": "bogus"})
        assert ff.get_kairos_mode() == "shadow"

    def test_yaml_ships_shadow(self):
        from pathlib import Path

        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "feature_flags.yaml").read_text())
        assert cfg["kairos_mode"] == "shadow"
